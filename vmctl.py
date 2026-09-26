#!/usr/bin/env python3
"""Управление виртуальными машинами libvirt/KVM на текущем хосте."""

import argparse
import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

from netbox import (
    NetBoxError, COMPONENT_NAME, _request, add_component, create_vm_components, ensure_primary_mac,
    find_device, find_vm, get_vm, has_netbox_key, import_vm, reconcile_adopted_vm, interface_ips, interface_macs, list_vms,
    local_spec_from_netbox, patch_vm, remove_component, reserve_vm, validate_import_vm, vm_disks, vm_interfaces,
    vm_serial_uuid,
)
from inventory import inspect_display, inspect_vm, local_names
from vmcreate import (NAME_RE, create_vm, interface_alias, pending_purge_file, purge_pending,
                      redefine_vm, validate_spec, verify_local_vm)


DEFAULT_CONFIG = Path(__file__).with_name("config.toml")


def load_config(path: Path) -> dict:
    with path.open("rb") as file:
        config = tomllib.load(file)
    host = config.get("host", {})
    if not isinstance(host, dict):
        raise ValueError(f"{path}: [host] must be a table")
    for field in ("libvirt_uri",):
        if not isinstance(host.get(field), str) or not host[field]:
            raise ValueError(f"{path}: host.{field} must be a non-empty string")
    storage = config.get("storage", {})
    if not isinstance(storage, dict) or not isinstance(storage.get("directory"), str) or not Path(storage["directory"]).is_absolute():
        raise ValueError(f"{path}: storage.directory must be an absolute path")
    network = config.get("network", {})
    if not isinstance(network, dict) or not isinstance(network.get("bridge"), str) or not network["bridge"]:
        raise ValueError(f"{path}: network.bridge must be a non-empty string")
    netbox = config.get("netbox", {})
    if not isinstance(netbox, dict):
        raise ValueError(f"{path}: [netbox] must be a table")
    if netbox.get("key") and path.stat().st_mode & 0o077:
        raise ValueError(f"{path}: protect netbox.key with chmod 600")
    return config


def _help_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action="help", help="Показать эту справку и выйти")
    parser._optionals.title = "Параметры"
    parser._positionals.title = "Аргументы"


