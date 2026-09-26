"""Create a local libvirt guest from an ISO or cloud image."""

import json
import hashlib
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


def vm_disks(vm: dict, default_disk: Path) -> list[dict]:
    return vm.get("disks") or [{"name": vm["name"], "path": str(default_disk), "size_gb": vm["disk_gb"]}]


def vm_interfaces(vm: dict, default_bridge: str) -> list[dict]:
    if "interfaces" in vm:
        return vm["interfaces"]
    return [{"name": vm.get("interface_name") or "inet", "bridge": default_bridge,
             "mac_address": vm.get("mac_address")}]


def interface_alias(name: str) -> str:
    return "ua-vmctl-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]


def pending_purge_file(project_dir: Path, name: str) -> Path:
    return project_dir / "state" / "pending-purge" / f"{name}.json"


def purge_pending(vm: dict, config: dict, project_dir: Path,
                  attached_paths: set[str] | None = None) -> None:
    pending = pending_purge_file(project_dir, vm["name"])
    if not pending.exists():
        return
    paths = json.loads(pending.read_text(encoding="utf-8"))
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError(f"invalid pending purge state: {pending}")
    directory = Path(vm.get("storage_directory") or config["storage"]["directory"])
    for value in paths:
        path = Path(value)
        if path.parent != directory or path.is_symlink() or not path.name.startswith(f"{vm['name']}-") or path.suffix != ".qcow2":
            raise ValueError(f"cannot purge unmanaged disk: {path}")
    if attached_paths is None:
        current = subprocess.run(["virsh", "-c", config["host"]["libvirt_uri"], "dumpxml", "--inactive", vm["name"]],
                                 check=True, capture_output=True, text=True).stdout
        root = ET.fromstring(current)
        attached_paths = {source.get("file") or source.get("dev")
                          for source in root.findall("./devices/disk[@device='disk']/source")}
    if any(path in attached_paths for path in paths):
        raise ValueError("pending purge disk is still attached to libvirt")
    for value in paths:
        Path(value).unlink(missing_ok=True)
    pending.unlink()


def _disk_element(devices: ET.Element, path: str, target: str) -> ET.Element:
    node = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(node, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(node, "source", {"file": path})
    ET.SubElement(node, "target", {"dev": target, "bus": "virtio"})
    return node


def _interface_element(devices: ET.Element, item: dict) -> ET.Element:
    node = ET.SubElement(devices, "interface", {"type": "bridge"})
    if item.get("mac_address"):
        ET.SubElement(node, "mac", {"address": item["mac_address"].lower()})
    ET.SubElement(node, "source", {"bridge": item["bridge"]})
    ET.SubElement(node, "model", {"type": "virtio"})
    ET.SubElement(node, "alias", {"name": item.get("alias") or interface_alias(item["name"])})
    return node


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
    for index, item in enumerate(vm_disks(vm, disk)):
        if index >= 26:
            raise ValueError("at most 26 virtio disks are supported")
        _disk_element(devices, item["path"], "vd" + chr(ord("a") + index))
    media = vm["iso"] if vm["source"] == "iso" else str(seed)
    cdrom = ET.SubElement(devices, "disk", {"type": "file", "device": "cdrom"})
    ET.SubElement(cdrom, "driver", {"name": "qemu", "type": "raw"})
    ET.SubElement(cdrom, "source", {"file": media})
    ET.SubElement(cdrom, "target", {"dev": "sda", "bus": "sata"})
    ET.SubElement(cdrom, "readonly")
    for item in vm_interfaces(vm, bridge):
        _interface_element(devices, item)
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
    directory = vm.get("storage_directory") or config.get("storage", {}).get("directory", "/")
    expected_disk = str(Path(directory) / f"{vm['name']}.qcow2")
    expected_media = (vm["iso"] if vm["source"] == "iso" else
                      str(Path(directory) / f"{vm['name']}-seed.iso") if vm["source"] == "cloud_image" else None)
    disks = root.findall("./devices/disk[@device='disk']/source")
    cdroms = root.findall("./devices/disk[@device='cdrom']/source")
    actual_interfaces = root.findall("./devices/interface[@type='bridge']")
    wanted_disks = vm_disks(vm, Path(expected_disk))
    wanted_interfaces = vm_interfaces(vm, vm["bridge"])
    actual_paths = {item.get("file") or item.get("dev") for item in disks}
    disk_match = len(disks) == len(wanted_disks) and actual_paths == {item["path"] for item in wanted_disks}
    if len(actual_interfaces) != len(wanted_interfaces):
        interface_match = False
    elif len(wanted_interfaces) == 1:
        local = actual_interfaces[0]
        wanted = wanted_interfaces[0]
        interface_match = (local.find("source") is not None
                           and local.find("source").get("bridge") == wanted["bridge"]
                           and (not wanted.get("mac_address") or
                                local.find("mac") is not None and local.find("mac").get("address", "").lower() == wanted["mac_address"].lower()))
    else:
        actual_by_alias = {node.find("alias").get("name"): node for node in actual_interfaces if node.find("alias") is not None}
        interface_match = len(actual_by_alias) == len(wanted_interfaces)
        for wanted in wanted_interfaces:
            local = actual_by_alias.get(wanted.get("alias") or interface_alias(wanted["name"]))
            if local is None or local.find("source") is None or local.find("mac") is None or \
                    local.find("source").get("bridge") != wanted["bridge"] or \
                    local.find("mac").get("address", "").lower() != wanted["mac_address"].lower():
                interface_match = False
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
        or not disk_match
        or (expected_media is not None and not any(item.get("file") == expected_media for item in cdroms))
        or not interface_match
        or ("description" in vm and (root.findtext("./description") or "") != vm["description"])
        or not display_matches
    ):
        raise ValueError(f"local VM {vm['name']} differs from its NetBox definition")


