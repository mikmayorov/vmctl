#!/usr/bin/env python3
"""Manage libvirt VMs on the local host."""

import argparse
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from netbox import (
    NetBoxError, create_vm_components, find_device, find_vm, get_vm, has_netbox_key, import_vm, list_vms,
    local_spec_from_netbox, patch_vm, reserve_vm,
)
from inventory import inspect_vm, local_names
from vmcreate import create_vm, redefine_vm, validate_spec, verify_local_vm


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="print command without executing it")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="define a new VM from ISO or cloud image")
    create.add_argument("spec", type=Path, help="TOML VM specification")
    prepare = commands.add_parser("prepare", help="create VM, disk and interface in NetBox before local provisioning")
    prepare.add_argument("spec", type=Path, help="TOML VM specification")
    sync = commands.add_parser("sync", help="apply a VM definition from NetBox locally")
    sync.add_argument("vm", help="VM name in NetBox")
    commands.add_parser("doctor", help="check local tools and NetBox host configuration")
    commands.add_parser("audit", help="compare local VM inventory with NetBox")
    commands.add_parser("adopt", help="register undocumented local VMs in NetBox")
    for name, help_text in (
        ("check", "show libvirt version"),
        ("list", "list all VMs"),
        ("pools", "list storage pools"),
        ("networks", "list libvirt networks"),
        ("info", "show VM details"),
        ("status", "show VM state"),
        ("start", "start a VM"),
        ("shutdown", "request a graceful VM shutdown"),
        ("reboot", "request a graceful VM reboot"),
        ("console", "open the VM serial console"),
        ("vnc", "show the VM VNC display"),
        ("autostart", "enable VM autostart"),
        ("autostart-off", "disable VM autostart"),
    ):
        command = commands.add_parser(name, help=help_text)
        if name in ("info", "status", "start", "shutdown", "reboot", "console", "vnc", "autostart", "autostart-off"):
            command.add_argument("vm", help="VM name")
    return parser.parse_args()


def require_netbox(config: dict) -> None:
    if not has_netbox_key(config):
        raise NetBoxError("NetBox key is absent; this host runs in local-only mode")


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
            supported_context = context.get("version") in (1, 2) and context.get("source") in ("iso", "cloud_image", "existing")
            if not supported_context:
                print(f"INVALID {name}: NetBox VM has no supported vmctl context")
                problems += 1
            else:
                if context["version"] == 2:
                    try:
                        spec = local_spec_from_netbox(vm, config)
                        expected.update({field: spec[field] for field in ("description", "display")})
                        expected["mac_address"] = spec["mac_address"].lower()
                    except NetBoxError as error:
                        print(f"INVALID {name}: {error}")
                        problems += 1
                if context.get("bridge") is not None:
                    expected["bridge"] = context["bridge"]
                if context.get("disk_paths") is not None:
                    expected["disk_paths"] = context["disk_paths"]
                elif context["source"] in ("iso", "cloud_image"):
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


