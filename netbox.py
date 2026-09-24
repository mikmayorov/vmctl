"""Read desired VM state from NetBox and write it before local changes."""

import json
import os
import secrets
from pathlib import Path
from decimal import Decimal, InvalidOperation
import urllib.error
import urllib.parse
import urllib.request


class NetBoxError(Exception):
    pass


KEY_FILE = Path(__file__).with_name("netbox.key")


def has_netbox_key(config: dict) -> bool:
    netbox = config.get("netbox", {})
    inline = netbox.get("key") if isinstance(netbox, dict) else None
    return bool(os.environ.get("NETBOX_TOKEN") or inline or KEY_FILE.is_file())


def _token(config: dict) -> str:
    netbox = config.get("netbox", {})
    token = os.environ.get("NETBOX_TOKEN") or netbox.get("key")
    if token:
        return token
    try:
        if KEY_FILE.stat().st_mode & 0o077:
            raise NetBoxError(f"protect {KEY_FILE} with chmod 600")
        token = KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise NetBoxError(f"set NETBOX_TOKEN, netbox.key or create {KEY_FILE}") from error
    except OSError as error:
        raise NetBoxError(f"cannot read {KEY_FILE}: {error}") from error
    if not token:
        raise NetBoxError(f"{KEY_FILE} is empty")
    return token


