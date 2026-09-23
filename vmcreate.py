"""Create a local libvirt guest from an ISO or cloud image."""

import json
import re
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def validate_spec(data: dict) -> dict:
    vm = data.get("vm")
    if not isinstance(vm, dict):
        raise ValueError("VM spec needs a [vm] table")
    name = vm.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("vm.name must be 1-64 letters, digits, dots, underscores or hyphens")
    for key in ("memory_mb", "vcpus", "disk_gb"):
        if type(vm.get(key)) is not int or vm[key] <= 0:
            raise ValueError(f"vm.{key} must be a positive integer")
    source = vm.get("source")
    if source not in ("iso", "cloud_image"):
        raise ValueError("vm.source must be 'iso' or 'cloud_image'")
    for field in (("iso",) if source == "iso" else ("image", "user_data")):
        value = vm.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"vm.{field} must be an absolute path on the host")
    return vm


def domain_xml(vm: dict, disk: Path, bridge: str, seed: Path | None = None) -> str:
    root = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(root, "name").text = vm["name"]
    ET.SubElement(root, "memory", {"unit": "MiB"}).text = str(vm["memory_mb"])
    ET.SubElement(root, "vcpu").text = str(vm["vcpus"])
    os_node = ET.SubElement(root, "os")
    ET.SubElement(os_node, "type", {"arch": "x86_64"}).text = "hvm"
    if vm["source"] == "iso":
        ET.SubElement(os_node, "boot", {"dev": "cdrom"})
    ET.SubElement(os_node, "boot", {"dev": "hd"})
    features = ET.SubElement(root, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(root, "cpu", {"mode": "host-passthrough"})
    devices = ET.SubElement(root, "devices")
    disk_node = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(disk_node, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(disk_node, "source", {"file": str(disk)})
    ET.SubElement(disk_node, "target", {"dev": "vda", "bus": "virtio"})
    media = vm["iso"] if vm["source"] == "iso" else str(seed)
    cdrom = ET.SubElement(devices, "disk", {"type": "file", "device": "cdrom"})
    ET.SubElement(cdrom, "driver", {"name": "qemu", "type": "raw"})
    ET.SubElement(cdrom, "source", {"file": media})
    ET.SubElement(cdrom, "target", {"dev": "sda", "bus": "sata"})
    ET.SubElement(cdrom, "readonly")
    interface = ET.SubElement(devices, "interface", {"type": "bridge"})
    ET.SubElement(interface, "source", {"bridge": bridge})
    ET.SubElement(interface, "model", {"type": "virtio"})
    ET.SubElement(devices, "serial", {"type": "pty"})
    ET.SubElement(devices, "console", {"type": "pty"})
    ET.SubElement(devices, "graphics", {"type": "vnc", "autoport": "yes", "listen": "127.0.0.1"})
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"


def verify_local_vm(vm: dict, config: dict) -> None:
    result = subprocess.run(
        ["virsh", "-c", config["host"]["libvirt_uri"], "dumpxml", "--inactive", vm["name"]],
        check=True, capture_output=True, text=True,
    )
    root = ET.fromstring(result.stdout)
    memory = root.find("./memory")
    units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}
    actual_memory = int(memory.text) * units.get(memory.get("unit", "KiB"), 0) if memory is not None else None
    expected_disk = str(Path(vm["storage_directory"]) / f"{vm['name']}.qcow2")
    expected_media = (
        vm["iso"] if vm["source"] == "iso"
        else str(Path(vm["storage_directory"]) / f"{vm['name']}-seed.iso")
    )
    disks = root.findall("./devices/disk[@device='disk']/source")
    cdroms = root.findall("./devices/disk[@device='cdrom']/source")
    bridges = root.findall("./devices/interface[@type='bridge']/source")
    if (
        root.findtext("./name") != vm["name"]
        or actual_memory != vm["memory_mb"]
        or root.findtext("./vcpu") != str(vm["vcpus"])
        or not any(item.get("file") == expected_disk for item in disks)
        or not any(item.get("file") == expected_media for item in cdroms)
        or not any(item.get("bridge") == vm["bridge"] for item in bridges)
    ):
        raise ValueError(f"local VM {vm['name']} differs from its NetBox definition")


def create_vm(vm: dict, config: dict, project_dir: Path, dry_run: bool) -> int:
    disk_dir_value = vm.get("storage_directory")
    bridge = vm.get("bridge")
    if not isinstance(disk_dir_value, str) or not Path(disk_dir_value).is_absolute():
        raise ValueError("config needs an absolute storage.directory")
    if not isinstance(bridge, str) or not bridge or Path(bridge).name != bridge:
        raise ValueError("config needs network.bridge")
    disk_dir = Path(disk_dir_value)
    disk = disk_dir / f"{vm['name']}.qcow2"
    seed = disk_dir / f"{vm['name']}-seed.iso" if vm["source"] == "cloud_image" else None
    xml_path = project_dir / "state" / "domains" / f"{vm['name']}.xml"
    seed_dir = project_dir / "state" / "cloud-init" / vm["name"]
    uri = config["host"]["libvirt_uri"]
    if vm["source"] == "iso":
        disk_commands = [["qemu-img", "create", "-f", "qcow2", str(disk), f"{vm['disk_gb']}G"]]
    else:
        disk_commands = [
            ["qemu-img", "convert", "-O", "qcow2", vm["image"], str(disk)],
            ["qemu-img", "resize", str(disk), f"{vm['disk_gb']}G"],
        ]
    seed_cmd = [
        "xorriso", "-as", "mkisofs", "-V", "cidata", "-r", "-J", "-o", str(seed),
        "-graft-points", f"user-data={seed_dir / 'user-data'}",
        f"meta-data={seed_dir / 'meta-data'}",
    ] if seed else None
    define_cmd = ["virsh", "-c", uri, "define", str(xml_path)]
    print(f"Source: {vm['iso'] if vm['source'] == 'iso' else vm['image']}")
    print(f"Bridge: {bridge}")
    print(f"Domain XML: {xml_path}")
    print(f"Disk: {disk}")
    if seed:
        print(f"Cloud-init seed: {seed}")
    if dry_run:
        for command in disk_commands:
            print(shlex.join(command))
        if seed_cmd:
            print(shlex.join(seed_cmd))
        print(shlex.join(define_cmd))
        return 0

    source_path = Path(vm["iso"] if vm["source"] == "iso" else vm["image"])
    if not source_path.is_file():
        raise ValueError(f"source image not found: {source_path}")
    if seed and not Path(vm["user_data"]).is_file():
        raise ValueError(f"cloud-init user-data not found: {vm['user_data']}")
    if seed and b"REPLACE_WITH_YOUR_PUBLIC_KEY" in Path(vm["user_data"]).read_bytes():
        raise ValueError("replace the example SSH public key in cloud-init user-data")
    if not disk_dir.is_dir():
        raise ValueError(f"storage directory not found: {disk_dir}")
    if disk.exists() or xml_path.exists() or (seed and seed.exists()):
        raise ValueError(f"VM artifacts already exist for: {vm['name']}")
    if seed and seed_dir.exists():
        raise ValueError(f"cloud-init state already exists: {seed_dir}")
    if not (Path("/sys/class/net") / bridge / "bridge").is_dir():
        raise ValueError(f"network bridge not found: {bridge}")
    existing = subprocess.run(
        ["virsh", "-c", uri, "list", "--all", "--name"],
        check=True, capture_output=True, text=True,
    )
    if vm["name"] in existing.stdout.splitlines():
        raise ValueError(f"VM already exists in libvirt: {vm['name']}")

    if seed and shutil.which("xorriso") is None:
        raise ValueError("xorriso is required for cloud images")
    if seed:
        info = subprocess.run(
            ["qemu-img", "info", "--output=json", str(source_path)],
            check=True, capture_output=True, text=True,
        )
        image_size = json.loads(info.stdout)["virtual-size"]
        if image_size > vm["disk_gb"] * 1024**3:
            raise ValueError("vm.disk_gb is smaller than the cloud image virtual size")
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    for command in disk_commands:
        subprocess.run(command, check=True)
    if seed and seed_cmd:
        seed_dir.mkdir(parents=True, exist_ok=True)
        (seed_dir / "user-data").write_bytes(Path(vm["user_data"]).read_bytes())
        (seed_dir / "meta-data").write_text(
            f"instance-id: {vm['name']}\nlocal-hostname: {vm['name']}\n", encoding="utf-8",
        )
        subprocess.run(seed_cmd, check=True)
    with xml_path.open("x", encoding="utf-8") as file:
        file.write(domain_xml(vm, disk, bridge, seed))
    subprocess.run(define_cmd, check=True)
    print(f"VM defined: {vm['name']}. Start it with: python3 vmctl.py start {vm['name']}")
    return 0
