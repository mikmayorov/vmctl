import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import netbox
import vmctl
import vmcreate
import vmdelete


class LocalDeletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = self.root / "images"
        self.storage.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.disk = self.storage / "guest.qcow2"
        self.data = self.storage / "guest-data.qcow2"
        self.disk.write_bytes(b"root-data")
        self.data.write_bytes(b"additional-data")
        self.seed = self.storage / "guest-seed.iso"
        self.seed.write_bytes(b"seed")
        self.cloud_dir = self.project / "state/cloud-init/guest"
        self.cloud_dir.mkdir(parents=True)
        (self.cloud_dir / "user-data").write_text("keys")
        self.xml_path = self.project / "state/domains/guest.xml"
        self.xml_path.parent.mkdir(parents=True)
        self.xml = self.domain_xml("guest", [self.disk, self.data], self.seed)
        self.xml_path.write_text(self.xml)
        self.domains = {"guest": self.xml}
        self.live = {}
        self.running = set()
        self.commands = []
        self.config = {"host": {"libvirt_uri": "qemu:///system"},
                       "storage": {"directory": str(self.storage)}, "netbox": {"key": "secret"}}
        self.run_patch = patch("vmdelete.subprocess.run", side_effect=self.virsh_run)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    @staticmethod
    def domain_xml(name, disks, seed=None):
        root = ET.Element("domain")
        ET.SubElement(root, "name").text = name
        ET.SubElement(root, "uuid").text = "00000000-0000-4000-8000-000000000001"
        os = ET.SubElement(root, "os", {"firmware": "efi"})
        ET.SubElement(os, "nvram").text = "/var/lib/libvirt/qemu/nvram/guest.fd"
        devices = ET.SubElement(root, "devices")
        for path in disks:
            disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
            ET.SubElement(disk, "source", {"file": str(path)})
        if seed:
            disk = ET.SubElement(devices, "disk", {"type": "file", "device": "cdrom"})
            ET.SubElement(disk, "source", {"file": str(seed)})
        return ET.tostring(root, encoding="unicode")

    def virsh_run(self, command, **kwargs):
        self.commands.append(command)
        if "list" in command:
            return SimpleNamespace(stdout="\n".join(self.domains))
        if "domstate" in command:
            return SimpleNamespace(stdout="running" if command[-1] in self.running else "shut off")
        if "dumpxml" in command:
            name = command[-1]
            return SimpleNamespace(stdout=self.domains[name] if "--inactive" in command
                                   else self.live.get(name, self.domains[name]))
        if "undefine" in command:
            del self.domains[command[command.index("undefine") + 1]]
            return SimpleNamespace(stdout="")
        raise AssertionError(command)

    def delete(self, dry_run=False):
        return vmdelete.delete_local_vm(self.config, "guest", self.project, dry_run)

    def test_local_delete_archives_disks_and_removes_generated_artifacts_only(self):
        template = self.root / "ubuntu.img"
        template.write_bytes(b"original-template")
        with patch("vmctl._request") as netbox_request:
            self.assertEqual(self.delete(), 0)
        netbox_request.assert_not_called()
        self.assertNotIn("guest", self.domains)
        self.assertFalse(self.disk.exists())
        self.assertFalse(self.data.exists())
        archives = list(self.storage.glob("*.purge"))
        self.assertEqual({path.read_bytes() for path in archives}, {b"root-data", b"additional-data"})
        for path in archives:
            match = vmdelete.ARCHIVE_RE.fullmatch(path.name)
            self.assertIsNotNone(match)
            datetime.strptime(match[1], vmdelete.STAMP_FORMAT)
        self.assertFalse(self.seed.exists())
        self.assertFalse(self.cloud_dir.exists())
        self.assertFalse(self.xml_path.exists())
        self.assertFalse(vmdelete.local_delete_file(self.project, "guest").exists())
        self.assertEqual(template.read_bytes(), b"original-template")
        self.assertIn("--nvram", next(command for command in self.commands if "undefine" in command))

    def test_local_delete_dry_run_has_no_disk_or_state_changes(self):
        self.assertEqual(self.delete(True), 0)
        self.assertIn("guest", self.domains)
        self.assertEqual(self.disk.read_bytes(), b"root-data")
        self.assertTrue(self.seed.exists())
        self.assertTrue(self.xml_path.exists())
        self.assertEqual(list(self.storage.glob("*.purge")), [])
        self.assertFalse(vmdelete.local_delete_file(self.project, "guest").exists())
        self.assertFalse(any("undefine" in command for command in self.commands))

    def test_bios_guest_keeps_installer_iso_and_does_not_remove_nvram(self):
        shutil.rmtree(self.cloud_dir)
        self.seed.unlink()
        installer = self.root / "installer.iso"
        installer.write_bytes(b"installer")
        root = ET.fromstring(self.xml)
        root.find("os").clear()
        root.find("./devices/disk[@device='cdrom']/source").set("file", str(installer))
        self.domains["guest"] = ET.tostring(root, encoding="unicode")
        self.delete()
        self.assertEqual(installer.read_bytes(), b"installer")
        self.assertNotIn("--nvram", next(command for command in self.commands if "undefine" in command))

    def test_running_guest_blocks_before_journal_or_renames(self):
        self.running.add("guest")
        with self.assertRaisesRegex(ValueError, "shut down"):
            self.delete()
        self.assertTrue(self.disk.exists())
        self.assertFalse(vmdelete.local_delete_file(self.project, "guest").exists())

    def test_shared_disk_in_next_start_or_live_xml_blocks_local_delete(self):
        other_disk = self.storage / "other.qcow2"
        other_disk.touch()
        for live in (False, True):
            with self.subTest(live=live):
                self.domains["other"] = self.domain_xml("other", [other_disk if live else self.disk])
                self.live["other"] = self.domain_xml("other", [self.disk if live else other_disk])
                with self.assertRaisesRegex(ValueError, "another local VM"):
                    self.delete()
                self.assertIn("guest", self.domains)
                self.assertTrue(self.disk.exists())

    def test_unmanaged_disk_is_rejected_before_undefine(self):
        outside = self.root / "external.qcow2"
        outside.touch()
        self.domains["guest"] = self.domain_xml("guest", [outside])
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            self.delete()
        self.assertIn("guest", self.domains)
        self.assertTrue(outside.exists())

    def test_partial_rename_can_resume_without_a_libvirt_guest(self):
        rename = Path.rename

        def fail_on_data(path, target):
            if path == self.data:
                raise OSError("rename failed")
            return rename(path, target)

        with patch.object(Path, "rename", fail_on_data):
            with self.assertRaisesRegex(OSError, "rename failed"):
                self.delete()
        journal = vmdelete.local_delete_file(self.project, "guest")
        self.assertTrue(journal.exists())
        self.assertNotIn("guest", self.domains)
        self.assertFalse(self.disk.exists())
        self.assertTrue(self.data.exists())
        with self.assertRaisesRegex(ValueError, "unfinished delete local"):
            vmdelete.purge_local_disks(self.config, self.project, False)
        with self.assertRaisesRegex(ValueError, "unfinished local deletion"):
            vmcreate.create_vm({"name": "guest"}, self.config, self.project, False)
        self.assertEqual(self.delete(), 0)
        self.assertEqual(len(list(self.storage.glob("*.purge"))), 2)
        self.assertFalse(journal.exists())
        self.assertFalse(self.cloud_dir.exists())

    def test_undefine_failure_does_not_rename_disks_and_can_be_retried(self):
        run = self.virsh_run

        def fail(command, **kwargs):
            if "undefine" in command:
                raise subprocess.CalledProcessError(1, command)
            return run(command, **kwargs)

        with patch("vmdelete.subprocess.run", side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                self.delete()
        self.assertTrue(self.disk.exists())
        self.assertTrue(self.data.exists())
        self.assertEqual(self.delete(), 0)

    def test_retry_rejects_a_different_vm_with_same_name(self):
        journal = vmdelete.local_delete_file(self.project, "guest")
        run = self.virsh_run

        def fail(command, **kwargs):
            if "undefine" in command:
                raise subprocess.CalledProcessError(1, command)
            return run(command, **kwargs)

        with patch("vmdelete.subprocess.run", side_effect=fail), self.assertRaises(subprocess.CalledProcessError):
            self.delete()
        self.assertTrue(journal.exists())
        self.domains["guest"] = self.xml.replace("000000000001", "000000000002")
        with self.assertRaisesRegex(ValueError, "changed since deletion"):
            self.delete()
        self.assertTrue(self.disk.exists())

    def test_purge_only_removes_dated_retired_files_and_dry_run_preserves_them(self):
        self.delete()
        normal = self.storage / "new.qcow2"
        normal.touch()
        arbitrary = self.storage / "file.purge"
        arbitrary.touch()
        invalid = self.storage / "file.20269999T123456123456Z.purge"
        invalid.touch()
        link = self.storage / "link.20261029T123456123456Z.purge"
        link.symlink_to(normal)
        self.assertEqual(vmdelete.purge_local_disks(self.config, self.project, True), 0)
        self.assertEqual(len(list(self.storage.glob("guest*.purge"))), 2)
        self.assertEqual(vmdelete.purge_local_disks(self.config, self.project, False), 0)
        self.assertEqual(list(self.storage.glob("guest*.purge")), [])
        for path in (normal, arbitrary, invalid, link):
            self.assertTrue(path.exists())

    def test_purge_rejects_an_archive_attached_to_another_vm(self):
        self.delete()
        archive = next(self.storage.glob("*.purge"))
        self.domains["restored"] = self.domain_xml("restored", [archive])
        with self.assertRaisesRegex(ValueError, "attached"):
            vmdelete.purge_local_disks(self.config, self.project, False)
        self.assertEqual(len(list(self.storage.glob("*.purge"))), 2)

    def test_pending_detached_disks_are_also_retired(self):
        detached = self.storage / "guest-detached.qcow2"
        detached.write_bytes(b"detached")
        pending = vmcreate.pending_purge_file(self.project, "guest")
        pending.parent.mkdir(parents=True)
        pending.write_text(json.dumps([str(detached)]))
        self.delete()
        self.assertFalse(detached.exists())
        self.assertEqual(len(list(self.storage.glob("*.purge"))), 3)
        self.assertFalse(pending.exists())


class DeleteCommandsTests(unittest.TestCase):
    def test_parser_requires_entity_and_has_separate_purge_without_local_flag(self):
        parser = vmctl.build_parser()
        self.assertEqual(parser.parse_args(["delete", "local", "guest"]).action, "local")
        self.assertEqual(parser.parse_args(["delete", "netbox", "guest"]).action, "netbox")
        self.assertEqual(parser.parse_args(["purge"]).command, "purge")
        for arguments in (["delete", "guest"], ["delete", "local", "guest", "--purge"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(arguments)

    def test_netbox_delete_never_contacts_libvirt_or_refreshes_local_links(self):
        args = Namespace(command="delete", action="netbox", vm="guest", dry_run=False, config=Path("config.toml"))
        with patch("vmctl.parse_args", return_value=args), \
                patch("vmctl.load_config", return_value={"netbox": {"key": "secret"}}), \
                patch("vmctl.find_device", return_value=(2, 3)), \
                patch("vmctl.get_vm", return_value={"id": 1}), \
                patch("vmctl.vm_interfaces", return_value=[]), \
                patch("vmctl._request") as request, patch("vmctl.subprocess.run") as local, \
                patch("vmctl.refresh_guest_links") as refresh, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vmctl.main(), 0)
        request.assert_called_once_with({"netbox": {"key": "secret"}}, "DELETE", "virtualization/virtual-machines/1/")
        local.assert_not_called()
        refresh.assert_not_called()

    def test_netbox_delete_preserves_assigned_ip_and_vm_by_stopping_before_delete(self):
        config = {"netbox": {"key": "secret"}}
        with patch("vmctl.find_device", return_value=(2, 3)), patch("vmctl.get_vm", return_value={"id": 1}), \
                patch("vmctl.vm_interfaces", return_value=[{"id": 10}]), \
                patch("vmctl.interface_ips", return_value=[{"id": 20}]), patch("vmctl._request") as request:
            with self.assertRaisesRegex(netbox.NetBoxError, "unassign"):
                vmctl.delete_vm(config, Namespace(action="netbox", vm="guest", dry_run=False))
        request.assert_not_called()

    def test_netbox_delete_dry_run_is_read_only_and_key_is_required(self):
        args = Namespace(action="netbox", vm="guest", dry_run=True)
        with patch("vmctl.find_device", return_value=(2, 3)), patch("vmctl.get_vm", return_value={"id": 1}), \
                patch("vmctl.vm_interfaces", return_value=[]), patch("vmctl._request") as request, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vmctl.delete_vm({"netbox": {"key": "secret"}}, args), 0)
        request.assert_not_called()
        with self.assertRaises(netbox.NetBoxError):
            vmctl.delete_vm({}, args)
