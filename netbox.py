"""Read desired VM state from NetBox and write it before local changes."""

import json
import os
import secrets
import re
from pathlib import Path
from decimal import Decimal, InvalidOperation
import urllib.error
import urllib.parse
import urllib.request
import uuid

from vmcreate import host_interface_name


class NetBoxError(Exception):
    pass


def vm_serial_uuid(record: dict) -> str | None:
    """Read a VM's libvirt UUID from the NetBox Serial field."""
    serial = record.get("serial")
    if not serial:
        return None
    try:
        canonical = str(uuid.UUID(serial))
    except (TypeError, ValueError, AttributeError) as error:
        raise NetBoxError(f"NetBox VM {record.get('name')!r} Serial is not a UUID") from error
    if serial != canonical:
        raise NetBoxError(f"NetBox VM {record.get('name')!r} Serial must be a canonical UUID: {canonical}")
    return canonical


KEY_FILE = Path(__file__).with_name("netbox.key")
COMPONENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


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
            body = response.read()
            return json.loads(body) if body else {}
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


def interface_macs(config: dict, interface_id: int) -> list[dict]:
    return _list_results(config, "dcim/mac-addresses/?" + urllib.parse.urlencode({
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": interface_id, "limit": 100,
    }))


def add_component(config: dict, kind: str, payload: dict) -> dict:
    paths = {"disk": "virtualization/virtual-disks/", "nic": "virtualization/interfaces/"}
    return _request(config, "POST", paths[kind], payload)


def remove_component(config: dict, kind: str, component_id: int) -> None:
    paths = {"disk": "virtualization/virtual-disks/", "nic": "virtualization/interfaces/",
             "mac": "dcim/mac-addresses/"}
    _request(config, "DELETE", f"{paths[kind]}{component_id}/")


def ensure_primary_mac(config: dict, interface: dict, mac: str | None = None) -> str:
    primary = interface.get("primary_mac_address")
    if primary:
        address = primary["mac_address"]
        if mac and address.lower() != mac.lower():
            raise NetBoxError(f"NetBox MAC for interface {interface['name']} differs")
        return address
    assigned = interface_macs(config, interface["id"])
    if len(assigned) > 1:
        raise NetBoxError(f"interface {interface['name']} has multiple MAC objects; choose one primary in NetBox")
    if assigned:
        address = assigned[0]["mac_address"]
        if mac and address.lower() != mac.lower():
            raise NetBoxError(f"assigned MAC for interface {interface['name']} differs")
        _request(config, "PATCH", f"virtualization/interfaces/{interface['id']}/", {
            "primary_mac_address": assigned[0]["id"],
        })
        return address
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
    return mac


