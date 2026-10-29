"""Build NoCloud input from NetBox inventory and host bridge routes."""

import ipaddress
import json
import subprocess

from netbox import NetBoxError, _request, root_ssh_authorized_keys


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
    settings = merge_context({}, (((record.get("config_context") or {}).get("vmctl") or {}).get("cloud_init") or {}))
    device = record.get("device") or {}
    if device.get("id"):
        host = _request(config, "GET", f"dcim/devices/{device['id']}/")
        for field in ("config_context", "local_context_data"):
            settings = merge_context(settings, ((host.get(field) or {}).get("vmctl") or {}).get("cloud_init") or {})
    settings = merge_context(settings, ((record.get("local_context_data") or {}).get("vmctl") or {}).get("cloud_init") or {})
    settings["root_ssh_authorized_keys"] = root_ssh_authorized_keys(config, record)
    return cloud_init_data(vm, settings)


def cloud_init_data(vm: dict, settings: dict) -> dict:
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
                continue
            field = f"gateway{family}"
            gateway = options.get(field)
            if field not in options and family not in defaults and family not in explicit:
                gateway = bridge_gateway(interface["bridge"], addresses, family)
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
        if not isinstance(dns, list) or not isinstance(search, list) or not all(isinstance(value, str) for value in search):
            raise NetBoxError("cloud-init dns_servers and search_domains must be lists")
        if dns or search:
            entry["nameservers"] = {"addresses": [str(ipaddress.ip_address(value)) for value in dns], "search": search}
        ethernets[f"nic{index}"] = entry
    if not ethernets:
        raise NetBoxError("cloud-init needs NetBox VM interfaces")
    return {
        "user-data": "#cloud-config\n" + json.dumps({
            "disable_root": False, "ssh_pwauth": False,
            "users": [{"name": "root", "lock_passwd": True, "ssh_authorized_keys": keys}],
            "growpart": {"mode": "auto", "devices": ["/"], "ignore_growroot_disabled": False},
            "resize_rootfs": True,
        }, ensure_ascii=False, indent=2) + "\n",
        "meta-data": json.dumps({"instance-id": vm["uuid"], "local-hostname": vm["name"]}) + "\n",
        "network-config": json.dumps({"version": 2, "ethernets": ethernets}, indent=2) + "\n",
    }
