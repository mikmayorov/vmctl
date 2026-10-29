"""Creation diagnostics: selected inputs and generated settings, never secrets."""

import json


def _id(value):
    return value.get("id") if isinstance(value, dict) else value


def context_matches(context: dict, record: dict) -> bool | None:
    """Confirm only selectors resolvable from this object's API representation.

    None means incomplete metadata (e.g. ancestry), never a guessed match.
    """
    uncertain = False
    cluster = record.get("cluster") or {}
    direct = {"clusters": record.get("cluster"), "sites": record.get("site"),
              "tenants": record.get("tenant"), "device_types": record.get("device_type")}
    for field, value in direct.items():
        choices = context.get(field) or []
        if choices and _id(value) not in {_id(item) for item in choices}:
            return False
    for field, key in (("cluster_types", "type"), ("cluster_groups", "group")):
        choices = context.get(field) or []
        if choices:
            if not cluster:
                return False
            if not isinstance(cluster, dict) or key not in cluster:
                uncertain = True
            elif _id(cluster[key]) not in {_id(item) for item in choices}:
                return False
    choices = context.get("tags") or []
    if choices and not ({_id(item) for item in choices} & {_id(item) for item in record.get("tags", [])}):
        return False
    # NetBox also matches parent roles/platforms/regions/groups/locations.
    for field, value in (("roles", record.get("role")), ("platforms", record.get("platform")),
                         ("locations", record.get("location")), ("regions", None),
                         ("site_groups", None), ("tenant_groups", None)):
        choices = context.get(field) or []
        if choices and _id(value) not in {_id(item) for item in choices}:
            # Virtual machines cannot match Device-only location selectors.
            if field == "locations" and "device" in record:
                return False
            uncertain = True
    return None if uncertain else True


def context_names(config: dict, record: dict, host: dict) -> dict:
    """Best-effort names; rendered Context remains the sole source of values."""
    from netbox import NetBoxError, _list_results

    try:
        contexts = _list_results(config, "extras/config-contexts/?is_active=true&limit=100")
    except NetBoxError:
        return {"available": False, "matched": [], "unresolved": []}
    matched, unresolved = [], []
    targets = [("VM", record)] + ([("Device", host)] if host else [])
    for context in sorted(contexts, key=lambda item: (item.get("weight", 1000), item.get("name", ""))):
        if not context.get("is_active", True):
            continue
        cloud = ((context.get("data") or {}).get("vmctl") or {}).get("cloud_init")
        if not isinstance(cloud, dict) or not cloud:
            continue
        for target, obj in targets:
            match = context_matches(context, obj)
            item = {"target": target, "id": context["id"], "name": context["name"],
                    "weight": context.get("weight", 1000)}
            if match is True:
                matched.append(item)
            elif match is None:
                unresolved.append(item)
    return {"available": True, "matched": matched, "unresolved": unresolved}


