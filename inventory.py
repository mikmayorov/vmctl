"""Read local libvirt inventory without changing domains."""

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def _virsh(config: dict, *args: str) -> str:
    return subprocess.run(
        ["virsh", "-c", config["host"]["libvirt_uri"], *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def local_names(config: dict) -> list[str]:
    return sorted(set(_virsh(config, "list", "--all", "--name").splitlines()))


def inspect_display(config: dict, name: str) -> dict:
    """Return effective graphics settings, including the live auto-assigned port."""
    root = ET.fromstring(_virsh(config, "dumpxml", "--security-info", name))
    graphics = root.find("./devices/graphics")
    if graphics is None:
        return {"type": "none"}
    listener = graphics.find("./listen[@type='address']")
    listen = graphics.get("listen") or (listener.get("address") if listener is not None else None)
    port_text = graphics.get("port")
    port = int(port_text) if port_text and int(port_text) >= 0 else "auto"
    result = {"type": graphics.get("type"), "listen": listen, "port": port}
    if graphics.get("passwd"):
        result["password"] = graphics.get("passwd")
    return result


def inspect_vm(config: dict, name: str) -> dict:
    root = ET.fromstring(_virsh(config, "dumpxml", "--security-info", name))
    memory = root.find("./memory")
    vcpu = root.findtext("./vcpu")
    units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}
    if memory is None or memory.text is None or memory.get("unit", "KiB") not in units or not vcpu:
        raise ValueError(f"VM {name}: cannot read memory or vCPU count")
    memory_mb = int(int(memory.text) * units[memory.get("unit", "KiB")])
    disks = root.findall("./devices/disk[@device='disk']")
    disk_paths = []
    disk_items = []
    for disk in disks:
        target = disk.find("target")
        source = disk.find("source")
        if target is None or not target.get("dev"):
            raise ValueError(f"VM {name}: disk has no target")
        path = (source.get("file") or source.get("dev")) if source is not None else None
        disk_paths.append(path)
        info = _virsh(config, "domblkinfo", name, target.get("dev"))
        capacity = next((line.split(":", 1)[1].strip() for line in info.splitlines()
                         if line.startswith("Capacity:")), None)
        if capacity is None or not capacity.isdecimal():
            raise ValueError(f"VM {name}: cannot read capacity of {target.get('dev')}")
        disk_items.append({"path": path, "target": target.get("dev"), "size_bytes": int(capacity),
                           "size_mb": (int(capacity) + 1024**2 - 1) // 1024**2,
                           "size_gb": (int(capacity) + 1024**3 - 1) // 1024**3})
    mounted_media = []
    for node in root.findall("./devices/disk[@device='cdrom']"):
        source, target = node.find("source"), node.find("target")
        path = (source.get("file") or source.get("dev")) if source is not None else None
        if path:
            media = {"path": path, "target": target.get("dev") if target is not None else None}
            try:
                size_bytes = Path(path).stat().st_size
            except OSError:
                size_bytes = 0
            if not size_bytes and media["target"]:
                try:
                    info = _virsh(config, "domblkinfo", name, media["target"])
                    capacity = next((line.split(":", 1)[1].strip() for line in info.splitlines()
                                     if line.startswith("Capacity:")), None)
                    size_bytes = int(capacity) if capacity and capacity.isdecimal() else 0
                except subprocess.CalledProcessError:
                    pass
            if size_bytes:
                media["size_mb"] = (size_bytes + 1024**2 - 1) // 1024**2
            mounted_media.append(media)
    unsupported_storage = sorted({node.get("device", "unknown") for node in root.findall("./devices/disk")
                                  if node.get("device") not in ("disk", "cdrom")})
    unsupported_interfaces = sorted({node.get("type", "unknown") for node in root.findall("./devices/interface")
                                     if node.get("type") != "bridge"})
    nic_items = []
    for index, node in enumerate(root.findall("./devices/interface[@type='bridge']")):
        source, mac, alias, target = node.find("source"), node.find("mac"), node.find("alias"), node.find("target")
        nic_items.append({"name": "inet" if index == 0 else f"net-{index + 1}",
                          "bridge": source.get("bridge") if source is not None else None,
                          "mac_address": mac.get("address") if mac is not None else None,
                          "host_dev": target.get("dev") if target is not None else None,
                          "alias": alias.get("name") if alias is not None else None})
    bridge = root.find("./devices/interface[@type='bridge']/source")
    mac = root.find("./devices/interface[@type='bridge']/mac")
    graphics = root.find("./devices/graphics")
    display = None
    if graphics is not None:
        display = {
            "type": graphics.get("type"), "listen": graphics.get("listen"),
            "port": "auto" if graphics.get("autoport") == "yes" else int(graphics.get("port", "-1")),
        }
        if graphics.get("passwd"):
            display["password"] = graphics.get("passwd")
    state = _virsh(config, "domstate", name)
    if state not in ("running", "shut off"):
        raise ValueError(f"VM {name}: unsupported power state {state!r}")
    dominfo = _virsh(config, "dominfo", name)
    autostart = next((line.split(":", 1)[1].strip().lower() for line in dominfo.splitlines()
                      if line.startswith("Autostart:")), "")
    if autostart not in ("enable", "disable"):
        raise ValueError(f"VM {name}: cannot read autostart state")
    return {
        "name": name, "uuid": root.findtext("./uuid"), "vcpus": int(vcpu), "memory_mb": memory_mb,
        "disk_mb": sum(item["size_mb"] for item in disk_items),
        "disk_paths": disk_paths,
        "disks": disk_items,
        "mounted_media": mounted_media,
        "unsupported_storage": unsupported_storage,
        "unsupported_interfaces": unsupported_interfaces,
        "interfaces": nic_items,
        "bridge": bridge.get("bridge") if bridge is not None else None,
        "mac_address": mac.get("address") if mac is not None else None,
        "description": root.findtext("./description") or "",
        "display": display or {"type": "none"},
        "status": "active" if state == "running" else "offline",
        "autostart": autostart == "enable",
    }