def _overview(groups: list[tuple[str, list[tuple[str, str]]]],
              parsers: dict[str, argparse.ArgumentParser]) -> str:
    listed = [name for _, commands in groups for name, _ in commands]
    leaves = {name for name, parser in parsers.items()
              if not any(isinstance(action, argparse._SubParsersAction) for action in parser._actions)}
    if len(listed) != len(leaves) or set(listed) != leaves:
        raise ValueError("every vmctl command must appear exactly once in the help overview")
    lines = []
    for title, commands in groups:
        lines.append(title + ":")
        for name, summary in commands:
            command = parsers[name]
            parts = [name]
            for argument in command._actions:
                if argument.dest == "help":
                    continue
                label = ", ".join(argument.option_strings) if argument.option_strings else argument.metavar or argument.dest
                if argument.option_strings and argument.nargs != 0:
                    label += " " + (argument.metavar or argument.dest.upper())
                synopsis = label.split(", ")[-1]
                parts.append(synopsis if (not argument.option_strings and argument.nargs != "?")
                             or argument.required else f"[{synopsis}]")
            synopsis = " ".join(parts)
            if len(synopsis) <= 34 and len(synopsis) + len(summary) + 3 <= 80:
                lines.append(f"  {synopsis:<34} {summary}")
            else:
                lines.extend((f"  {synopsis}", f"  {'':34} {summary}"))
        lines.append("")
    lines.extend((
        "Подробности: vmctl КОМАНДА -h (например, vmctl disk add -h).",
        "При наличии ключа изменения сначала записываются в NetBox.",
    ))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmctl", add_help=False, description=__doc__,
        usage="vmctl [-h] [--config ФАЙЛ] [--dry-run] КОМАНДА [АРГУМЕНТЫ]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _help_argument(parser)
    parser._optionals.title = "Общие параметры"
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, metavar="ФАЙЛ",
                        help="Путь к config.toml хоста")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать план без изменений")
    commands = parser.add_subparsers(dest="command", required=True, metavar="КОМАНДА", help=argparse.SUPPRESS)
    parsers = {}

    def add(name: str, description: str, example: str | None = None, parent=commands,
            details: str | None = None) -> argparse.ArgumentParser:
        epilog = "\n\n".join(part for part in (details, f"Пример:\n  {example}" if example else None) if part)
        command = parent.add_parser(name, add_help=False, help=description, description=description,
                                    formatter_class=argparse.RawDescriptionHelpFormatter,
                                    epilog=epilog or None)
        _help_argument(command)
        parsers[command.prog.removeprefix("vmctl ")] = command
        return command

    add("check", "Без имени проверить хост, libvirt, пулы и сети; с именем показать состояние и параметры ВМ.",
        "vmctl check guest", details="Для проверки всего хоста выполните vmctl check без имени.").add_argument(
            "vm", nargs="?", metavar="ИМЯ", help="Имя локальной ВМ; без имени проверяется хост")
    add("audit", "Сверить все локальные ВМ с NetBox; вывести расхождения, ничего не меняя. Нужен API-ключ.",
        "vmctl audit")
    add("list", "Показать таблицу локальных ВМ: состояние, ресурсы, автозапуск, IP из NetBox и URL дисплея.", "vmctl list")

    prepare = add("prepare", "Создать запись ВМ, диск, интерфейс и MAC только в NetBox. Локальная ВМ появится после sync; нужен API-ключ.",
                  "vmctl prepare local/guest.toml",
                  details="После prepare можно назначить IP в NetBox IPAM, настроить дисплей и изменить параметры ВМ. Затем выполните vmctl sync ИМЯ.")
    prepare.add_argument("spec", type=Path, metavar="ФАЙЛ", help="TOML-файл с первоначальными параметрами ВМ")
    create = add("create", "При наличии ключа выполнить prepare и первый sync за один вызов: сначала запись и компоненты в NetBox, затем локальная ВМ из этой записи. Между этапами нет паузы для правок в NetBox. Без ключа создаёт ВМ только локально; ВМ не запускает.",
                 "vmctl create local/guest.toml",
                 details="Если нужно назначить IP или изменить дисплей до создания локальной ВМ, используйте prepare, затем правки в NetBox и sync.")
    create.add_argument("spec", type=Path, metavar="ФАЙЛ", help="TOML-файл с первоначальными параметрами ВМ")
    sync = add("sync", "Применить записи NetBox к одной или всем ВМ этого хоста. XML работающей ВМ вступит в силу после выключения и запуска.",
               "vmctl --dry-run sync",
               details="Без имени обработать все ВМ устройства этого хоста; с именем — одну. Ошибка одной ВМ не останавливает остальные. IP в гостевой ОС команда не настраивает.")
    sync.add_argument("vm", nargs="?", metavar="ИМЯ", help="Одна ВМ; без имени все ВМ устройства хоста в NetBox")
    sync.add_argument("--purge", action="store_true",
                      help="Удалить файлы управляемых дисков, уже исключённых из NetBox (без имени — у всех ВМ)")
    adopt_cmd = add("adopt", "Перенести фактические параметры локальных ВМ в NetBox; без имени обработать все. Libvirt не меняется.",
                    "vmctl --dry-run adopt guest")
    adopt_cmd.add_argument("vm", nargs="?", metavar="ИМЯ", help="Одна локальная ВМ; без имени все ВМ хоста")

    disk = add("disk", "Добавить или удалить диск выключенной ВМ. При наличии ключа сначала меняет NetBox.",
               "vmctl disk add guest data --size-gb 20")
    disk_actions = disk.add_subparsers(dest="action", required=True, title="Действия", metavar="{add,remove}")
    disk_add = add("add", "Добавить виртуальный диск указанного размера к выключенной ВМ.",
                   "vmctl disk add guest data --size-gb 20", disk_actions)
    disk_add.add_argument("vm", metavar="ВМ", help="Имя выключенной ВМ")
    disk_add.add_argument("name", metavar="ДИСК", help="Имя нового диска в NetBox и vmctl")
    disk_add.add_argument("--size-gb", type=int, required=True, metavar="ГиБ",
                          help="Положительный целый размер нового диска в ГиБ")
    disk_remove = add("remove", "Отключить дополнительный диск выключенной ВМ; загрузочный и последний диск удалить нельзя.",
                      "vmctl disk remove guest data --purge", disk_actions,
                      details="Без --purge файл диска сохраняется. С --purge удаляется только управляемый файл.")
    disk_remove.add_argument("vm", metavar="ВМ", help="Имя выключенной ВМ")
    disk_remove.add_argument("name", metavar="ДИСК", help="Имя существующего дополнительного диска")
    disk_remove.add_argument("--purge", action="store_true",
                             help="Также удалить файл управляемого диска; без флага файл останется")

    nic = add("nic", "Добавить или удалить интерфейс выключенной ВМ. IP-адреса назначают вручную в NetBox IPAM.",
              "vmctl nic add guest backup --bridge br1")
    nic_actions = nic.add_subparsers(dest="action", required=True, title="Действия", metavar="{add,remove}")
    nic_add = add("add", "Добавить интерфейс и primary MAC к выключенной ВМ.",
                  "vmctl nic add guest backup --bridge br1", nic_actions)
    nic_add.add_argument("vm", metavar="ВМ", help="Имя выключенной ВМ")
    nic_add.add_argument("name", metavar="ИНТЕРФЕЙС", help="Имя нового интерфейса в NetBox")
    nic_add.add_argument("--bridge", metavar="МОСТ",
                         help="Существующий мост хоста; без флага берётся мост ВМ или хоста")
    nic_add.add_argument("--mac", metavar="MAC",
                         help="MAC вида 52:54:00:12:34:56; без флага создаётся автоматически")
    nic_remove = add("remove", "Удалить интерфейс и его MAC у выключенной ВМ; сначала снимите IP в NetBox IPAM.",
                     "vmctl nic remove guest backup", nic_actions,
                     details="При назначенном интерфейсу IP-адресе в NetBox команда остановится до изменений.")
    nic_remove.add_argument("vm", metavar="ВМ", help="Имя выключенной ВМ")
    nic_remove.add_argument("name", metavar="ИНТЕРФЕЙС", help="Имя существующего интерфейса")

    delete = add("delete", "Удалить выключенную ВМ из libvirt и, при наличии ключа, из NetBox. IP с интерфейсов нужно снять заранее; файлы дисков по умолчанию остаются.",
                 "vmctl --dry-run delete guest --purge",
                 details="Проверьте план через --dry-run. После частичной ошибки повторите delete с тем же выбором --purge.")
    delete.add_argument("vm", metavar="ИМЯ", help="Имя выключенной ВМ")
    delete.add_argument("--purge", action="store_true",
                        help="Удалить также управляемые файлы дисков; без флага сохранить их")

    for name, description in (
        ("start", "Запросить запуск ВМ; при наличии ключа сначала установить статус active в NetBox."),
        ("shutdown", "Запросить штатное выключение и ждать до 60 секунд. Если гость не ответил, вернуть ошибку. При наличии ключа сначала установить offline в NetBox."),
        ("reboot", "Запросить штатную перезагрузку работающей ВМ."),
        ("autostart", "Включить автоматический запуск ВМ вместе с хостом."),
        ("autostart-off", "Выключить автоматический запуск ВМ вместе с хостом."),
    ):
        add(name, description, f"vmctl {name} guest").add_argument("vm", metavar="ИМЯ",
                                                                   help="Имя локальной ВМ")
    parsers["shutdown"].add_argument("--force", action="store_true",
                                     help="Немедленно выключить через virsh destroy; возможна потеря незаписанных данных")

    parser.epilog = _overview([
        ("Просмотр", [
            ("list", "ВМ: питание, ресурсы, IP, дисплей"),
            ("check", "Проверить хост или показать подробности ВМ"),
            ("audit", "Сверить ВМ с NetBox"),
        ]),
        ("Виртуальные машины", [
            ("prepare", "Только NetBox; затем правки и sync"),
            ("create", "prepare + sync сразу, без паузы для правок"),
            ("sync", "Одна или все ВМ по NetBox; --purge: файлы"),
            ("adopt", "Передать в NetBox одну или все локальные ВМ"),
            ("delete", "Удалить ВМ; --purge удалит и диски"),
        ]),
        ("Компоненты", [
            ("disk add", "Добавить диск"),
            ("disk remove", "Удалить диск; --purge удалит и файл"),
            ("nic add", "Добавить интерфейс и MAC"),
            ("nic remove", "Удалить интерфейс и MAC"),
        ]),
        ("Питание", [
            ("start", "Запустить"),
            ("shutdown", "Выключить; --force: без ожидания гостя"),
            ("reboot", "Перезагрузить"),
            ("autostart", "Включить автозапуск"),
            ("autostart-off", "Выключить автозапуск"),
        ]),
    ], parsers)
    return parser