def create_vm_components(config: dict, record: dict, interface_name: str = "inet", mac: str | None = None,
                         root_size_mb: int | None = None) -> None:
    """Create the NetBox disk, interface and primary MAC before local provisioning."""
    vm_id = record["id"]
    disks = vm_disks(config, vm_id)
    if not disks:
        directory = config.get("storage", {}).get("directory")
        if not directory:
            raise NetBoxError("config needs storage.directory for the VM disk path")
        _request(config, "POST", "virtualization/virtual-disks/", {
            "virtual_machine": vm_id, "name": record["name"], "size": root_size_mb or int(record["disk"]),
            "description": str(Path(directory) / f"{record['name']}.qcow2"),
        })
    elif not any(item["name"] == record["name"] and (root_size_mb is None or int(item["size"]) == root_size_mb)
                 for item in disks):
        raise NetBoxError(f"NetBox disks for {record['name']} differ from the VM request")
    context = (record.get("local_context_data") or {}).get("vmctl") or {}
    if context.get("source") == "iso":
        iso = context.get("iso")
        if not isinstance(iso, str) or not Path(iso).is_absolute():
            raise NetBoxError(f"VM {record['name']}: ISO path is missing")
        try:
            size = (Path(iso).stat().st_size + 1024**2 - 1) // 1024**2
        except OSError as error:
            raise NetBoxError(f"VM {record['name']}: cannot read ISO size: {error}") from error
        if size <= 0:
            raise NetBoxError(f"VM {record['name']}: ISO is empty")
        media = next((item for item in disks if item["name"] == "media-sda"), None)
        if media is None:
            _request(config, "POST", "virtualization/virtual-disks/", {
                "virtual_machine": vm_id, "name": "media-sda", "size": size, "description": iso,
            })
        elif int(media["size"]) != size or media.get("description") != iso:
            raise NetBoxError(f"VM {record['name']}: ISO Virtual Disk differs from the preparation request")
    tap_name = host_interface_name(record["name"], interface_name)
    interfaces = vm_interfaces(config, vm_id)
    if not interfaces:
        interface = _request(config, "POST", "virtualization/interfaces/", {
            "virtual_machine": vm_id, "name": tap_name, "enabled": True,
        })
    else:
        interface = next((item for item in interfaces if item["name"] in (tap_name, interface_name)), None)
        if interface is None:
            raise NetBoxError(f"NetBox interface {tap_name!r} is missing for {record['name']}")
    ensure_primary_mac(config, interface, mac)


def normalize_vm_interface_names(config: dict, record: dict, vm: dict, dry_run: bool) -> bool:
    """Rename existing VM interfaces to host TAP names without adopting other VM fields."""
    interfaces = vm_interfaces(config, record["id"])
    observed = vm.get("interfaces", [])
    if len(interfaces) != len(observed):
        raise NetBoxError(f"VM {vm['name']}: interface counts differ; inspect before normalizing names")
    used = set()
    plan = []
    for index, local in enumerate(observed):
        mac = (local.get("mac_address") or "").lower()
        match = next((item for item in interfaces if item["id"] not in used and
                      (item.get("primary_mac_address") or {}).get("mac_address", "").lower() == mac), None)
        if match is None:
            raise NetBoxError(f"VM {vm['name']}: no NetBox interface with guest MAC {mac}")
        used.add(match["id"])
        host_dev = local.get("host_dev")
        seed = host_dev if isinstance(host_dev, str) and host_dev.startswith("vm-") else (
            local.get("name") or ("inet" if index == 0 else f"net-{index + 1}"))
        target = host_interface_name(vm["name"], seed)
        plan.append((match, target, local))
    if len({target for _, target, _ in plan}) != len(plan):
        raise NetBoxError(f"VM {vm['name']}: duplicate host TAP interface names")
    renames = [(item, target) for item, target, _ in plan if item["name"] != target]
    data = dict(record.get("local_context_data") or {})
    context = dict(data.get("vmctl") or {})
    bridges = dict(context.get("interface_bridges") or {})
    new_bridges = dict(bridges)
    for item, target, _ in plan:
        if item["name"] != target and item["name"] in new_bridges:
            new_bridges[target] = new_bridges.pop(item["name"])
    new_context = dict(context)
    if new_bridges != bridges:
        new_context["interface_bridges"] = new_bridges
    if context.get("interface_name") in {item["name"] for item, _, _ in plan}:
        new_context["interface_name"] = next(target for item, target, _ in plan
                                             if item["name"] == context["interface_name"])
    if not renames and new_context == context:
        print(f"UNCHANGED {vm['name']} interfaces")
        return False
    print(f"{'WOULD UPDATE' if dry_run else 'UPDATED'} {vm['name']} interfaces: {len(renames)} rename(s)")
    for item, target in renames:
        print(f"  RENAME VM Interface {item['name']} → {target} (IP and MAC stay assigned)")
    if dry_run:
        return True
    if new_context != context:
        patch_vm(config, record["id"], {
            "local_context_data": {**data, "vmctl": new_context},
            "changelog_message": "vmctl normalized host TAP interface names",
        })
    for item, target in renames:
        _request(config, "PATCH", f"virtualization/interfaces/{item['id']}/", {"name": target})
    return True


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