def redefine_vm(vm: dict, config: dict, project_dir: Path, dry_run: bool, purge: bool = False) -> None:
    """Reconcile an offline libvirt definition while preserving unrelated XML devices."""
    uri = config["host"]["libvirt_uri"]
    name = vm["name"]
    state = subprocess.run(["virsh", "-c", uri, "domstate", name],
                           check=True, capture_output=True, text=True).stdout.strip()
    if state != "shut off":
        raise ValueError(f"shut down {name} before changing its libvirt definition")
    xml_path = project_dir / "state" / "domains" / f"{name}.xml"
    current = subprocess.run(["virsh", "-c", uri, "dumpxml", "--inactive", "--security-info", name],
                             check=True, capture_output=True, text=True).stdout
    root = ET.fromstring(current)
    devices = root.find("./devices")
    if devices is None:
        raise ValueError(f"{name} has no libvirt devices")
    directory = vm.get("storage_directory") or config.get("storage", {}).get("directory")
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("storage.directory is required")
    desired_disks = vm_disks(vm, Path(directory) / f"{name}.qcow2")
    desired_paths = {item["path"] for item in desired_disks}
    existing_disks = {}
    used_targets = set()
    for node in devices.findall("./disk[@device='disk']"):
        source, target = node.find("source"), node.find("target")
        if source is None or not (source.get("file") or source.get("dev")) or target is None or not target.get("dev"):
            raise ValueError(f"{name} has an unsupported disk")
        existing_disks[source.get("file") or source.get("dev")] = node
        used_targets.add(target.get("dev"))
    removed_paths = set(existing_disks) - desired_paths
    for item in desired_disks:
        path = Path(item["path"])
        if vm["source"] == "existing" and item["path"] in existing_disks:
            continue
        if path.exists():
            info = subprocess.run(["qemu-img", "info", "--output=json", str(path)],
                                  check=True, capture_output=True, text=True)
            disk_info = json.loads(info.stdout)
            if disk_info.get("format") != "qcow2" or disk_info.get("virtual-size") != item["size_gb"] * 1024**3:
                raise ValueError(f"local disk differs from NetBox: {path}; resizing is unsupported")
        elif item["path"] in existing_disks:
            raise ValueError(f"attached disk is missing: {path}")
        elif path.parent != Path(directory) or path.is_symlink():
            raise ValueError(f"refusing to create disk outside managed storage: {path}")
    for path in removed_paths:
        devices.remove(existing_disks[path])
    for item in desired_disks:
        if item["path"] not in existing_disks:
            target = next((f"vd{chr(letter)}" for letter in range(ord("a"), ord("z") + 1)
                           if f"vd{chr(letter)}" not in used_targets), None)
            if target is None:
                raise ValueError("no free virtio disk target")
            used_targets.add(target)
            _disk_element(devices, item["path"], target)
    desired_interfaces = vm_interfaces(vm, vm.get("bridge") or config["network"]["bridge"])
    wanted = {item.get("alias") or interface_alias(item["name"]): item for item in desired_interfaces}
    current_interfaces = devices.findall("./interface[@type='bridge']")
    used = set()
    for node in current_interfaces:
        alias = node.find("alias")
        mac = node.find("mac")
        key = alias.get("name") if alias is not None else None
        if key not in wanted and mac is not None:
            key = next((alias_name for alias_name, item in wanted.items()
                        if item["mac_address"].lower() == mac.get("address", "").lower() and alias_name not in used), None)
        if key not in wanted and len(current_interfaces) == len(desired_interfaces) == 1:
            key = next(iter(wanted))
        if key not in wanted:
            devices.remove(node)
            continue
        item = wanted[key]
        used.add(key)
        if alias is None:
            ET.SubElement(node, "alias", {"name": key})
        else:
            alias.set("name", key)
        if mac is None:
            mac = ET.SubElement(node, "mac")
        mac.set("address", item["mac_address"].lower())
        source = node.find("source")
        if source is None:
            source = ET.SubElement(node, "source")
        source.set("bridge", item["bridge"])
    for key, item in wanted.items():
        if key not in used:
            _interface_element(devices, item)
    root.find("./memory").text = str(vm["memory_mb"])
    root.find("./memory").set("unit", "MiB")
    root.find("./vcpu").text = str(vm["vcpus"])
    description = root.find("./description")
    if description is None:
        description = ET.Element("description")
        root.insert(1 + (root.find("./uuid") is not None), description)
    description.text = vm.get("description") or ""
    display = vm.get("display")
    if display is not None:
        graphics = devices.find("./graphics")
        if graphics is not None:
            devices.remove(graphics)
    if display and display["type"] != "none":
        attributes = {"type": display["type"], "listen": display["listen"],
                      "autoport": "yes" if display["port"] == "auto" else "no",
                      "port": "-1" if display["port"] == "auto" else str(display["port"])}
        if display.get("password"):
            attributes["passwd"] = display["password"]
        ET.SubElement(devices, "graphics", attributes)
    if dry_run:
        print(f"WOULD UPDATE local XML for {name} from NetBox")
        for path in sorted(removed_paths):
            print(f"WOULD {'PURGE' if purge else 'KEEP'} detached disk {path}")
        return
    if purge and removed_paths:
        for value in removed_paths:
            path = Path(value)
            if path.parent != Path(directory) or path.is_symlink() or not path.name.startswith(f"{name}-") or path.suffix != ".qcow2":
                raise ValueError(f"cannot purge unmanaged disk: {path}")
        pending = pending_purge_file(project_dir, name)
        pending.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(pending.read_text(encoding="utf-8")) if pending.exists() else []
        pending.write_text(json.dumps(sorted(set(previous) | removed_paths)), encoding="utf-8")
    for item in desired_disks:
        path = Path(item["path"])
        if not path.exists():
            subprocess.run(["qemu-img", "create", "-f", "qcow2", str(path), f"{item['size_gb']}G"], check=True)
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=xml_path.parent,
                                     prefix=f".{name}-", delete=False) as file:
        temporary_xml = Path(file.name)
        file.write(ET.tostring(root, encoding="unicode") + "\n")
    os.replace(temporary_xml, xml_path)
    subprocess.run(["virsh", "-c", uri, "define", str(xml_path)], check=True)
    if purge:
        purge_pending(vm, config, project_dir, desired_paths)
    print(f"Updated local VM definition from NetBox: {name}")


