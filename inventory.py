"""Read local libvirt inventory without changing domains."""

import subprocess
import xml.etree.ElementTree as ET


def _virsh(config: dict, *args: str) -> str:
    return subprocess.run(
        ["virsh", "-c", config["host"]["libvirt_uri"], *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def local_names(config: dict) -> list[str]:
    return sorted(set(_virsh(config, "list", "--all", "--name").splitlines()))


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
    disk_bytes = 0
    for disk in disks:
        target = disk.find("target")
        source = disk.find("source")
        if target is None or not target.get("dev"):
            raise ValueError(f"VM {name}: disk has no target")
        disk_paths.append((source.get("file") or source.get("dev")) if source is not None else None)
        info = _virsh(config, "domblkinfo", name, target.get("dev"))
        capacity = next((line.split(":", 1)[1].strip() for line in info.splitlines()
                         if line.startswith("Capacity:")), None)
        if capacity is None or not capacity.isdecimal():
            raise ValueError(f"VM {name}: cannot read capacity of {target.get('dev')}")
        disk_bytes += int(capacity)
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
        "name": name, "vcpus": int(vcpu), "memory_mb": memory_mb,
        "disk_mb": (disk_bytes + 1024**2 - 1) // 1024**2,
        "disk_paths": disk_paths,
        "bridge": bridge.get("bridge") if bridge is not None else None,
        "mac_address": mac.get("address") if mac is not None else None,
        "description": root.findtext("./description") or "",
        "display": display or {"type": "none"},
        "status": "active" if state == "running" else "offline",
        "autostart": autostart == "enable",
    }
