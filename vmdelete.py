"""Local guest removal and deferred disk disposal, independent of NetBox."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from inventory import local_names
from vmcreate import NAME_RE, pending_purge_file


STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
ARCHIVE_RE = re.compile(r".+\.(\d{8}T\d{12}Z)\.purge\Z")


def local_delete_file(project_dir: Path, name: str) -> Path:
    return project_dir / "state" / "local-delete" / f"{name}.json"


def _virsh(config: dict, *args: str) -> str:
    return subprocess.run(["virsh", "-c", config["host"]["libvirt_uri"], *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _xml(config: dict, name: str, inactive: bool = True) -> ET.Element:
    try:
        return ET.fromstring(_virsh(config, "dumpxml", *(["--inactive"] if inactive else []), name))
    except ET.ParseError as error:
        raise ValueError(f"cannot read libvirt XML for {name}: {error}") from error


def _disk_paths(root: ET.Element) -> list[str]:
    paths = []
    for disk in root.findall("./devices/disk[@device='disk']"):
        source = disk.find("source")
        if disk.get("type") != "file" or source is None or not source.get("file"):
            raise ValueError("delete local can retire only file-backed disks in storage.directory")
        paths.append(source.get("file"))
    if len(set(paths)) != len(paths):
        raise ValueError("VM has duplicate disk paths")
    return paths


def _attached_paths(config: dict, exclude: str | None = None) -> set[Path]:
    paths = set()
    for name in local_names(config):
        if name == exclude:
            continue
        # Check both next-start and running configurations: they can differ.
        for inactive in (True, False):
            root = _xml(config, name, inactive)
            for node in root.findall("./devices/disk/source"):
                value = node.get("file") or node.get("dev")
                if value:
                    paths.add(Path(value).resolve())
    return paths


def _storage(config: dict) -> Path:
    value = config.get("storage", {}).get("directory")
    if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_dir():
        raise ValueError("delete local/purge needs an existing absolute storage.directory")
    return Path(value)


def _managed_path(value: str, directory: Path) -> Path:
    path = Path(value)
    if path.parent != directory or path.is_symlink() or not path.is_absolute():
        raise ValueError(f"cannot retire unmanaged disk: {path}")
    return path


def _save_journal(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.stem}-", delete=False) as file:
        temporary = Path(file.name)
        json.dump(data, file)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def delete_local_vm(config: dict, name: str, project_dir: Path, dry_run: bool) -> int:
    """Undefine a stopped guest, then rename disks to dated .purge files."""
    if not NAME_RE.fullmatch(name):
        raise ValueError("invalid VM name")
    directory = _storage(config)
    journal = local_delete_file(project_dir, name)
    pending = pending_purge_file(project_dir, name)
    cloud_dir = project_dir / "state" / "cloud-init" / name
    xml_path = project_dir / "state" / "domains" / f"{name}.xml"
    exists = name in local_names(config)
    root = None
    if exists:
        if _virsh(config, "domstate", name) != "shut off":
            raise ValueError(f"shut down {name} before deleting it locally")
        root = _xml(config, name)
    if journal.is_symlink():
        raise ValueError(f"invalid local deletion journal: {journal}")
    resuming = journal.exists()
    if resuming:
        data = json.loads(journal.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("name") != name
                or data.get("uri") != config["host"]["libvirt_uri"]
                or data.get("storage_directory") != str(directory)):
            raise ValueError(f"local deletion journal does not match this host: {journal}")
        if root is not None and (root.findtext("uuid") != data.get("uuid")
                                 or _disk_paths(root) != data.get("attached_paths")):
            raise ValueError("local VM changed since deletion started; inspect the local deletion journal")
    else:
        if root is None:
            raise ValueError(f"local VM {name!r} does not exist and has no pending local deletion")
        paths = _disk_paths(root)
        detached = json.loads(pending.read_text(encoding="utf-8")) if pending.exists() else []
        if not isinstance(detached, list) or not all(isinstance(value, str) for value in detached):
            raise ValueError(f"invalid pending disk purge state: {pending}")
        stamp = datetime.now(timezone.utc).strftime(STAMP_FORMAT)
        data = {"name": name, "uuid": root.findtext("uuid"), "uri": config["host"]["libvirt_uri"],
                "storage_directory": str(directory), "attached_paths": paths,
                "disks": [{"source": value, "archive": f"{value}.{stamp}.purge"}
                          for value in dict.fromkeys(paths + detached)],
                "seed": str(directory / f"{name}-seed.iso") if cloud_dir.exists() else None}
    disks = data.get("disks")
    if not isinstance(disks, list):
        raise ValueError(f"invalid disk list in local deletion journal: {journal}")
    moves = []
    all_paths = []
    for entry in disks:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("source", "archive")):
            raise ValueError(f"invalid disk entry in local deletion journal: {journal}")
        source = _managed_path(entry["source"], directory)
        archive = _managed_path(entry["archive"], directory)
        match = ARCHIVE_RE.fullmatch(archive.name)
        if not match or archive.name != f"{source.name}.{match[1]}.purge":
            raise ValueError(f"invalid retired disk path: {archive}")
        datetime.strptime(match[1], STAMP_FORMAT)
        if not resuming and archive.exists():
            raise ValueError(f"retired disk path already exists: {archive}")
        if source.exists() and archive.exists():
            raise ValueError(f"both original and retired disks exist: {source}, {archive}")
        if not source.is_file() and not archive.is_file():
            raise ValueError(f"disk is missing or is not a regular file: {source}")
        if (source.exists() and not source.is_file()) or (archive.exists() and not archive.is_file()):
            raise ValueError(f"disk is not a regular file: {source}")
        all_paths.extend((source, archive))
        if source.exists():
            moves.append((source, archive))
    if len(set(all_paths)) != len(all_paths):
        raise ValueError("duplicate disk paths in local deletion journal")
    attached = _attached_paths(config, exclude=name)
    shared = [str(path) for path in all_paths if path.resolve() in attached]
    if shared:
        raise ValueError("cannot retire disks used by another local VM: " + ", ".join(shared))
    seed = data.get("seed")
    if seed is not None:
        if seed != str(directory / f"{name}-seed.iso") or Path(seed).is_symlink():
            raise ValueError("invalid managed cloud-init seed path")
        if Path(seed).resolve() in attached:
            raise ValueError("cloud-init seed is used by another local VM")
    if cloud_dir.is_symlink() or (cloud_dir.exists() and not cloud_dir.is_dir()):
        raise ValueError(f"invalid cloud-init state directory: {cloud_dir}")
    undefine = ["undefine", name]
    if root is not None and (root.find("./os/nvram") is not None or root.find("./os[@firmware='efi']") is not None):
        undefine.append("--nvram")
    if dry_run:
        if exists:
            print("WOULD " + " ".join(["virsh", "-c", config["host"]["libvirt_uri"], *undefine]))
        for source, archive in moves:
            print(f"WOULD RENAME {source} -> {archive}")
        if seed:
            print(f"WOULD REMOVE generated cloud-init seed {seed}")
        if cloud_dir.exists():
            print(f"WOULD REMOVE generated cloud-init state {cloud_dir}")
        print(f"WOULD REMOVE managed XML and deletion state for {name}")
        return 0
    if not journal.exists():
        _save_journal(journal, data)
    if exists:
        _virsh(config, *undefine)
    for source, archive in moves:
        source.rename(archive)
        print(f"Retired disk: {source} -> {archive}")
    if seed:
        Path(seed).unlink(missing_ok=True)
    if cloud_dir.exists():
        shutil.rmtree(cloud_dir)
    xml_path.unlink(missing_ok=True)
    pending.unlink(missing_ok=True)
    journal.unlink()
    print(f"Deleted local VM {name}; retired disks kept until vmctl purge")
    return 0


def purge_local_disks(config: dict, project_dir: Path, dry_run: bool) -> int:
    """Remove only regular, dated .purge files in the configured disk directory."""
    directory = _storage(config)
    candidates = []
    for path in sorted(directory.iterdir()):
        match = ARCHIVE_RE.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        try:
            datetime.strptime(match[1], STAMP_FORMAT)
        except ValueError:
            continue
        candidates.append(path)
    if not candidates:
        print("No retired .purge disks in storage.directory")
        return 0
    # A partial delete must be completed before its retired disks can be purged.
    protected = set()
    for journal in (project_dir / "state" / "local-delete").glob("*.json"):
        if journal.is_symlink():
            raise ValueError(f"invalid local deletion journal: {journal}")
        data = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("disks"), list):
            raise ValueError(f"invalid local deletion journal: {journal}")
        for entry in data["disks"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("archive"), str):
                raise ValueError(f"invalid disk entry in local deletion journal: {journal}")
            protected.add(Path(entry["archive"]).resolve())
    attached = _attached_paths(config)
    blocked = [str(path) for path in candidates if path.resolve() in attached or path.resolve() in protected]
    if blocked:
        raise ValueError("retired disks are attached or belong to an unfinished delete local: " + ", ".join(blocked))
    for path in candidates:
        print(f"{'WOULD PURGE' if dry_run else 'PURGE'} {path}")
        if not dry_run:
            path.unlink()
    return 0