def parse_args() -> argparse.Namespace:
    return build_parser().parse_args()


def require_netbox(config: dict) -> None:
    if not has_netbox_key(config):
        raise NetBoxError("NetBox key is absent; this host runs in local-only mode")


def local_uuid(config: dict, name: str) -> str:
    value = subprocess.run(["virsh", "-c", config["host"]["libvirt_uri"], "domuuid", name],
                           check=True, capture_output=True, text=True).stdout.strip()
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError(f"libvirt VM {name!r} returned an invalid UUID: {value!r}") from error
    return canonical


def verify_vm_serial(config: dict, record: dict, local_exists: bool) -> str:
    """Require NetBox UUID and reject a conflicting local VM."""
    saved = vm_serial_uuid(record)
    if not saved:
        raise NetBoxError(f"VM {record['name']}: NetBox Serial is empty; set it to the libvirt UUID in NetBox before sync")
    actual = local_uuid(config, record["name"]) if local_exists else None
    if actual and saved != actual:
        raise NetBoxError(f"VM {record['name']}: NetBox Serial {saved} differs from libvirt UUID {actual}")
    return saved


def display_url(display: dict) -> str:
    kind = (display.get("type") or "none").lower()
    if kind == "none":
        return "-"
    listen = display.get("listen")
    port = display.get("port")
    if not listen:
        return kind.upper()
    host = f"[{listen}]" if ":" in listen and not listen.startswith("[") else listen
    if isinstance(port, int) and port >= 0:
        return f"{kind}://{host}:{port}"
    return f"{kind.upper()} {host} (порт при запуске)"