def _settings(config: dict) -> tuple[str, str]:
    netbox = config.get("netbox", {})
    url = netbox.get("url") if isinstance(netbox, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise NetBoxError("config needs an HTTPS netbox.url")
    token = _token(config)
    return url.rstrip("/"), token


def _request(config: dict, method: str, path: str, payload: dict | None = None) -> dict:
    base_url, token = _settings(config)
    scheme = "Bearer" if token.startswith("nbt_") else "Token"
    headers = {
        "Authorization": f"{scheme} {token}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/api/" + path.lstrip("/"), data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read(500).decode("utf-8", errors="replace")
        raise NetBoxError(f"HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise NetBoxError(str(error)) from error


def find_device(config: dict) -> tuple[int, int]:
    settings = config.get("netbox", {})
    device = settings.get("device")
    site = settings.get("site") or None
    tenant = settings.get("tenant") or None
    for field, value in (("site", site), ("tenant", tenant)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise NetBoxError(f"netbox.{field} must be a non-empty slug")
    if type(device) is int and device > 0:
        result = _request(config, "GET", f"dcim/devices/{device}/")
    elif isinstance(device, str) and device:
        query = urllib.parse.urlencode({"name": device})
        matches = [
            item for item in _list_results(config, f"dcim/devices/?{query}")
            if item.get("name") == device
            and (site is None or (item.get("site") or {}).get("slug") == site)
            and (tenant is None or (item.get("tenant") or {}).get("slug") == tenant)
        ]
        if len(matches) != 1:
            suffix = " set netbox.site and, if needed, netbox.tenant" if len(matches) > 1 else " check netbox.device/site/tenant"
            raise NetBoxError(f"expected one NetBox device named {device!r}, found {len(matches)};{suffix}")
        result = matches[0]
    else:
        raise NetBoxError("set netbox.device to a NetBox device name or numeric ID")
    if site is not None and (result.get("site") or {}).get("slug") != site:
        raise NetBoxError(f"NetBox device {device!r} is not in site {site!r}")
    if tenant is not None and (result.get("tenant") or {}).get("slug") != tenant:
        raise NetBoxError(f"NetBox device {device!r} is not in tenant {tenant!r}")
    assigned_cluster = result.get("cluster")
    if not isinstance(assigned_cluster, dict) or type(assigned_cluster.get("id")) is not int:
        raise NetBoxError(f"NetBox device {device!r} is not assigned to a cluster")
    return result["id"], assigned_cluster["id"]


def find_vm(config: dict, name: str, cluster_id: int) -> dict | None:
    query = urllib.parse.urlencode({"name": name})
    matches = [
        item for item in _list_results(config, f"virtualization/virtual-machines/?{query}")
        if item.get("name") == name
        and isinstance(item.get("cluster"), dict)
        and item["cluster"].get("id") == cluster_id
    ]
    if len(matches) > 1:
        raise NetBoxError(f"multiple NetBox VMs named {name!r} in this cluster")
    return matches[0] if matches else None


def list_vms(config: dict, device_id: int) -> list[dict]:
    """List every VM assigned to this device, following NetBox pagination."""
    path = "virtualization/virtual-machines/?" + urllib.parse.urlencode(
        {"device_id": device_id, "limit": 100}
    )
    return [vm for vm in _list_results(config, path) if
            isinstance(vm.get("device"), dict) and vm["device"].get("id") == device_id]


def vm_disks(config: dict, vm_id: int) -> list[dict]:
    return _list_results(config, f"virtualization/virtual-disks/?virtual_machine_id={vm_id}&limit=100")


def vm_interfaces(config: dict, vm_id: int) -> list[dict]:
    return _list_results(config, f"virtualization/interfaces/?virtual_machine_id={vm_id}&limit=100")


def interface_ips(config: dict, interface_id: int) -> list[dict]:
    return _list_results(config, "ipam/ip-addresses/?" + urllib.parse.urlencode({
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": interface_id, "limit": 100,
    }))


def create_vm_components(config: dict, record: dict, interface_name: str = "inet", mac: str | None = None) -> None:
    """Create the NetBox disk, interface and primary MAC before local provisioning."""
    vm_id = record["id"]
    disks = vm_disks(config, vm_id)
    if not disks:
        _request(config, "POST", "virtualization/virtual-disks/", {
            "virtual_machine": vm_id, "name": record["name"], "size": int(record["disk"]),
        })
    elif len(disks) != 1 or disks[0]["name"] != record["name"] or int(disks[0]["size"]) != int(record["disk"]):
        raise NetBoxError(f"NetBox disks for {record['name']} differ from the VM request")
    interfaces = vm_interfaces(config, vm_id)
    if not interfaces:
        interface = _request(config, "POST", "virtualization/interfaces/", {
            "virtual_machine": vm_id, "name": interface_name, "enabled": True,
        })
    elif len(interfaces) == 1 and interfaces[0]["name"] == interface_name:
        interface = interfaces[0]
    else:
        raise NetBoxError(f"NetBox interfaces for {record['name']} differ from the VM request")
    primary = interface.get("primary_mac_address")
    if not primary:
        if mac is None:
            suffix = secrets.token_bytes(3)
            mac = "52:54:00:" + ":".join(f"{byte:02X}" for byte in suffix)
        created = _request(config, "POST", "dcim/mac-addresses/", {
            "mac_address": mac, "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": interface["id"],
        })
        _request(config, "PATCH", f"virtualization/interfaces/{interface['id']}/", {
            "primary_mac_address": created["id"],
        })
    elif mac and primary["mac_address"].lower() != mac.lower():
        raise NetBoxError(f"NetBox MAC for {record['name']} differs from the local VM")


def _list_results(config: dict, path: str) -> list[dict]:
    records = []
    while path:
        page = _request(config, "GET", path)
        records.extend(page.get("results", []))
        next_url = page.get("next")
        if not next_url:
            break
        base_url, _ = _settings(config)
        parsed = urllib.parse.urlsplit(next_url)
        base = urllib.parse.urlsplit(base_url)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or not parsed.path.startswith("/api/"):
            raise NetBoxError("NetBox pagination returned an unexpected URL")
        path = parsed.path.removeprefix("/api/") + ("?" + parsed.query if parsed.query else "")
    return records


def import_vm(config: dict, vm: dict, cluster_id: int, device_id: int) -> dict:
    """Document a pre-existing local VM without changing libvirt."""
    if len(vm["disk_paths"]) != 1 or not vm.get("mac_address"):
        raise NetBoxError(f"VM {vm['name']} needs one disk and one interface with MAC for adoption")
    payload = {
        "name": vm["name"], "cluster": cluster_id, "device": device_id,
        "status": vm["status"], "vcpus": vm["vcpus"],
        "memory": vm["memory_mb"], "disk": vm["disk_mb"],
        "description": vm.get("description", ""),
        "start_on_boot": "on" if vm["autostart"] else "off",
        "local_context_data": {"vmctl": {
            "version": 2, "source": "existing", "bridge": vm.get("bridge"),
            "disk_paths": vm["disk_paths"],
            "interface_name": "inet", "display": vm.get("display"),
        }},
        "changelog_message": "Imported existing libvirt VM with vmctl",
    }
    record = _request(config, "POST", "virtualization/virtual-machines/", payload)
    create_vm_components(config, record, "inet", vm.get("mac_address"))
    return record


def reserve_vm(config: dict, vm: dict, cluster_id: int, device_id: int) -> dict:
    existing = find_vm(config, vm["name"], cluster_id)
    if existing:
        raise NetBoxError(f"VM {vm['name']!r} already exists in NetBox")
    bridge = config.get("network", {}).get("bridge")
    storage_directory = config.get("storage", {}).get("directory")
    if not isinstance(bridge, str) or not bridge:
        raise NetBoxError("config needs network.bridge")
    if not isinstance(storage_directory, str) or not storage_directory.startswith("/"):
        raise NetBoxError("config needs an absolute storage.directory")
    payload = {
        "name": vm["name"],
        "cluster": cluster_id,
        "device": device_id,
        "status": "planned",
        "start_on_boot": "off",
        "vcpus": vm["vcpus"],
        "memory": vm["memory_mb"],
        "disk": vm["disk_gb"] * 1024,
        "description": vm.get("description", ""),
        "local_context_data": {
            "vmctl": {
                "version": 2,
                "source": vm["source"],
                "iso": vm.get("iso"),
                "image": vm.get("image"),
                "user_data": vm.get("user_data"),
                "bridge": bridge,
                "storage_directory": storage_directory,
                "interface_name": vm.get("interface_name", "inet"),
                "display": {"type": "vnc", "listen": "127.0.0.1", "port": "auto"},
            }
        },
    }
    result = _request(config, "POST", "virtualization/virtual-machines/", payload)
    print(f"Planned in NetBox: {vm['name']} (ID {result['id']})")
    return result


def get_vm(config: dict, name: str, cluster_id: int, device_id: int) -> dict:
    match = find_vm(config, name, cluster_id)
    if match is None:
        raise NetBoxError(f"VM {name!r} does not exist in NetBox on this cluster")
    result = _request(config, "GET", f"virtualization/virtual-machines/{match['id']}/")
    device = result.get("device")
    if not isinstance(device, dict) or device.get("id") != device_id:
        raise NetBoxError(f"VM {name!r} belongs to another host device")
    return result


def patch_vm(config: dict, vm_id: int, changes: dict) -> dict:
    return _request(config, "PATCH", f"virtualization/virtual-machines/{vm_id}/", changes)


def local_spec_from_netbox(record: dict, config: dict | None = None) -> dict:
    try:
        memory = int(record["memory"])
        vcpus_value = Decimal(str(record["vcpus"]))
        vcpus = int(vcpus_value)
        disk_mb = int(record["disk"])
        name = record["name"]
        context = record["local_context_data"]["vmctl"]
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise NetBoxError("NetBox VM lacks name, vCPUs, memory or disk") from error
    if disk_mb <= 0 or disk_mb % 1024 or memory <= 0 or vcpus <= 0 or vcpus_value != vcpus:
        raise NetBoxError("NetBox VM sizing must be positive and disk must be whole GiB")
    if not isinstance(context, dict) or context.get("version") not in (1, 2):
        raise NetBoxError("NetBox VM has no supported vmctl context")
    result = {
        "name": name,
        "vcpus": vcpus,
        "memory_mb": memory,
        "disk_gb": disk_mb // 1024,
        "source": context.get("source"),
        "iso": context.get("iso"),
        "image": context.get("image"),
        "user_data": context.get("user_data"),
        "bridge": context.get("bridge"),
        "storage_directory": context.get("storage_directory"),
    }
    if context["version"] == 2:
        if config is None:
            raise NetBoxError("NetBox VM inventory needs a NetBox connection")
        interfaces = vm_interfaces(config, record["id"])
        disks = vm_disks(config, record["id"])
        name = context.get("interface_name", "inet")
        if len(interfaces) != 1 or interfaces[0]["name"] != name:
            raise NetBoxError(f"NetBox VM {record['name']} needs exactly one interface named {name!r}")
        primary_mac = interfaces[0].get("primary_mac_address")
        if not isinstance(primary_mac, dict) or not primary_mac.get("mac_address"):
            raise NetBoxError(f"NetBox interface {name!r} needs a primary MAC address")
        if len(disks) != 1 or disks[0]["name"] != record["name"] or int(disks[0]["size"]) != disk_mb:
            raise NetBoxError(f"NetBox VM {record['name']} needs one matching virtual disk")
        ip_ids = {item["id"] for item in interface_ips(config, interfaces[0]["id"])}
        for family in (4, 6):
            primary_ip = record.get(f"primary_ip{family}")
            if primary_ip and primary_ip["id"] not in ip_ids:
                raise NetBoxError(f"NetBox primary IPv{family} is not assigned to VM interface {name!r}")
        result.update({
            "description": record.get("description") or "",
            "interface_name": name,
            "mac_address": primary_mac["mac_address"],
            "display": context.get("display"),
        })
    return result
