"""Create a local libvirt guest from an ISO or cloud image."""

import json
import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import tempfile
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
    if "description" in vm and not isinstance(vm["description"], str):
        raise ValueError("vm.description must be a string")
    if "interface_name" in vm and (not isinstance(vm["interface_name"], str) or not vm["interface_name"]):
        raise ValueError("vm.interface_name must be a non-empty string")
    if "mac_address" in vm and (not isinstance(vm["mac_address"], str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", vm["mac_address"])):
        raise ValueError("vm.mac_address must be a 48-bit MAC address")
    if "display" in vm:
        validate_display(vm["display"])
    return vm


def validate_display(display: dict) -> None:
    if not isinstance(display, dict) or display.get("type") not in ("vnc", "spice", "none"):
        raise ValueError("vm.display.type must be vnc, spice or none")
    if display["type"] == "none":
        return
    try:
        ipaddress.ip_address(display.get("listen"))
    except ValueError as error:
        raise ValueError("vm.display.listen must be an IP address") from error
    port = display.get("port")
    if port != "auto" and (type(port) is not int or not 5900 <= port <= 65535):
        raise ValueError("vm.display.port must be auto or an integer from 5900 to 65535")
    password = display.get("password")
    if password is not None and (not isinstance(password, str) or not password):
        raise ValueError("vm.display.password must be a non-empty string")
    if display["type"] == "vnc" and password and len(password.encode("utf-8")) > 8:
        raise ValueError("VNC password must be at most 8 UTF-8 bytes")
    if display["listen"] not in ("127.0.0.1", "::1") and not password:
        raise ValueError("vm.display.password is required when listening beyond localhost")


def domain_xml(vm: dict, disk: Path, bridge: str, seed: Path | None = None) -> str:
    root = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(root, "name").text = vm["name"]
    if vm.get("uuid"):
        ET.SubElement(root, "uuid").text = vm["uuid"]
    if vm.get("description"):
        ET.SubElement(root, "description").text = vm["description"]
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
    if vm.get("mac_address"):
        ET.SubElement(interface, "mac", {"address": vm["mac_address"].lower()})
    ET.SubElement(interface, "source", {"bridge": bridge})
    ET.SubElement(interface, "model", {"type": "virtio"})
    ET.SubElement(devices, "serial", {"type": "pty"})
    ET.SubElement(devices, "console", {"type": "pty"})
    display = vm.get("display", {"type": "vnc", "listen": "127.0.0.1", "port": "auto"})
    if display["type"] != "none":
        attributes = {"type": display["type"], "listen": display["listen"]}
        if display["port"] == "auto":
            attributes.update({"autoport": "yes", "port": "-1"})
        else:
            attributes.update({"autoport": "no", "port": str(display["port"])})
        if display.get("password"):
            attributes["passwd"] = display["password"]
        ET.SubElement(devices, "graphics", attributes)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"


def verify_local_vm(vm: dict, config: dict) -> None:
    result = subprocess.run(
        ["virsh", "-c", config["host"]["libvirt_uri"], "dumpxml", "--inactive", "--security-info", vm["name"]],
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
    mac = root.find("./devices/interface[@type='bridge']/mac")
    graphics = root.find("./devices/graphics")
    display = vm.get("display")
    display_matches = True
    if display:
        display_matches = (graphics is None if display["type"] == "none" else
            graphics is not None and graphics.get("type") == display["type"]
            and graphics.get("listen") == display["listen"]
            and graphics.get("autoport") == ("yes" if display["port"] == "auto" else "no")
            and (display["port"] == "auto" or graphics.get("port") == str(display["port"]))
            and graphics.get("passwd") == display.get("password"))
    if (
        root.findtext("./name") != vm["name"]
        or actual_memory != vm["memory_mb"]
        or root.findtext("./vcpu") != str(vm["vcpus"])
        or not any(item.get("file") == expected_disk for item in disks)
        or not any(item.get("file") == expected_media for item in cdroms)
        or not any(item.get("bridge") == vm["bridge"] for item in bridges)
        or ("description" in vm and (root.findtext("./description") or "") != vm["description"])
        or ("mac_address" in vm and (mac is None or mac.get("address", "").lower() != vm["mac_address"].lower()))
        or not display_matches
    ):
        raise ValueError(f"local VM {vm['name']} differs from its NetBox definition")


def redefine_vm(vm: dict, config: dict, project_dir: Path, dry_run: bool) -> None:
    """Apply a changed NetBox definition to an offline VM created by vmctl."""
    uri = config["host"]["libvirt_uri"]
    name = vm["name"]
    state = subprocess.run(["virsh", "-c", uri, "domstate", name],
                           check=True, capture_output=True, text=True).stdout.strip()
    if state != "shut off":
        raise ValueError(f"shut down {name} before changing its libvirt definition")
    disk = Path(vm["storage_directory"]) / f"{name}.qcow2"
    xml_path = project_dir / "state" / "domains" / f"{name}.xml"
    current = subprocess.run(["virsh", "-c", uri, "dumpxml", "--inactive", "--security-info", name],
                             check=True, capture_output=True, text=True).stdout
    root = ET.fromstring(current)
    sources = root.findall("./devices/disk[@device='disk']/source")
    if len(sources) != 1 or sources[0].get("file") != str(disk) or not xml_path.is_file():
        raise ValueError(f"{name} is not a single-disk VM managed from {xml_path}")
    info = subprocess.run(["qemu-img", "info", "--output=json", str(disk)],
                          check=True, capture_output=True, text=True)
    disk_info = json.loads(info.stdout)
    if disk_info.get("format") != "qcow2" or disk_info.get("virtual-size") != vm["disk_gb"] * 1024**3:
        raise ValueError(f"local disk differs from NetBox: {disk}")
    seed = Path(vm["storage_directory"]) / f"{name}-seed.iso" if vm["source"] == "cloud_image" else None
    if seed and not seed.is_file():
        raise ValueError(f"cloud-init seed not found: {seed}")
    if dry_run:
        print(f"WOULD UPDATE local XML for {name} from NetBox")
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=xml_path.parent,
                                     prefix=f".{name}-", delete=False) as file:
        temporary_xml = Path(file.name)
        file.write(domain_xml({**vm, "uuid": root.findtext("./uuid")}, disk, vm["bridge"], seed))
    os.replace(temporary_xml, xml_path)
    subprocess.run(["virsh", "-c", uri, "define", str(xml_path)], check=True)
    print(f"Updated local VM definition from NetBox: {name}")


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
    if disk.exists():
        info = subprocess.run(
            ["qemu-img", "info", "--output=json", str(disk)],
            check=True, capture_output=True, text=True,
        )
        existing_disk = json.loads(info.stdout)
        if existing_disk.get("format") != "qcow2" or existing_disk.get("virtual-size") != vm["disk_gb"] * 1024**3:
            raise ValueError(f"existing disk differs from NetBox: {disk}")
        if seed:
            raise ValueError(f"cloud image disk already exists; inspect before retrying: {disk}")
    if xml_path.exists():
        previous = ET.fromstring(xml_path.read_text(encoding="utf-8"))
        previous_sources = previous.findall("./devices/disk[@device='disk']/source")
        if previous.findtext("./name") != vm["name"] or not any(item.get("file") == str(disk) for item in previous_sources):
            raise ValueError(f"existing XML differs from this VM: {xml_path}")
    if seed and seed.exists():
        raise ValueError(f"cloud-init seed already exists; inspect before retrying: {seed}")
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
    if not disk.exists():
        for command in disk_commands:
            subprocess.run(command, check=True)
    if seed and seed_cmd:
        seed_dir.mkdir(parents=True, exist_ok=True)
        (seed_dir / "user-data").write_bytes(Path(vm["user_data"]).read_bytes())
        (seed_dir / "meta-data").write_text(
            f"instance-id: {vm['name']}\nlocal-hostname: {vm['name']}\n", encoding="utf-8",
        )
        subprocess.run(seed_cmd, check=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=xml_path.parent, prefix=f".{vm['name']}-", delete=False) as file:
        temporary_xml = Path(file.name)
        file.write(domain_xml(vm, disk, bridge, seed))
    os.replace(temporary_xml, xml_path)
    subprocess.run(define_cmd, check=True)
    print(f"VM defined: {vm['name']}. Start it with: vmctl start {vm['name']}")
    return 0