def show_list(config: dict) -> int:
    """Show local domains with NetBox primary addresses when available."""
    names = local_names(config)
    if not names:
        print("Локальных ВМ нет.")
        return 0
    records = {}
    if has_netbox_key(config):
        try:
            device_id, _ = find_device(config)
            records = {vm["name"]: vm for vm in list_vms(config, device_id)}
        except NetBoxError as error:
            print(f"NetBox недоступен; IP-адреса не показаны: {error}", file=sys.stderr)

    headings = ("Имя", "Состояние", "CPU", "RAM MiB", "Диск GiB", "Авто", "IPv4", "IPv6", "Дисплей / URL")
    rows = []
    failed = False
    for name in names:
        try:
            vm = inspect_vm(config, name)
            display = inspect_display(config, name)
            disk_gib = vm["disk_mb"] / 1024
            disk_text = f"{disk_gib:.1f}".rstrip("0").rstrip(".") if disk_gib else "0"
            record = records.get(name, {})

            def primary_ip(family: str) -> str:
                ip = record.get(f"primary_ip{family}")
                return ip.get("address", "-") if isinstance(ip, dict) else "-"
            rows.append((name, "запущена" if vm["status"] == "active" else "выключена",
                         str(vm["vcpus"]), str(vm["memory_mb"]), disk_text,
                         "да" if vm["autostart"] else "нет", primary_ip("4"), primary_ip("6"),
                         display_url(display)))
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            failed = True
            print(f"{name}: не удалось прочитать параметры: {error}", file=sys.stderr)
            rows.append((name, "ошибка", "-", "-", "-", "-", "-", "-", "-"))

    widths = [max(len(str(row[index])) for row in (headings, *rows)) for index in range(len(headings))]
    for row in (headings, *rows):
        print("  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)).rstrip())
    return 1 if failed else 0


def _stopped(config: dict, name: str) -> bool:
    if name not in local_names(config):
        return False
    state = subprocess.run(["virsh", "-c", config["host"]["libvirt_uri"], "domstate", name],
                           check=True, capture_output=True, text=True).stdout.strip()
    if state != "shut off":
        raise ValueError(f"shut down {name} before changing components or deleting it")
    return True


def _managed_disk_path(config: dict, name: str, disk_name: str) -> Path:
    filename = f"{name}.qcow2" if disk_name == name else f"{name}-{disk_name}.qcow2"
    return Path(config["storage"]["directory"]) / filename


def _local_component_spec(config: dict, name: str) -> dict:
    local = inspect_vm(config, name)
    disks = [{"name": name if index == 0 else f"disk-{item['target']}",
              "path": item["path"], "size_gb": item["size_gb"]}
             for index, item in enumerate(local["disks"])]
    return {"name": name, "source": "existing", "vcpus": local["vcpus"],
            "memory_mb": local["memory_mb"], "disk_gb": sum(item["size_gb"] for item in disks),
            "description": local["description"], "display": local["display"],
            "storage_directory": config["storage"]["directory"], "bridge": config["network"]["bridge"],
            "disks": disks, "interfaces": local["interfaces"]}


def _purgeable(config: dict, name: str, paths: list[str]) -> None:
    directory = Path(config["storage"]["directory"])
    for value in paths:
        path = Path(value)
        if path.parent != directory or path.is_symlink() or not (
                path.name == f"{name}.qcow2" or path.name.startswith(f"{name}-") and path.suffix == ".qcow2"):
            raise ValueError(f"cannot purge unmanaged disk: {path}")


def component_command(config: dict, args: argparse.Namespace) -> int:
    name, kind, action = args.vm, args.command, args.action
    if not NAME_RE.fullmatch(name):
        raise ValueError("invalid VM name")
    component_name = args.name
    if not COMPONENT_NAME.fullmatch(component_name):
        raise ValueError("component name must contain only letters, digits, dots, underscores or hyphens")
    if kind == "disk" and component_name.startswith("media-"):
        raise ValueError("media-* names are reserved for mounted ISO/CD-ROM virtual disks")
    if kind == "disk" and action == "add" and (args.size_gb <= 0):
        raise ValueError("--size-gb must be positive")
    bridge = getattr(args, "bridge", None)
    if bridge and (Path(bridge).name != bridge or not (Path("/sys/class/net") / bridge / "bridge").is_dir()):
        raise ValueError(f"bridge is missing: {bridge}")
    mac = getattr(args, "mac", None)
    if mac and not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        raise ValueError("--mac must be a 48-bit MAC address")
    exists = _stopped(config, name)
    if has_netbox_key(config):
        device_id, cluster_id = find_device(config)
        record = get_vm(config, name, cluster_id, device_id)
        if (record.get("local_context_data") or {}).get("vmctl", {}).get("delete_requested"):
            raise NetBoxError("VM deletion is pending")
        items = vm_disks(config, record["id"]) if kind == "disk" else vm_interfaces(config, record["id"])
        found = next((item for item in items if item["name"] == component_name), None)
        if kind == "disk" and action == "add" and not found:
            context = (record.get("local_context_data") or {}).get("vmctl") or {}
            directory = context.get("storage_directory") or config["storage"]["directory"]
            paths = context.get("disk_paths_by_name") or {}
            candidate = Path(paths.get(component_name) or
                             str(Path(directory) / f"{name}-{component_name}.qcow2"))
            if candidate.exists():
                raise ValueError(f"disk file already exists; choose another name or inspect it: {candidate}")
        if action == "add" and found and kind == "disk" and int(found["size"]) != args.size_gb * 1024:
            raise NetBoxError("existing NetBox disk has a different size")
        if action == "remove" and not found:
            raise NetBoxError(f"NetBox {kind} {component_name!r} does not exist")
        if kind == "disk" and action == "remove" and (component_name == name or len(items) <= 1):
            raise ValueError("cannot remove the boot or last disk; delete the VM instead")
        if kind == "nic" and action == "remove" and interface_ips(config, found["id"]):
            raise NetBoxError("unassign IP addresses in NetBox IPAM before removing this interface")
        if args.dry_run:
            print(f"WOULD {action.upper()} NetBox {kind} {component_name} on {name}")
            if exists:
                print(f"WOULD RECONCILE local VM {name}")
            return 0
        if action == "add":
            if kind == "disk" and not found:
                add_component(config, "disk", {"virtual_machine": record["id"],
                                               "name": component_name, "size": args.size_gb * 1024,
                                               "description": str(candidate)})
            elif kind == "nic":
                if not found:
                    found = add_component(config, "nic", {"virtual_machine": record["id"],
                                                          "name": component_name, "enabled": True})
                ensure_primary_mac(config, found, mac)
                if bridge:
                    context_data = record.get("local_context_data") or {}
                    context = dict(context_data.get("vmctl") or {})
                    bridges = dict(context.get("interface_bridges") or {})
                    bridges[component_name] = bridge
                    context["interface_bridges"] = bridges
                    patch_vm(config, record["id"], {"local_context_data": {**context_data, "vmctl": context}})
        else:
            if kind == "disk":
                remove_component(config, "disk", found["id"])
            else:
                macs = interface_macs(config, found["id"])
                if not macs and found.get("primary_mac_address"):
                    macs = [found["primary_mac_address"]]
                if found.get("primary_mac_address"):
                    _request(config, "PATCH", f"virtualization/interfaces/{found['id']}/", {"primary_mac_address": None})
                for mac_item in macs:
                    remove_component(config, "mac", mac_item["id"])
                remove_component(config, "nic", found["id"])
            context_data = record.get("local_context_data") or {}
            context = dict(context_data.get("vmctl") or {})
            field = "disk_paths_by_name" if kind == "disk" else "interface_bridges"
            mappings = dict(context.get(field) or {})
            if component_name in mappings:
                del mappings[component_name]
                context[field] = mappings
                patch_vm(config, record["id"], {"local_context_data": {**context_data, "vmctl": context}})
        if exists:
            updated = get_vm(config, name, cluster_id, device_id)
            spec = local_spec_from_netbox(updated, config)
            redefine_vm(spec, config, Path(__file__).parent, False, getattr(args, "purge", False))
            verify_local_vm(spec, config)
        return 0
    if not exists:
        raise ValueError(f"local VM {name!r} does not exist")
    spec = _local_component_spec(config, name)
    items = spec["disks"] if kind == "disk" else spec["interfaces"]
    if kind == "disk":
        path = _managed_disk_path(config, name, component_name)
        found = next((item for item in items if item["path"] == str(path) or item["name"] == component_name), None)
        if action == "add":
            if found:
                raise ValueError("disk already exists")
            if path.exists():
                raise ValueError(f"disk file already exists; choose another name or inspect it: {path}")
            items.append({"name": component_name, "path": str(path), "size_gb": args.size_gb})
        else:
            if not found and getattr(args, "purge", False):
                pending = pending_purge_file(Path(__file__).parent, name)
                if pending.exists() and str(path) in json.loads(pending.read_text(encoding="utf-8")):
                    if args.dry_run:
                        print(f"WOULD RETRY pending purge of {path}")
                    else:
                        purge_pending(spec, config, Path(__file__).parent)
                    return 0
            if component_name == name or len(items) <= 1 or not found:
                raise ValueError("disk missing or is the boot/last disk")
            items.remove(found)
    else:
        found = next((item for item in items if item.get("alias") == interface_alias(component_name)
                      or item["name"] == component_name), None)
        if action == "add":
            if found:
                raise ValueError("interface already exists")
            generated = mac or "52:54:00:" + ":".join(f"{byte:02X}" for byte in secrets.token_bytes(3))
            items.append({"name": component_name, "bridge": bridge or config["network"]["bridge"],
                          "mac_address": generated})
        else:
            if not found:
                raise ValueError("interface does not exist")
            items.remove(found)
    if args.dry_run:
        redefine_vm(spec, config, Path(__file__).parent, True, getattr(args, "purge", False))
        return 0
    redefine_vm(spec, config, Path(__file__).parent, False, getattr(args, "purge", False))
    verify_local_vm(spec, config)
    return 0


def delete_vm(config: dict, args: argparse.Namespace) -> int:
    name = args.vm
    if not NAME_RE.fullmatch(name):
        raise ValueError("invalid VM name")
    exists = _stopped(config, name)
    managed = has_netbox_key(config)
    record = None
    if managed:
        device_id, cluster_id = find_device(config)
        record = get_vm(config, name, cluster_id, device_id)
        for interface in vm_interfaces(config, record["id"]):
            if interface_ips(config, interface["id"]):
                raise NetBoxError("unassign VM interface IP addresses in NetBox IPAM before deleting the VM")
    elif not exists:
        raise ValueError(f"local VM {name!r} does not exist")
    previous = ((record or {}).get("local_context_data") or {}).get("vmctl", {}).get("delete_requested")
    paths = inspect_vm(config, name)["disk_paths"] if exists else (previous or {}).get("disk_paths", [])
    if args.purge:
        _purgeable(config, name, paths)
    if args.dry_run:
        if managed:
            print(f"WOULD MARK NetBox VM {name} for deletion")
        if exists:
            print(f"WOULD UNDEFINE local VM {name}")
            for path in paths:
                print(f"WOULD {'PURGE' if args.purge else 'KEEP'} {path}")
        if managed:
            print(f"WOULD DELETE NetBox VM {name}")
        return 0
    if record:
        data = record.get("local_context_data") or {}
        context = dict(data.get("vmctl") or {})
        previous = context.get("delete_requested")
        if previous and bool(previous.get("purge")) != args.purge:
            raise ValueError("retry deletion with the original --purge choice")
        if not previous:
            context["delete_requested"] = {"purge": args.purge, "disk_paths": paths}
            patch_vm(config, record["id"], {"local_context_data": {**data, "vmctl": context},
                                           "changelog_message": "vmctl requested deletion"})
    if exists:
        subprocess.run(["virsh", "-c", config["host"]["libvirt_uri"], "undefine", name], check=True)
        (Path(__file__).parent / "state" / "domains" / f"{name}.xml").unlink(missing_ok=True)
    if args.purge:
        for value in paths:
            Path(value).unlink(missing_ok=True)
    if record:
        _request(config, "DELETE", f"virtualization/virtual-machines/{record['id']}/")
    print(f"Deleted VM {name}; disk files {'purged' if args.purge else 'kept'}")
    return 0


def audit(config: dict) -> int:
    require_netbox(config)
    device_id, _ = find_device(config)
    records = list_vms(config, device_id)
    remote = {vm["name"]: vm for vm in records}
    if len(remote) != len(records):
        raise NetBoxError("duplicate VM names are assigned to this host device")
    names = set(local_names(config))
    problems = 0
    for name in sorted(names | remote.keys()):
        if name not in remote:
            print(f"MISSING NETBOX: {name}")
            problems += 1
            continue
        if name not in names:
            print(f"MISSING LOCAL: {name}")
            problems += 1
            continue
        try:
            local = inspect_vm(config, name)
            vm = remote[name]
            try:
                serial = vm_serial_uuid(vm)
                if not serial:
                    print(f"MISSING SERIAL {name}: NetBox VM has no libvirt UUID")
                    problems += 1
                elif local.get("uuid") != serial:
                    print(f"DIFF {name} uuid: NetBox Serial={serial} local={local.get('uuid')!r}")
                    problems += 1
            except NetBoxError as error:
                print(f"INVALID {name}: {error}")
                problems += 1
            status = vm.get("status")
            status = status.get("value") if isinstance(status, dict) else status
            start = vm.get("start_on_boot")
            start = start.get("value") if isinstance(start, dict) else start
            expected = {
                "vcpus": int(vm["vcpus"]), "memory_mb": int(vm["memory"]),
                "disk_mb": int(vm["disk"]),
                "autostart": start == "on",
            }
            if status in ("active", "offline"):
                expected["status"] = status
            context = (vm.get("local_context_data") or {}).get("vmctl") or {}
            supported_context = context.get("version") in (1, 2, 3) and context.get("source") in ("iso", "cloud_image", "existing")
            if not supported_context:
                print(f"INVALID {name}: NetBox VM has no supported vmctl context")
                problems += 1
            else:
                if context["version"] in (2, 3):
                    try:
                        spec = local_spec_from_netbox(vm, config)
                        expected.update({field: spec[field] for field in ("description", "display")})
                        expected["disk_mb"] = sum(item["size_mb"] for item in spec["disks"])
                        if {item["path"] for item in spec["disks"]} != set(local["disk_paths"]):
                            print(f"DIFF {name} disks: NetBox and local disk paths differ")
                            problems += 1
                        actual_sizes = {item["path"]: item.get("size_bytes") for item in local["disks"]}
                        if any((actual_sizes.get(item["path"]) + 1024**2 - 1) // 1024**2 != item["size_mb"]
                               if actual_sizes.get(item["path"]) is not None else True
                               for item in spec["disks"]):
                            print(f"DIFF {name} disk sizes: NetBox and local capacities differ")
                            problems += 1
                        wanted_media = [{key: item[key] for key in ("path", "target", "size_mb") if key in item}
                                        for item in spec.get("mounted_media", [])]
                        observed_media = [{key: item[key] for key in ("path", "target", "size_mb") if key in item}
                                          for item in local.get("mounted_media", [])]
                        if context.get("source") != "cloud_image" and wanted_media != observed_media:
                            print(f"DIFF {name} mounted media: NetBox and local CD-ROM sources differ")
                            problems += 1
                        wanted_nics = {(item["bridge"], item["mac_address"].lower()) for item in spec["interfaces"]}
                        actual_nics = {(item["bridge"], (item["mac_address"] or "").lower())
                                       for item in local["interfaces"]}
                        if wanted_nics != actual_nics or len(spec["interfaces"]) != len(local["interfaces"]):
                            print(f"DIFF {name} interfaces: NetBox and local interfaces differ")
                            problems += 1
                    except NetBoxError as error:
                        print(f"INVALID {name}: {error}")
                        problems += 1
                if context.get("bridge") is not None and context["version"] == 1:
                    expected["bridge"] = context["bridge"]
                if context.get("disk_paths") is not None and context["version"] == 1:
                    expected["disk_paths"] = context["disk_paths"]
                elif context["version"] == 1 and context["source"] in ("iso", "cloud_image"):
                    directory = context.get("storage_directory")
                    if not isinstance(directory, str):
                        raise ValueError("NetBox VM has no storage_directory")
                    expected["disk_paths"] = [str(Path(directory) / f"{name}.qcow2")]
            for field, wanted in expected.items():
                observed = local[field].lower() if field == "mac_address" and local[field] else local[field]
                if observed != wanted:
                    if field == "display":
                        print(f"DIFF {name} display: NetBox and local XML differ (password redacted)")
                    else:
                        print(f"DIFF {name} {field}: NetBox={wanted!r} local={observed!r}")
                    problems += 1
        except (ValueError, KeyError, subprocess.CalledProcessError) as error:
            print(f"INVALID {name}: {error}")
            problems += 1
    print(f"Audit: {len(names)} local, {len(remote)} NetBox, {problems} issue(s)")
    return 1 if problems else 0


def adopt(config: dict, dry_run: bool, name: str | None = None) -> int:
    require_netbox(config)
    device_id, cluster_id = find_device(config)
    records = list_vms(config, device_id)
    remote = {vm["name"]: vm for vm in records}
    if len(remote) != len(records):
        raise NetBoxError("duplicate VM names are assigned to this host device")
    names = local_names(config)
    if name is not None:
        if name not in names:
            raise ValueError(f"local VM {name!r} does not exist")
        names = [name]
    for vm_name in names:
        existing = remote.get(vm_name)
        if not existing:
            duplicate = find_vm(config, vm_name, cluster_id)
            if duplicate:
                raise NetBoxError(f"VM {vm_name!r} already exists in this cluster on another host")
        vm = inspect_vm(config, vm_name)
        if existing:
            record = get_vm(config, vm_name, cluster_id, device_id)
            reconcile_adopted_vm(config, record, vm, dry_run)
        elif dry_run:
            validate_import_vm(vm)
            print(f"WOULD IMPORT {vm_name}: {vm['vcpus']} vCPU, {vm['memory_mb']} MiB, "
                  f"{len(vm['disks'])} disk(s), {len(vm['interfaces'])} interface(s), "
                  f"{len(vm.get('mounted_media', []))} ISO/media")
        else:
            record = import_vm(config, vm, cluster_id, device_id)
            print(f"IMPORTED {vm_name} as NetBox VM {record['id']}")
    print(f"Adoption: {len(names)} VM(s) processed")
    return 0


def doctor(config: dict) -> int:
    problems = 0
    for binary in ("virsh", "qemu-img"):
        if shutil.which(binary):
            print(f"OK {binary}")
        else:
            print(f"MISSING {binary}")
            problems += 1
    try:
        print(f"OK libvirt: {len(local_names(config))} VM(s)")
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR libvirt: {error}")
        problems += 1
    storage = config.get("storage", {}).get("directory")
    bridge = config.get("network", {}).get("bridge")
    if isinstance(storage, str) and Path(storage).is_dir():
        print(f"OK storage: {storage}")
    else:
        print(f"ERROR storage directory: {storage!r}")
        problems += 1
    if isinstance(bridge, str) and (Path("/sys/class/net") / bridge / "bridge").is_dir():
        print(f"OK bridge: {bridge}")
    else:
        print(f"ERROR network bridge: {bridge!r}")
        problems += 1
    if has_netbox_key(config):
        try:
            device_id, cluster_id = find_device(config)
            print(f"OK NetBox: device {device_id}, cluster {cluster_id}")
            list_vms(config, device_id)
            print("OK NetBox VM read access")
        except NetBoxError as error:
            print(f"ERROR NetBox: {error}")
            problems += 1
    else:
        print("LOCAL ONLY: no NetBox key; VM inventory is not documented in NetBox")
    return 1 if problems else 0


def check_host(config: dict) -> int:
    result = doctor(config)
    uri = config["host"]["libvirt_uri"]
    for title, command in (("Версия libvirt", "version"),
                           ("Пулы хранения", "pool-list"),
                           ("Сети libvirt", "net-list")):
        print(f"\n{title}:")
        try:
            completed = subprocess.run(
                ["virsh", "-c", uri, command, *(["--all"] if command != "version" else [])],
                capture_output=True, text=True, check=False,
            )
            if completed.returncode:
                print(completed.stderr.strip() or f"virsh {command} завершился с кодом {completed.returncode}")
                result = 1
            else:
                print(completed.stdout.rstrip() or "Нет записей")
        except OSError as error:
            print(f"Ошибка: {error}")
            result = 1
    return result


def check_vm(config: dict, name: str) -> int:
    vm = inspect_vm(config, name)
    display = inspect_display(config, name)
    print(f"ВМ: {name}")
    print(f"UUID: {vm.get('uuid') or '-'}")
    print(f"Состояние libvirt: {'запущена' if vm['status'] == 'active' else 'выключена'}")
    print(f"Автозапуск: {'да' if vm['autostart'] else 'нет'}")
    print(f"CPU: {vm['vcpus']}; RAM: {vm['memory_mb']} MiB")
    print(f"Диски: {len(vm['disks'])}; общий размер: {vm['disk_mb']} MiB")
    for disk in vm["disks"]:
        print(f"  {disk['target']}: {disk['size_gb']} GiB; {disk['path'] or '-'}")
    print(f"ISO/CD-ROM: {len(vm.get('mounted_media', []))}")
    for media in vm.get("mounted_media", []):
        print(f"  {media.get('target') or '-'}: {media.get('size_mb', '?')} MiB; {media['path']}")
    print(f"Интерфейсы: {len(vm['interfaces'])}")
    for interface in vm["interfaces"]:
        print(f"  {interface['name']}: MAC {interface['mac_address'] or '-'}; "
              f"мост {interface['bridge'] or '-'}; host {interface.get('host_dev') or '-'}")
    print(f"Дисплей: {(display.get('type') or 'none').upper()}")
    if display.get("type") != "none":
        print(f"  IP: {display.get('listen') or '-'}")
        port = display.get("port")
        print(f"  Порт: {port if isinstance(port, int) else 'назначится при запуске ВМ'}")
        password = display.get("password")
        print(f"  Пароль: {json.dumps(password, ensure_ascii=False) if password is not None else 'не задан'}")
    print(f"Description: {vm['description'] or '-'}")
    if has_netbox_key(config):
        try:
            device_id, _ = find_device(config)
            record = next((item for item in list_vms(config, device_id) if item.get("name") == name), None)
            if record is None:
                print("NetBox: ВМ не заведена на устройстве этого хоста")
            else:
                status = record.get("status")
                status = status.get("value") if isinstance(status, dict) else status
                print(f"NetBox: статус {status or '-'}")
                print(f"NetBox Serial: {record.get('serial') or '-'}")
                for family in ("4", "6"):
                    ip = record.get(f"primary_ip{family}")
                    print(f"Primary IPv{family}: {ip.get('address', '-') if isinstance(ip, dict) else '-'}")
        except NetBoxError as error:
            print(f"NetBox недоступен: {error}", file=sys.stderr)
    return 0


def reconcile_power(config: dict, record: dict, desired_status: str) -> None:
    if desired_status not in ("active", "offline"):
        return
    uri = config["host"]["libvirt_uri"]
    name = record["name"]
    state = subprocess.run(
        ["virsh", "-c", uri, "domstate", name],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if state not in ("running", "shut off"):
        raise ValueError(f"cannot reconcile VM power from local state {state!r}")
    command = None
    if desired_status == "active" and state != "running":
        command = "start"
    elif desired_status == "offline" and state == "running":
        command = "shutdown"
    if command:
        patch_vm(config, record["id"], {
            "status": desired_status,
            "changelog_message": f"vmctl requested {command} during sync",
        })
        subprocess.run(["virsh", "-c", uri, command, name], check=True)


def shutdown_vm(config: dict, name: str, force: bool, dry_run: bool) -> int:
    uri = config["host"]["libvirt_uri"]
    command = ["virsh", "-c", uri, "destroy" if force else "shutdown", name]
    if dry_run:
        if has_netbox_key(config):
            print(f"NetBox: update {name}: {{'status': 'offline'}}")
        print(shlex.join(command))
        return 0

    if has_netbox_key(config):
        device_id, cluster_id = find_device(config)
        record = get_vm(config, name, cluster_id, device_id)
        patch_vm(config, record["id"], {
            "status": "offline",
            "changelog_message": "vmctl requested forced shutdown" if force else "vmctl requested shutdown",
        })

    def state() -> str:
        return subprocess.run(["virsh", "-c", uri, "domstate", name], check=True,
                              capture_output=True, text=True).stdout.strip()

    if state() == "shut off":
        print(f"VM {name} is already shut off")
        return 0
    subprocess.run(command, check=True)
    if force:
        if state() != "shut off":
            print(f"VM {name} is still running after forced shutdown", file=sys.stderr)
            return 1
        print(f"VM {name} is shut off")
        return 0

    deadline = time.monotonic() + 60
    while True:
        current = state()
        if current == "shut off":
            print(f"VM {name} is shut off")
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"VM {name} did not shut down within 60 seconds (state: {current}). "
                  f"Check the guest or use vmctl shutdown {name} --force for a hard power-off.", file=sys.stderr)
            return 1
        time.sleep(min(2, remaining))


def provision_command(config: dict, args: argparse.Namespace) -> int:
    reserved_name = None
    try:
        if args.command in ("create", "prepare"):
            with args.spec.open("rb") as file:
                request_vm = validate_spec(tomllib.load(file))
            if args.command == "prepare":
                require_netbox(config)
            if not has_netbox_key(config):
                plan = {
                    **request_vm,
                    "bridge": config["network"]["bridge"],
                    "storage_directory": config["storage"]["directory"],
                }
                return create_vm(plan, config, Path(__file__).parent, args.dry_run)
            if args.dry_run:
                print(f"NetBox: create planned VM {request_vm['name']} on device {config['netbox']['device']}")
                print(f"NetBox: create virtual disk, interface {request_vm.get('interface_name', 'inet')} and primary MAC")
                if request_vm["source"] == "iso":
                    print(f"NetBox: create Virtual Disk media-sda for ISO {request_vm['iso']}")
                if args.command == "prepare":
                    return 0
                plan = {
                    **request_vm,
                    "bridge": config["network"]["bridge"],
                    "storage_directory": config["storage"]["directory"],
                }
                return create_vm(plan, config, Path(__file__).parent, True)
            device_id, cluster_id = find_device(config)
            if args.command == "prepare":
                existing = find_vm(config, request_vm["name"], cluster_id)
                if existing:
                    record = get_vm(config, request_vm["name"], cluster_id, device_id)
                    context = (record.get("local_context_data") or {}).get("vmctl") or {}
                    if context.get("version") not in (2, 3) or context.get("source") != request_vm["source"]:
                        raise NetBoxError("existing NetBox VM does not match this preparation request")
                else:
                    reserve_vm(config, request_vm, cluster_id, device_id)
                    record = get_vm(config, request_vm["name"], cluster_id, device_id)
            else:
                reserve_vm(config, request_vm, cluster_id, device_id)
                record = get_vm(config, request_vm["name"], cluster_id, device_id)
            reserved_name = request_vm["name"]
            create_vm_components(config, record, request_vm.get("interface_name", "inet"),
                                 request_vm.get("mac_address"),
                                 request_vm["disk_gb"] * 1024)
            if args.command == "prepare":
                print(f"Prepared in NetBox: {request_vm['name']}. Set IPs and display, then run: vmctl sync {request_vm['name']}")
                return 0
        else:
            require_netbox(config)
            device_id, cluster_id = find_device(config)
            record = get_vm(config, args.vm, cluster_id, device_id)
            serial = verify_vm_serial(config, record, args.vm in local_names(config))
            context = (record.get("local_context_data") or {}).get("vmctl") or {}
            if context.get("delete_requested"):
                raise NetBoxError("VM deletion is pending; retry vmctl delete instead of sync")
            missing_macs = [item for item in vm_interfaces(config, record["id"])
                            if not item.get("primary_mac_address")]
            if missing_macs and args.dry_run:
                for item in missing_macs:
                    print(f"WOULD CREATE primary MAC in NetBox for {item['name']}")
                return 0
            for item in missing_macs:
                ensure_primary_mac(config, item)
        vm = local_spec_from_netbox(record, config)
        if vm["source"] == "existing":
            if args.command != "sync":
                raise NetBoxError("existing VM records cannot be used as creation requests")
            if args.vm not in local_names(config):
                raise NetBoxError("adopted VM is missing locally; restore it before sync")
        else:
            vm = validate_spec({"vm": vm})
        if args.command == "sync":
            vm["uuid"] = serial
        desired_status = record.get("status")
        if isinstance(desired_status, dict):
            desired_status = desired_status.get("value")
        if desired_status not in ("planned", "staged", "offline", "active"):
            raise NetBoxError(f"VM has unsupported NetBox status: {desired_status!r}")
        if args.command == "sync":
            existing = subprocess.run(
                ["virsh", "-c", config["host"]["libvirt_uri"], "list", "--all", "--name"],
                check=True, capture_output=True, text=True,
            )
            if vm["name"] in existing.stdout.splitlines():
                changed = False
                try:
                    verify_local_vm(vm, config)
                except ValueError:
                    context = (record.get("local_context_data") or {}).get("vmctl") or {}
                    if context.get("version") not in (2, 3):
                        raise
                    if not args.dry_run:
                        patch_vm(config, record["id"], {
                            "status": desired_status,
                            "changelog_message": "vmctl requested local definition sync",
                        })
                    redefine_vm(vm, config, Path(__file__).parent, args.dry_run,
                                getattr(args, "purge", False), stage_running=True)
                    changed = True
                    if not args.dry_run:
                        verify_local_vm(vm, config)
                if args.dry_run:
                    if not changed:
                        print(f"Local VM definition matches NetBox: {vm['name']}")
                    if getattr(args, "purge", False) and pending_purge_file(Path(__file__).parent, vm["name"]).exists():
                        print(f"WOULD RETRY pending disk purge for {vm['name']}")
                    if desired_status == "planned":
                        print(f"NetBox: stage {vm['name']}")
                    if desired_status in ("active", "offline"):
                        state = subprocess.run(
                            ["virsh", "-c", config["host"]["libvirt_uri"], "domstate", vm["name"]],
                            check=True, capture_output=True, text=True,
                        ).stdout.strip()
                        power_command = "start" if desired_status == "active" and state == "shut off" else (
                            "shutdown" if desired_status == "offline" and state == "running" else None
                        )
                        if power_command:
                            print(f"NetBox: set {vm['name']} status to {desired_status}")
                            print(shlex.join(["virsh", "-c", config["host"]["libvirt_uri"], power_command, vm["name"]]))
                        if state == "running" and not changed:
                            try:
                                verify_local_vm(vm, config, live=True)
                            except ValueError:
                                print(f"PENDING RESTART: {vm['name']} needs shutdown and start")
                    return 0
                if getattr(args, "purge", False) and not changed and pending_purge_file(Path(__file__).parent, vm["name"]).exists():
                    patch_vm(config, record["id"], {
                        "status": desired_status,
                        "changelog_message": "vmctl requested pending disk purge",
                    })
                    purge_pending(vm, config, Path(__file__).parent)
                state = subprocess.run(
                    ["virsh", "-c", config["host"]["libvirt_uri"], "domstate", vm["name"]],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                if state == "running":
                    try:
                        verify_local_vm(vm, config, live=True)
                    except ValueError:
                        print(f"Pending restart for {vm['name']}: shutdown and start to apply persistent XML")
                if desired_status == "planned":
                    patch_vm(config, record["id"], {"status": "staged"})
                reconcile_power(config, record, desired_status)
                print(f"Local VM matches NetBox: {vm['name']}")
                return 0
            patch_vm(config, record["id"], {
                "status": desired_status,
                "changelog_message": "vmctl requested local sync",
            })
        create_vm(vm, config, Path(__file__).parent, args.dry_run)
        if not args.dry_run and desired_status == "planned":
            patch_vm(config, record["id"], {"status": "staged"})
            print(f"NetBox VM {vm['name']} is staged")
        if args.command == "sync" and desired_status == "active" and not args.dry_run:
            reconcile_power(config, record, desired_status)
    except (OSError, tomllib.TOMLDecodeError, ValueError, NetBoxError, subprocess.CalledProcessError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        if has_netbox_key(config):
            print("NetBox remains the source of truth; inspect its VM record before retrying.", file=sys.stderr)
        if reserved_name:
            print(f"Retry: vmctl sync {reserved_name}", file=sys.stderr)
        return 1
    return 0


def sync_all(config: dict, args: argparse.Namespace) -> int:
    try:
        require_netbox(config)
        device_id, _ = find_device(config)
        records = list_vms(config, device_id)
        names = sorted(record["name"] for record in records)
        if len(names) != len(set(names)):
            raise NetBoxError("multiple VM records with the same name are assigned to this host")
    except (NetBoxError, ValueError, OSError) as error:
        print(f"sync failed: {error}", file=sys.stderr)
        return 1

    if not names:
        print("NetBox: нет ВМ, привязанных к устройству этого хоста")
        return 0

    failures = 0
    for name in names:
        print(f"\n== {name} ==", flush=True)
        vm_args = argparse.Namespace(**{**vars(args), "vm": name})
        failures += provision_command(config, vm_args) != 0
    print(f"Sync: {len(names)} VM(s), {failures} failure(s)")
    return 1 if failures else 0


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    if args.command in ("disk", "nic", "delete"):
        try:
            return delete_vm(config, args) if args.command == "delete" else component_command(config, args)
        except (NetBoxError, ValueError, OSError, subprocess.CalledProcessError) as error:
            print(f"{args.command} failed: {error}", file=sys.stderr)
            if has_netbox_key(config):
                print("NetBox remains the source of truth; run vmctl audit and retry sync or delete.", file=sys.stderr)
            return 1

    if args.command in ("check", "audit", "adopt"):
        try:
            if args.command == "check":
                return check_vm(config, args.vm) if args.vm else check_host(config)
            if args.command == "audit":
                return audit(config)
            return adopt(config, args.dry_run, args.vm)
        except (NetBoxError, ValueError, OSError, subprocess.CalledProcessError) as error:
            print(f"{args.command} failed: {error}", file=sys.stderr)
            return 1

    if args.command == "list":
        try:
            return show_list(config)
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            print(f"list failed: {error}", file=sys.stderr)
            return 1

    if args.command == "shutdown":
        try:
            return shutdown_vm(config, args.vm, args.force, args.dry_run)
        except (NetBoxError, OSError, subprocess.CalledProcessError) as error:
            print(f"shutdown failed: {error}", file=sys.stderr)
            if has_netbox_key(config):
                print("NetBox remains the source of truth; check vmctl audit and retry.", file=sys.stderr)
            return 1

    if args.command == "sync" and args.vm is None:
        return sync_all(config, args)
    if args.command in ("create", "prepare", "sync"):
        return provision_command(config, args)

    virsh_args = {
        "start": ["start", getattr(args, "vm", "")],
        "reboot": ["reboot", getattr(args, "vm", "")],
        "autostart": ["autostart", getattr(args, "vm", "")],
        "autostart-off": ["autostart", "--disable", getattr(args, "vm", "")],
    }[args.command]
    command = ["virsh", "-c", config["host"]["libvirt_uri"], *virsh_args]
    netbox_changes = {
        "start": {"status": "active"},
        "reboot": {"status": "active"},
        "autostart": {"start_on_boot": "on"},
        "autostart-off": {"start_on_boot": "off"},
    }.get(args.command)
    if args.dry_run:
        if netbox_changes and has_netbox_key(config):
            print(f"NetBox: update {args.vm}: {netbox_changes}")
        print(shlex.join(command))
        return 0
    if netbox_changes and has_netbox_key(config):
        try:
            device_id, cluster_id = find_device(config)
            record = get_vm(config, args.vm, cluster_id, device_id)
            patch_vm(config, record["id"], {
                **netbox_changes,
                "changelog_message": f"vmctl requested {args.command}",
            })
        except NetBoxError as error:
            print(f"NetBox update failed; local command was not run: {error}", file=sys.stderr)
            return 1
    try:
        result = subprocess.run(command, check=False).returncode
        if result and netbox_changes and has_netbox_key(config):
            print("Local command failed after NetBox was updated; reconcile this VM.", file=sys.stderr)
        return result
    except FileNotFoundError:
        print("virsh is not installed on this host", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
