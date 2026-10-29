"""Build NoCloud input from NetBox inventory and host bridge routes."""

import ipaddress
import json
import subprocess

from netbox import NetBoxError, _request, root_ssh_authorized_keys
from creation_report import context_names


def merge_context(base: dict, override: dict) -> dict:
    if not isinstance(base, dict) or not isinstance(override, dict):
        raise NetBoxError("vmctl.cloud_init must be an object")
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_context(result[key], value)
        else:
            result[key] = value
    return result


def bridge_gateway(bridge: str, addresses: list, family: int) -> str | None:
    routes = json.loads(subprocess.run(
        ["ip", "-j", f"-{family}", "route", "show", "default", "dev", bridge],
        check=True, capture_output=True, text=True,
    ).stdout)
    gateways = set()
    for route in routes:
        if not route.get("gateway"):
            continue
        gateway = ipaddress.ip_address(route["gateway"])
        if gateway.version == family and ((family == 6 and gateway.is_link_local) or any(
                gateway in address.network for address in addresses if address.version == family)):
            gateways.add(str(gateway))
    if len(gateways) > 1:
        raise NetBoxError(f"ambiguous IPv{family} gateway on {bridge}; set cloud_init.network explicitly")
    return next(iter(gateways), None)


def cloud_init_from_netbox(config: dict, record: dict, vm: dict) -> dict:
    settings, origins = {}, {}
    report = vm.setdefault("creation_report", {})
    report["contexts"] = []

    def apply(data: dict, label: str) -> None:
        nonlocal settings
        context = (data or {}).get("vmctl") or {}
        incoming = merge_context({}, context.get("cloud_init") or {})
        if "root_password" in context:
            raise NetBoxError(f"move vmctl.root_password to vmctl.cloud_init.root_password in {label}")
        settings = merge_context(settings, incoming)
        if incoming:
            report["contexts"].append({"label": label, "fields": sorted(incoming)})
            remember_sources(incoming, label, origins)

    apply(record.get("config_context"), "итоговый Context VM (общие назначения NetBox и локальные значения)")
    device = record.get("device") or {}
    host = {}
    if device.get("id"):
        host = _request(config, "GET", f"dcim/devices/{device['id']}/")
        apply(host.get("config_context"), "итоговый Context Device")
        apply(host.get("local_context_data"), "локальный Context Device")
    apply(record.get("local_context_data"), "локальный Context VM")
    settings["root_ssh_authorized_keys"] = root_ssh_authorized_keys(config, record)
    report["ignored_fields"] = sorted(set(settings) - {
        "root_ssh_authorized_keys", "root_password", "network", "dns_servers", "search_domains"})
    data = cloud_init_data(vm, settings, origins=origins, report=report)
    report["context_names"] = context_names(config, record, host)
    return data


def remember_sources(data: dict, label: str, origins: dict, prefix: tuple = ()) -> None:
    for key, value in data.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            remember_sources(value, label, origins, path)
        else:
            for existing in list(origins):
                if existing[:len(path)] == path:
                    del origins[existing]
            origins[path] = label