def named_media(vm: dict) -> list[dict]:
    result = []
    for index, item in enumerate(vm.get("mounted_media", [])):
        target = item.get("target")
        name = f"media-{target}" if target else f"media-{index + 1}"
        if not COMPONENT_NAME.fullmatch(name) or not item.get("path") or not item.get("size_mb"):
            raise NetBoxError(f"VM {vm['name']}: mounted ISO/media needs path, target and readable size")
        result.append({**item, "name": name})
    if len({item["name"] for item in result}) != len(result):
        raise NetBoxError(f"VM {vm['name']}: duplicate mounted media targets")
    return result


def import_vm(config: dict, vm: dict, cluster_id: int, device_id: int) -> dict:
    """Document a pre-existing local VM without changing libvirt."""
    validate_import_vm(vm)
    local_disks = vm.get("disks") or [{"path": path, "size_gb": vm["disk_mb"] // 1024}
                                       for path in vm["disk_paths"]]
    local_interfaces = vm["interfaces"] if "interfaces" in vm else [{"name": "inet", "bridge": vm.get("bridge"),
                                                                       "mac_address": vm.get("mac_address")}]
    serial = str(uuid.UUID(vm["uuid"]))
    named_disks = [{**item, "name": vm["name"] if index == 0 else f"disk-{index + 1}"}
                   for index, item in enumerate(local_disks)]
    media_disks = named_media(vm)
    named_interfaces = [{**item, "name": host_interface_name(vm["name"],
                         item.get("host_dev") if str(item.get("host_dev") or "").startswith("vm-") else
                         item.get("name") or ("inet" if index == 0 else f"net-{index + 1}"))}
                        for index, item in enumerate(local_interfaces)]
    payload = {
        "name": vm["name"], "serial": serial, "cluster": cluster_id, "device": device_id,
        "status": vm["status"], "vcpus": vm["vcpus"],
        "memory": vm["memory_mb"], "disk": vm["disk_mb"] + sum(item["size_mb"] for item in media_disks),
        "description": vm.get("description", ""),
        "start_on_boot": "on" if vm["autostart"] else "off",
        "local_context_data": {"vmctl": {
            "version": 3, "source": "existing", "bridge": vm.get("bridge"),
            "mounted_media": [{"name": item["name"], "target": item["target"]} for item in media_disks],
            "interface_bridges": {item["name"]: item["bridge"] for item in named_interfaces},
            "interface_name": named_interfaces[0]["name"] if named_interfaces else None,
            "display": vm.get("display"),
        }},
        "changelog_message": "Imported existing libvirt VM with vmctl",
    }
    record = _request(config, "POST", "virtualization/virtual-machines/", payload)
    for item in named_disks:
        add_component(config, "disk", {"virtual_machine": record["id"],
                                       "name": item["name"], "size": item.get("size_mb") or item["size_gb"] * 1024,
                                       "description": item["path"]})
    for item in media_disks:
        add_component(config, "disk", {"virtual_machine": record["id"],
                                       "name": item["name"], "size": item["size_mb"],
                                       "description": item["path"]})
    for item in named_interfaces:
        interface = add_component(config, "nic", {"virtual_machine": record["id"],
                                                 "name": item["name"], "enabled": True})
        ensure_primary_mac(config, interface, item["mac_address"])
    return record


def validate_adopt_devices(vm: dict) -> None:
    if vm.get("unsupported_storage") or vm.get("unsupported_interfaces"):
        raise NetBoxError(f"VM {vm['name']}: unsupported libvirt storage or interface types: "
                          f"{vm.get('unsupported_storage', [])}, {vm.get('unsupported_interfaces', [])}")


def validate_import_vm(vm: dict) -> None:
    validate_adopt_devices(vm)
    named_media(vm)
    try:
        uuid.UUID(vm["uuid"])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise NetBoxError(f"VM {vm['name']} needs a readable libvirt UUID for adoption") from error
    if any(not item.get("path") or not (item.get("size_mb") or item.get("size_gb", 0) * 1024)
           for item in vm.get("disks", [])) or any(not item.get("mac_address") or not item.get("bridge")
           for item in vm.get("interfaces", [])):
        raise NetBoxError(f"VM {vm['name']} needs readable disks and bridge interface MAC addresses for adoption")


def reconcile_adopted_vm(config: dict, record: dict, vm: dict, dry_run: bool) -> bool:
    """Refresh an existing NetBox VM from libvirt; never change libvirt or IPAM."""
    validate_adopt_devices(vm)
    serial = str(uuid.UUID(vm["uuid"]))
    saved = vm_serial_uuid(record)
    if saved and saved != serial:
        raise NetBoxError(f"VM {vm['name']}: NetBox Serial {saved} differs from libvirt UUID {serial}")
    vm_id = record["id"]
    disks = sorted(vm_disks(config, vm_id), key=lambda item: item["id"])
    interfaces = sorted(vm_interfaces(config, vm_id), key=lambda item: item["id"])
    local_disks = vm.get("disks", [])
    media_disks = named_media(vm)
    local_interfaces = vm.get("interfaces", [])
    if any(not item.get("path") or not item.get("size_mb") for item in local_disks):
        raise NetBoxError(f"VM {vm['name']}: disk path or capacity is missing")
    if any(not item.get("mac_address") or not item.get("bridge") for item in local_interfaces):
        raise NetBoxError(f"VM {vm['name']}: interface MAC or bridge is missing")
    data = dict(record.get("local_context_data") or {})
    context = dict(data.get("vmctl") or {})
    old_paths = context.get("disk_paths_by_name") or {}
    if not isinstance(old_paths, dict):
        raise NetBoxError(f"VM {vm['name']}: invalid disk_paths_by_name in NetBox")
    disk_by_name = {item["name"]: item for item in disks}
    old_media_names = {item.get("name") for item in context.get("mounted_media", []) if isinstance(item, dict)}
    regular_existing = [item for item in disks if item["name"] not in old_media_names
                        and not item["name"].startswith("media-")]
    disk_plan = []
    used_disks = set()
    for index, local in enumerate(local_disks):
        match = next((item for item in regular_existing if (item.get("description") or old_paths.get(item["name"])) == local["path"]
                      and item["id"] not in used_disks), None)
        if match is None:
            candidate = vm["name"] if index == 0 else f"disk-{local['target']}"
            match = disk_by_name.get(candidate)
            if match and match["id"] in used_disks:
                match = None
        if match is None and index < len(regular_existing) and regular_existing[index]["id"] not in used_disks:
            match = regular_existing[index]
        name = match["name"] if match else (vm["name"] if index == 0 else f"disk-{local['target']}")
        if not COMPONENT_NAME.fullmatch(name):
            raise NetBoxError(f"VM {vm['name']}: invalid disk name {name!r}")
        if match:
            used_disks.add(match["id"])
        disk_plan.append((local, match, name))
    for local in media_disks:
        match = next((item for item in disks if item["id"] not in used_disks
                      and item.get("description") == local["path"]), None)
        if match is None:
            match = disk_by_name.get(local["name"])
            if match and match["id"] in used_disks:
                match = None
        name = match["name"] if match else local["name"]
        if match:
            used_disks.add(match["id"])
        disk_plan.append((local, match, name))
    stale_disks = [item for item in disks if item["id"] not in used_disks]

    nic_plan = []
    used_nics = set()
    for index, local in enumerate(local_interfaces):
        mac = local["mac_address"].lower()
        match = next((item for item in interfaces
                      if item["id"] not in used_nics and
                      (item.get("primary_mac_address") or {}).get("mac_address", "").lower() == mac), None)
        if match is None:
            name = local.get("name") or ("inet" if index == 0 else f"net-{index + 1}")
            match = next((item for item in interfaces if item["id"] not in used_nics and item["name"] == name), None)
        host_dev = local.get("host_dev")
        logical_name = (host_dev if isinstance(host_dev, str) and host_dev.startswith("vm-") else
                        local.get("name") or ("inet" if index == 0 else f"net-{index + 1}"))
        name = host_interface_name(vm["name"], logical_name)
        if not COMPONENT_NAME.fullmatch(name):
            raise NetBoxError(f"VM {vm['name']}: invalid interface name {name!r}")
        if match:
            used_nics.add(match["id"])
        nic_plan.append((local, match, name))
    stale_nics = [item for item in interfaces if item["id"] not in used_nics]
    if len({name for _, _, name in nic_plan}) != len(nic_plan):
        raise NetBoxError(f"VM {vm['name']}: duplicate host TAP interface names")
    for item in stale_nics:
        if interface_ips(config, item["id"]):
            raise NetBoxError(f"VM {vm['name']}: interface {item['name']} has IP addresses; unassign them in NetBox before adoption")

    new_context = {**context, "version": 3, "source": "existing",
                   "bridge": local_interfaces[0]["bridge"] if local_interfaces else None,
                   "interface_bridges": {name: item["bridge"] for item, _, name in nic_plan},
                   "interface_name": nic_plan[0][2] if nic_plan else None,
                   "mounted_media": [{"name": name, "target": item["target"]}
                                     for item, _, name in disk_plan[len(local_disks):]],
                   "display": vm.get("display")}
    new_context.pop("disk_paths_by_name", None)
    new_context.pop("disk_paths", None)
    changes = {"vcpus": vm["vcpus"], "memory": vm["memory_mb"],
               "description": vm.get("description", ""),
               "start_on_boot": "on" if vm["autostart"] else "off",
               "status": vm["status"],
               "local_context_data": {**data, "vmctl": new_context}}
    if not saved:
        changes["serial"] = serial
    def saved_value(key: str):
        value = record.get(key)
        if isinstance(value, dict) and key in ("status", "start_on_boot"):
            return value.get("value")
        if key == "vcpus" and value is not None:
            try:
                return Decimal(str(value))
            except InvalidOperation as error:
                raise NetBoxError(f"VM {vm['name']}: invalid NetBox vCPU value") from error
        return value

    changes = {key: value for key, value in changes.items() if saved_value(key) != value}
    disk_changes = [(local, match, name) for local, match, name in disk_plan
                    if match is None or int(match["size"]) != local["size_mb"]
                    or match.get("description") != local["path"]]
    nic_changes = [(local, match, name) for local, match, name in nic_plan
                   if match is None or match["name"] != name or
                   (match.get("primary_mac_address") or {}).get("mac_address", "").lower()
                   != local["mac_address"].lower()]
    if not (changes or disk_changes or stale_disks or nic_changes or stale_nics):
        print(f"UNCHANGED {vm['name']}")
        return False
    verb = "WOULD UPDATE" if dry_run else "UPDATED"
    print(f"{verb} {vm['name']}: fields {', '.join(changes) or '-'}, "
          f"disks {len(disk_changes)} changed/{len(stale_disks)} removed, "
          f"interfaces {len(nic_changes)} changed/{len(stale_nics)} removed, "
          f"ISO/media {len(vm.get('mounted_media', []))}")
    for local, match, name in disk_changes:
        print(f"  {'UPDATE' if match else 'ADD'} Virtual Disk {name}: "
              f"{local['size_mb']} MiB; Description {local['path']}")
    for item in stale_disks:
        print(f"  REMOVE Virtual Disk {item['name']}")
    for _, match, name in nic_changes:
        if match and match["name"] != name:
            print(f"  RENAME VM Interface {match['name']} → {name} (IP and MAC stay assigned)")
    if dry_run:
        return True
    if changes:
        patch_vm(config, vm_id, {**changes, "changelog_message": "vmctl adopted current libvirt VM state"})
    for local, match, name in disk_changes:
        if match:
            _request(config, "PATCH", f"virtualization/virtual-disks/{match['id']}/",
                     {"size": local["size_mb"], "description": local["path"]})
        else:
            add_component(config, "disk", {"virtual_machine": vm_id, "name": name,
                                           "size": local["size_mb"], "description": local["path"]})
    for item in stale_disks:
        remove_component(config, "disk", item["id"])
    for local, match, name in nic_changes:
        if match is None:
            match = add_component(config, "nic", {"virtual_machine": vm_id, "name": name, "enabled": True})
        elif match["name"] != name:
            _request(config, "PATCH", f"virtualization/interfaces/{match['id']}/", {"name": name})
        primary = match.get("primary_mac_address")
        if primary and primary.get("mac_address", "").lower() != local["mac_address"].lower():
            _request(config, "PATCH", f"dcim/mac-addresses/{primary['id']}/", {"mac_address": local["mac_address"]})
        else:
            ensure_primary_mac(config, match, local["mac_address"])
    for item in stale_nics:
        for mac in interface_macs(config, item["id"]):
            remove_component(config, "mac", mac["id"])
        remove_component(config, "nic", item["id"])
    return True


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
        "serial": str(uuid.uuid4()),
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
                "version": 3,
                "source": vm["source"],
                "iso": vm.get("iso"),
                "image": vm.get("image"),
                "user_data": vm.get("user_data"),
                "bridge": bridge,
                "storage_directory": storage_directory,
                "interface_name": host_interface_name(vm["name"], vm.get("interface_name", "inet")),
                "mounted_media": [{"name": "media-sda", "target": "sda"}] if vm["source"] == "iso" else [],
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
    if not isinstance(context, dict) or context.get("version") not in (1, 2, 3):
        raise NetBoxError("NetBox VM has no supported vmctl context")
    if disk_mb < 0 or memory <= 0 or vcpus <= 0 or vcpus_value != vcpus:
        raise NetBoxError("NetBox VM sizing must be valid")
    result = {
        "name": name,
        "uuid": vm_serial_uuid(record),
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
    if context["version"] in (2, 3):
        if config is None:
            raise NetBoxError("NetBox VM inventory needs a NetBox connection")
        interfaces = vm_interfaces(config, record["id"])
        disks = vm_disks(config, record["id"])
        if not disks or sum(int(item["size"]) for item in disks) != disk_mb:
            raise NetBoxError(f"NetBox disks for {record['name']} must match VM disk total")
        media_context = context.get("mounted_media") or []
        if not isinstance(media_context, list):
            raise NetBoxError("vmctl mounted_media must be a list")
        media_names = {item.get("name") for item in media_context if isinstance(item, dict) and item.get("name")}
        media_by_name = {item["name"]: item for item in disks if item["name"] in media_names}
        wanted_media = []
        for item in media_context:
            if not isinstance(item, dict):
                raise NetBoxError("vmctl mounted_media contains an invalid entry")
            media_disk = media_by_name.get(item.get("name"))
            if media_disk:
                path = media_disk.get("description")
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise NetBoxError(f"media Virtual Disk {media_disk['name']!r} needs an absolute path in Description")
                wanted_media.append({"name": media_disk["name"], "path": path,
                                     "target": item.get("target"), "size_mb": int(media_disk["size"])})
            elif item.get("path"):
                wanted_media.append(item)
            else:
                raise NetBoxError(f"media Virtual Disk {item.get('name')!r} is missing")
        directory = context.get("storage_directory") or config.get("storage", {}).get("directory")
        paths = context.get("disk_paths_by_name") or {}
        if not isinstance(paths, dict):
            raise NetBoxError("vmctl disk_paths_by_name must be a mapping")
        legacy_paths = context.get("disk_paths") or []
        wanted_disks = []
        for item in disks:
            if item["name"] in media_names:
                continue
            disk_name = item["name"]
            if not isinstance(disk_name, str) or not COMPONENT_NAME.fullmatch(disk_name):
                raise NetBoxError(f"invalid NetBox disk name: {disk_name!r}")
            size = int(item["size"])
            if size <= 0 or (size % 1024 and context.get("source") != "existing"):
                raise NetBoxError(f"disk {disk_name!r} must have a positive size in MiB; new disks require whole GiB")
            path = item.get("description") or paths.get(disk_name)
            if not path and disk_name == name and legacy_paths:
                path = legacy_paths[0]
            if not path:
                if not isinstance(directory, str) or not Path(directory).is_absolute():
                    raise NetBoxError("vmctl needs storage_directory for a new disk")
                filename = f"{name}.qcow2" if disk_name == name else f"{name}-{disk_name}.qcow2"
                path = str(Path(directory) / filename)
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise NetBoxError(f"invalid path for disk {disk_name!r}")
            wanted_disks.append({"name": disk_name, "size_gb": (size + 1023) // 1024,
                                 "size_mb": size, "path": path})
        if len({item["name"] for item in wanted_disks}) != len(wanted_disks) or len({item["path"] for item in wanted_disks}) != len(wanted_disks):
            raise NetBoxError("duplicate NetBox disk name or path")
        if context.get("source") in ("iso", "cloud_image") and name not in {item["name"] for item in wanted_disks}:
            raise NetBoxError("boot disk named after the VM is missing from NetBox")
        wanted_disks.sort(key=lambda item: (item["name"] != name, item["name"]))
        bridges = context.get("interface_bridges") or {}
        if not isinstance(bridges, dict):
            raise NetBoxError("vmctl interface_bridges must be a mapping")
        wanted_interfaces = []
        ip_ids = set()
        for item in interfaces:
            interface_name = item["name"]
            if not isinstance(interface_name, str) or not COMPONENT_NAME.fullmatch(interface_name):
                raise NetBoxError(f"invalid NetBox interface name: {interface_name!r}")
            primary_mac = item.get("primary_mac_address")
            if not isinstance(primary_mac, dict) or not primary_mac.get("mac_address"):
                raise NetBoxError(f"NetBox interface {interface_name!r} needs a primary MAC address")
            bridge = bridges.get(interface_name, context.get("bridge") or config.get("network", {}).get("bridge"))
            if not isinstance(bridge, str) or not bridge or Path(bridge).name != bridge:
                raise NetBoxError(f"invalid bridge for interface {interface_name!r}")
            wanted_interfaces.append({"name": interface_name, "mac_address": primary_mac["mac_address"], "bridge": bridge})
            ip_ids.update(item["id"] for item in interface_ips(config, item["id"]))
        if len({item["name"] for item in wanted_interfaces}) != len(wanted_interfaces):
            raise NetBoxError("duplicate NetBox interface name")
        wanted_interfaces.sort(key=lambda item: item["name"])
        for family in (4, 6):
            primary_ip = record.get(f"primary_ip{family}")
            if primary_ip and primary_ip["id"] not in ip_ids:
                raise NetBoxError(f"NetBox primary IPv{family} is not assigned to this VM")
        result.update({
            "description": record.get("description") or "",
            "disks": wanted_disks,
            "mounted_media": wanted_media,
            "interfaces": wanted_interfaces,
            "disk_gb": sum(item["size_mb"] for item in wanted_disks) // 1024,
            "display": context.get("display"),
        })
        if context.get("source") == "iso" and wanted_media:
            result["iso"] = wanted_media[0]["path"]
        if wanted_interfaces:
            result["interface_name"] = wanted_interfaces[0]["name"]
            result["mac_address"] = wanted_interfaces[0]["mac_address"]
    return result