def print_creation_summary(vm: dict, dry_run: bool) -> None:
    print(f"\n{'План создания' if dry_run else 'Параметры создания'} ВМ: {vm['name']}")
    print(f"  UUID: {vm.get('uuid') or 'будет назначен libvirt'}")
    print(f"  Ресурсы: {vm['vcpus']} vCPU, {vm['memory_mb']} МиБ RAM")
    print(f"  Исходный hardware-профиль: {vm.get('hardware') or 'не указан/не сохранён; используются явные ресурсы'}")
    if vm.get("description"):
        print(f"  Description: {vm['description']}")
    source = vm.get("iso") if vm["source"] == "iso" else vm.get("image")
    print(f"  Источник ОС: {vm['source']} — {source}")
    if vm["source"] == "cloud_image":
        print("  Развёртывание: отдельный qcow2; q35, UEFI без Secure Boot; CPU host-passthrough")
    for item in vm.get("disks") or [{"name": vm["name"], "size_gb": vm["disk_gb"]}]:
        print(f"  Диск {item['name']}: {item['size_gb']} ГиБ" + (f" — {item['path']}" if item.get("path") else ""))
    report = vm.get("creation_report") or {}
    for field, label in (("cluster", "Кластер NetBox"), ("device", "Device NetBox")):
        obj = report.get(field)
        if obj:
            print(f"  {label}: {obj.get('name') or obj.get('display') or '—'} (ID {obj['id']})")
    if report.get("contexts"):
        print("  Источники cloud-init, в порядке переопределения:")
        for layer in report["contexts"]:
            print(f"    {layer['label']}: {', '.join(layer['fields'])}")
    if report.get("ignored_fields"):
        print("  Неиспользуемые поля cloud_init: " + ", ".join(report["ignored_fields"]))
    names = report.get("context_names")
    if names is not None:
        if not names["available"]:
            print("  Имена Config Contexts: недоступны API; для просмотра нужен view extras.configcontext")
        else:
            print("  Видимые Config Contexts с подтверждёнными назначениями (список зависит от прав API):")
            for item in names["matched"]:
                print(f"    {item['target']}: {item['name']} (ID {item['id']}, weight {item['weight']})")
            if not names["matched"]:
                print("    нет подтверждённых именованных контекстов с настройками vmctl")
            for item in names["unresolved"]:
                print(f"    {item['target']}: {item['name']} — назначение не подтверждено, недостаточно данных о селекторах")
    data = vm.get("cloud_init_data") or {}
    network = json.loads(data["network-config"]) if data.get("network-config") else {}
    for index, interface in enumerate(vm.get("interfaces") or [{"name": vm.get("interface_name") or "inet",
                                                               "bridge": vm["bridge"], "mac_address": vm.get("mac_address")}]):
        name = interface["name"]
        print(f"  NIC {name}: MAC {interface.get('mac_address') or 'назначит libvirt'}, мост {interface['bridge']}"
              + (f", TAP {interface['host_dev']}" if interface.get("host_dev") else ""))
        entry = network.get("ethernets", {}).get(f"nic{index}")
        if entry is None:
            continue
        print(f"    IP: {', '.join(entry['addresses'])}; DHCPv4/DHCPv6/IPv6 RA отключены")
        sources = report.get("network_sources", {}).get(name, {})
        for family, destination in ((4, "0.0.0.0/0"), (6, "::/0")):
            gateway = next((route["via"] for route in entry.get("routes", []) if route["to"] == destination), None)
            origin = sources.get(f"gateway{family}")
            print(f"    Шлюз IPv{family}: {gateway or 'не задан'}" + (f" [{origin}]" if origin else ""))
        dns = entry.get("nameservers", {})
        for key, label, field in (("addresses", "DNS", "dns_servers"), ("search", "Search domains", "search_domains")):
            origin = sources.get(field)
            print(f"    {label}: {', '.join(dns.get(key, [])) or 'не заданы'}" + (f" [{origin}]" if origin else ""))
    if data.get("user-data", "").startswith("#cloud-config\n{"):
        user = json.loads(data["user-data"].split("\n", 1)[1])
        keys = next((item.get("ssh_authorized_keys", []) for item in user.get("users", [])
                     if isinstance(item, dict) and item.get("name") == "root"), [])
        password_auth = "отключён" if user.get("ssh_pwauth") is False else "определяется пользовательским user-data"
        print(f"  SSH root: уникальных ключей {len(keys)}; парольный вход {password_auth}")
        if ["passwd", "-d", "root"] in user.get("runcmd", []):
            print("  Консоль root: пустой пароль после финального этапа cloud-init")
        elif any(item.get("name") == "root" for item in (user.get("chpasswd") or {}).get("users", [])):
            print("  Консоль root: заданный пароль будет установлен cloud-init; значение скрыто")
        if report.get("root_password"):
            print(f"    Источник политики пароля: {report['root_password']['source']}")
        print("  Шаблоны NoCloud: user-data, meta-data, network-config")
        print("  При первом запуске: hostname, статическая сеть, ключи root, расширение корневой ФС")
    elif vm["source"] == "cloud_image":
        print(f"  Cloud-init user-data: {vm.get('user_data') or 'передан отдельно'}; содержимое не выводится")
    display = vm.get("display") or {"type": "vnc", "listen": "127.0.0.1", "port": "auto"}
    print(f"  Дисплей: {display['type']}" + (f" {display.get('listen')}:{display.get('port')}" if display['type'] != "none" else ""))
    if vm["source"] == "cloud_image":
        print("  После создания: ВМ выключена; настройки внутри гостя выполняются при его запуске.\n")
    else:
        print("  После создания: ВМ выключена; установка ОС запускается отдельно.\n")