def cloud_init_data(vm: dict, settings: dict, *, origins: dict | None = None,
                    report: dict | None = None) -> dict:
    origins = origins or {}
    root_password = settings.get("root_password", "")
    if not isinstance(root_password, str) or any(char in root_password for char in ("\n", "\r", "\0")):
        raise NetBoxError("vmctl.cloud_init.root_password must be a string without newline or NUL; use an empty string for no password")
    if report is not None:
        report["root_password"] = {"configured": root_password != "",
                                   "source": origins.get(("root_password",), "по умолчанию")}
    keys = settings.get("root_ssh_authorized_keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key.strip() for key in keys):
        raise NetBoxError("cloud-init needs at least one root SSH public key from coreroot Contacts or VM Context")
    network_settings = settings.get("network") or {}
    if not isinstance(network_settings, dict):
        raise NetBoxError("cloud_init.network must be an object")
    ethernets = {}
    defaults = set()
    explicit = {family for options in network_settings.values() if isinstance(options, dict)
                for family in (4, 6) if options.get(f"gateway{family}")}
    for index, interface in enumerate(vm.get("interfaces", [])):
        name = interface["name"]
        options = network_settings.get(name, network_settings.get(interface["bridge"], {}))
        options_key = name if name in network_settings else interface["bridge"]
        sources = {}
        if report is not None:
            report.setdefault("network_sources", {})[name] = sources
        if not isinstance(options, dict):
            raise NetBoxError(f"cloud_init.network.{name} must be an object")
        addresses = [ipaddress.ip_interface(value) for value in interface.get("addresses", [])]
        if not addresses:
            raise NetBoxError(f"NetBox interface {name!r} needs static IP addresses before cloud-init creation")
        if not interface.get("mac_address"):
            raise NetBoxError(f"NetBox interface {name!r} needs a primary MAC address")
        entry = {"match": {"macaddress": interface["mac_address"].lower()},
                 "dhcp4": False, "dhcp6": False, "accept-ra": False,
                 "addresses": [str(value) for value in addresses]}
        routes = []
        for family in (4, 6):
            if not any(address.version == family for address in addresses):
                sources[f"gateway{family}"] = f"нет IPv{family}-адресов на этом NIC"
                continue
            field = f"gateway{family}"
            gateway = options.get(field)
            if field in options:
                source = origins.get(("network", options_key, field), "Context")
                sources[field] = f"{source}: network.{options_key}.{field}"
                if not gateway:
                    sources[field] += "; автопоиск отключён"
            if field not in options and family not in defaults and family not in explicit:
                gateway = bridge_gateway(interface["bridge"], addresses, family)
                sources[field] = f"маршруты хоста через {interface['bridge']}"
            elif field not in options:
                sources[field] = "автопоиск пропущен: шлюз задан для другой записи network"
            if not gateway:
                continue
            address = ipaddress.ip_address(gateway)
            if address.version != family or not ((family == 6 and address.is_link_local) or any(
                    address in item.network for item in addresses if item.version == family)):
                raise NetBoxError(f"IPv{family} gateway {gateway} is outside interface {name!r} subnet")
            if family in defaults:
                raise NetBoxError(f"multiple IPv{family} default gateways; select one interface in cloud_init.network")
            defaults.add(family)
            routes.append({"to": "0.0.0.0/0" if family == 4 else "::/0", "via": str(address)})
        if routes:
            entry["routes"] = routes
        dns = options.get("dns_servers", settings.get("dns_servers", []))
        search = options.get("search_domains", settings.get("search_domains", []))
        for field in ("dns_servers", "search_domains"):
            path = ("network", options_key, field) if field in options else (field,)
            sources[field] = origins.get(path, "не задано в Context") + ": " + ".".join(path)
        if not isinstance(dns, list) or not isinstance(search, list) or not all(isinstance(value, str) for value in search):
            raise NetBoxError("cloud-init dns_servers and search_domains must be lists")
        if dns or search:
            entry["nameservers"] = {"addresses": [str(ipaddress.ip_address(value)) for value in dns], "search": search}
        ethernets[f"nic{index}"] = entry
    if not ethernets:
        raise NetBoxError("cloud-init needs NetBox VM interfaces")
    user_data = {
        "disable_root": False, "ssh_pwauth": False,
        "users": [{"name": "root", "lock_passwd": True, "ssh_authorized_keys": keys}],
        "growpart": {"mode": "auto", "devices": ["/"], "ignore_growroot_disabled": False},
        "resize_rootfs": True,
    }
    if root_password:
        # Structured data preserves literal characters; no password in shell commands.
        user_data["chpasswd"] = {"expire": False, "users": [
            {"name": "root", "password": root_password, "type": "text"}]}
    else:
        user_data["runcmd"] = [["passwd", "-d", "root"]]
    return {
        "user-data": "#cloud-config\n" + json.dumps(user_data, ensure_ascii=False, indent=2) + "\n",
        "meta-data": json.dumps({"instance-id": vm["uuid"], "local-hostname": vm["name"]}) + "\n",
        "network-config": json.dumps({"version": 2, "ethernets": ethernets}, indent=2) + "\n",
    }