def create_vm(vm: dict, config: dict, project_dir: Path, dry_run: bool) -> int:
    disk_dir_value = vm.get("storage_directory")
    bridge = vm.get("bridge")
    if not isinstance(disk_dir_value, str) or not Path(disk_dir_value).is_absolute():
        raise ValueError("config needs an absolute storage.directory")
    if not isinstance(bridge, str) or not bridge or Path(bridge).name != bridge:
        raise ValueError("config needs network.bridge")
    disk_dir = Path(disk_dir_value)
    wanted_disks = vm_disks(vm, disk_dir / f"{vm['name']}.qcow2")
    if not wanted_disks or wanted_disks[0]["name"] != vm["name"]:
        raise ValueError("VM needs a boot disk named after the VM")
    disk = Path(wanted_disks[0]["path"])
    seed = disk_dir / f"{vm['name']}-seed.iso" if vm["source"] == "cloud_image" else None
    xml_path = project_dir / "state" / "domains" / f"{vm['name']}.xml"
    seed_dir = project_dir / "state" / "cloud-init" / vm["name"]
    uri = config["host"]["libvirt_uri"]
    if vm["source"] == "iso":
        disk_commands = [["qemu-img", "create", "-f", "qcow2", str(disk), f"{wanted_disks[0]['size_gb']}G"]]
    else:
        disk_commands = [
            ["qemu-img", "convert", "-O", "qcow2", vm["image"], str(disk)],
            ["qemu-img", "resize", str(disk), f"{wanted_disks[0]['size_gb']}G"],
        ]
    for item in wanted_disks[1:]:
        disk_commands.append(["qemu-img", "create", "-f", "qcow2", item["path"], f"{item['size_gb']}G"])
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
    for item in wanted_disks:
        item_path = Path(item["path"])
        if item_path.parent != disk_dir or item_path.is_symlink():
            raise ValueError(f"disk must be in managed storage: {item_path}")
        if not item_path.exists():
            continue
        info = subprocess.run(
            ["qemu-img", "info", "--output=json", str(item_path)],
            check=True, capture_output=True, text=True,
        )
        existing_disk = json.loads(info.stdout)
        if existing_disk.get("format") != "qcow2" or existing_disk.get("virtual-size") != item["size_gb"] * 1024**3:
            raise ValueError(f"existing disk differs from NetBox: {item_path}")
        if seed and item_path == disk:
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
    for item in vm_interfaces(vm, bridge):
        if not (Path("/sys/class/net") / item["bridge"] / "bridge").is_dir():
            raise ValueError(f"network bridge not found: {item['bridge']}")
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
        if image_size > wanted_disks[0]["size_gb"] * 1024**3:
            raise ValueError("vm.disk_gb is smaller than the cloud image virtual size")
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    if not disk.exists():
        for command in disk_commands[:2 if seed else 1]:
            subprocess.run(command, check=True)
    for item in wanted_disks[1:]:
        if not Path(item["path"]).exists():
            subprocess.run(["qemu-img", "create", "-f", "qcow2", item["path"], f"{item['size_gb']}G"], check=True)
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