def adopt(config: dict, dry_run: bool) -> int:
    require_netbox(config)
    device_id, cluster_id = find_device(config)
    records = list_vms(config, device_id)
    remote = {vm["name"]: vm for vm in records}
    if len(remote) != len(records):
        raise NetBoxError("duplicate VM names are assigned to this host device")
    pending = []
    for name in local_names(config):
        existing = remote.get(name)
        if existing:
            print(f"SKIP {name}: already documented on this host (ID {existing['id']})")
            continue
        # Also check the cluster, so a name assigned to another host cannot be duplicated.
        duplicate = find_vm(config, name, cluster_id)
        if duplicate:
            raise NetBoxError(f"VM {name!r} already exists in this cluster on another host")
        pending.append(inspect_vm(config, name))
    for vm in pending:
        if dry_run:
            print(f"WOULD IMPORT {vm['name']}: {vm['vcpus']} vCPU, {vm['memory_mb']} MiB, {vm['disk_mb']} MiB disk")
        else:
            record = import_vm(config, vm, cluster_id, device_id)
            print(f"IMPORTED {vm['name']} as NetBox VM {record['id']}")
    print(f"Adoption: {len(pending)} VM(s) {'would be ' if dry_run else ''}imported")
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


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    if args.command in ("doctor", "audit", "adopt"):
        try:
            if args.command == "doctor":
                return doctor(config)
            if args.command == "audit":
                return audit(config)
            return adopt(config, args.dry_run)
        except (NetBoxError, ValueError, OSError, subprocess.CalledProcessError) as error:
            print(f"{args.command} failed: {error}", file=sys.stderr)
            return 1

    if args.command in ("create", "prepare", "sync"):
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
                        if context.get("version") != 2 or context.get("source") != request_vm["source"]:
                            raise NetBoxError("existing NetBox VM does not match this preparation request")
                    else:
                        reserve_vm(config, request_vm, cluster_id, device_id)
                        record = get_vm(config, request_vm["name"], cluster_id, device_id)
                else:
                    reserve_vm(config, request_vm, cluster_id, device_id)
                    record = get_vm(config, request_vm["name"], cluster_id, device_id)
                reserved_name = request_vm["name"]
                create_vm_components(config, record, request_vm.get("interface_name", "inet"))
                if args.command == "prepare":
                    print(f"Prepared in NetBox: {request_vm['name']}. Set IPs and display, then run: vmctl sync {request_vm['name']}")
                    return 0
            else:
                require_netbox(config)
                device_id, cluster_id = find_device(config)
                record = get_vm(config, args.vm, cluster_id, device_id)
            if (record.get("local_context_data") or {}).get("vmctl", {}).get("source") == "existing":
                if args.command != "sync":
                    raise NetBoxError("existing VM records cannot be used as creation requests")
                if args.vm not in local_names(config):
                    raise NetBoxError("adopted VM is missing locally; restore it before sync")
                local = inspect_vm(config, args.vm)
                if (int(record["vcpus"]), int(record["memory"]), int(record["disk"])) != (
                    local["vcpus"], local["memory_mb"], local["disk_mb"]
                ):
                    raise NetBoxError("adopted VM sizing differs from NetBox; run audit")
                context = record["local_context_data"]["vmctl"]
                for field in ("bridge", "disk_paths"):
                    if context.get(field) != local[field]:
                        raise NetBoxError(f"adopted VM {field} differs from NetBox; run audit")
                if context.get("version") == 2:
                    documented = local_spec_from_netbox(record, config)
                    for field in ("description", "display"):
                        if documented[field] != local[field]:
                            raise NetBoxError(f"adopted VM {field} differs from NetBox; run audit")
                    if documented["mac_address"].lower() != (local["mac_address"] or "").lower():
                        raise NetBoxError("adopted VM MAC differs from NetBox; run audit")
                status = record.get("status")
                status = status.get("value") if isinstance(status, dict) else status
                if not args.dry_run:
                    reconcile_power(config, record, status)
                print(f"Adopted local VM matches NetBox sizing: {args.vm}")
                return 0
            vm = validate_spec({"vm": local_spec_from_netbox(record, config)})
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
                        if context.get("version") != 2:
                            raise
                        redefine_vm(vm, config, Path(__file__).parent, args.dry_run)
                        changed = True
                        if not args.dry_run:
                            verify_local_vm(vm, config)
                    if args.dry_run:
                        if not changed:
                            print(f"Local VM definition matches NetBox: {vm['name']}")
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
                        return 0
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

    virsh_args = {
        "check": ["version"],
        "list": ["list", "--all"],
        "pools": ["pool-list", "--all"],
        "networks": ["net-list", "--all"],
        "info": ["dominfo", getattr(args, "vm", "")],
        "status": ["domstate", getattr(args, "vm", "")],
        "start": ["start", getattr(args, "vm", "")],
        "shutdown": ["shutdown", getattr(args, "vm", "")],
        "reboot": ["reboot", getattr(args, "vm", "")],
        "console": ["console", getattr(args, "vm", "")],
        "vnc": ["vncdisplay", getattr(args, "vm", "")],
        "autostart": ["autostart", getattr(args, "vm", "")],
        "autostart-off": ["autostart", "--disable", getattr(args, "vm", "")],
    }[args.command]
    command = ["virsh", "-c", config["host"]["libvirt_uri"], *virsh_args]
    netbox_changes = {
        "start": {"status": "active"},
        "shutdown": {"status": "offline"},
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
