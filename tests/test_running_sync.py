import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import vmcreate


class RunningSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.disk = self.project / "mvm.qcow2"
        self.disk.touch()
        self.config = {"host": {"libvirt_uri": "qemu:///system"},
                       "storage": {"directory": str(self.project)}, "network": {"bridge": "br0"}}
        self.old = {"name": "mvm", "source": "cloud_image", "image": "/images-ro/ubuntu.img",
                    "memory_mb": 8000, "vcpus": 4, "disk_gb": 60,
                    "storage_directory": str(self.project), "bridge": "br0", "interfaces": []}
        self.vm = {**self.old, "vcpus": 8, "memory_mb": 16000}
        root = ET.fromstring(vmcreate.domain_xml(self.old, self.disk, "br0", self.project / "mvm-seed.iso"))
        ET.SubElement(root, "currentMemory", {"unit": "KiB"}).text = str(8000 * 1024)
        root.find("vcpu").set("current", "4")
        ET.SubElement(root.find("cpu"), "topology", {"sockets": "1", "cores": "4", "threads": "1"})
        self.xml = ET.tostring(root, encoding="unicode")
        self.commands = []
        self.state = "running"
        self.capacity = 60 * 1024**3
        self.extra = None

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        if "domstate" in command:
            return SimpleNamespace(stdout=self.state)
        if "dumpxml" in command:
            return SimpleNamespace(stdout=self.xml)
        if "domblkinfo" in command:
            return SimpleNamespace(stdout=f"Capacity: {self.capacity}\nAllocation: 1234\nPhysical: 1234\n")
        if command[:2] == ["qemu-img", "info"]:
            if self.extra is not None and command[-1] == str(self.extra):
                return SimpleNamespace(stdout=json.dumps({"format": "qcow2", "virtual-size": 20 * 1024**3}))
            raise subprocess.CalledProcessError(1, command, stderr="Failed to get shared 'write' lock")
        return SimpleNamespace(stdout="")

    def test_running_managed_disk_uses_libvirt_and_stages_all_startup_resources(self):
        for state, dry_run in (("running", True), ("running", False), ("paused", True)):
            with self.subTest(state=state, dry_run=dry_run):
                self.state = state
                self.commands = []
                with patch("vmcreate.subprocess.run", side_effect=self.run_command), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    vmcreate.redefine_vm(self.vm, self.config, self.project, dry_run, stage_running=True)
                self.assertTrue(any("domblkinfo" in command for command in self.commands))
                self.assertFalse(any(command[0] == "qemu-img" for command in self.commands))
                self.assertFalse(any("start" in command or "shutdown" in command or "setmem" in command
                                     for command in self.commands))
                if dry_run:
                    self.assertIn("NEXT START", output.getvalue())
                else:
                    saved = ET.parse(self.project / "state/domains/mvm.xml")
                    for tag in ("memory", "currentMemory"):
                        self.assertEqual(saved.find(tag).text, "16000")
                        self.assertEqual(saved.find(tag).get("unit"), "MiB")
                    self.assertEqual(saved.find("vcpu").text, "8")
                    self.assertEqual(saved.find("vcpu").get("current"), "8")
                    self.assertEqual(saved.find("./cpu/topology").get("cores"), "8")
                    self.assertIn("Staged persistent XML", output.getvalue())
                    with patch("vmcreate.subprocess.run", return_value=SimpleNamespace(
                            stdout=ET.tostring(saved.getroot(), encoding="unicode"))):
                        vmcreate.verify_local_vm(self.vm, self.config)

    def test_running_disk_capacity_mismatch_is_not_silently_ignored(self):
        self.capacity = 40 * 1024**3
        with patch("vmcreate.subprocess.run", side_effect=self.run_command):
            with self.assertRaisesRegex(ValueError, "resizing is unsupported"):
                vmcreate.redefine_vm(self.vm, self.config, self.project, False, stage_running=True)
        self.assertFalse(any("define" in command for command in self.commands))
        self.assertFalse((self.project / "state").exists())

    def test_running_guest_still_validates_an_unattached_existing_disk(self):
        self.extra = self.project / "mvm-data.qcow2"
        self.extra.touch()
        vm = {**self.vm, "disks": [{"name": "mvm", "path": str(self.disk), "size_gb": 60},
                                   {"name": "data", "path": str(self.extra), "size_gb": 20}]}
        with patch("vmcreate.subprocess.run", side_effect=self.run_command), contextlib.redirect_stdout(io.StringIO()):
            vmcreate.redefine_vm(vm, self.config, self.project, False, stage_running=True)
        info = [command for command in self.commands if command[:2] == ["qemu-img", "info"]]
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0][-1], str(self.extra))

    def test_offline_image_info_failure_includes_its_stderr(self):
        self.state = "shut off"
        with patch("vmcreate.subprocess.run", side_effect=self.run_command):
            with self.assertRaisesRegex(ValueError, "Failed to get shared 'write' lock"):
                vmcreate.redefine_vm(self.vm, self.config, self.project, False)
        self.assertFalse(any("define" in command for command in self.commands))

    def test_verifier_detects_stale_startup_current_memory_or_vcpus(self):
        for stale in ("memory", "vcpus"):
            with self.subTest(stale=stale):
                root = ET.fromstring(self.xml)
                root.find("memory").text = "16000"
                root.find("memory").set("unit", "MiB")
                root.find("currentMemory").text = "8000" if stale == "memory" else "16000"
                root.find("currentMemory").set("unit", "MiB")
                root.find("vcpu").text = "8"
                root.find("vcpu").set("current", "4" if stale == "vcpus" else "8")
                with patch("vmcreate.subprocess.run", return_value=SimpleNamespace(
                        stdout=ET.tostring(root, encoding="unicode"))):
                    with self.assertRaisesRegex(ValueError, "differs"):
                        vmcreate.verify_local_vm(self.vm, self.config)

    def test_preserves_explicit_socket_and_thread_layout_when_increasing_vcpus(self):
        root = ET.fromstring(self.xml)
        topology = root.find("./cpu/topology")
        topology.set("sockets", "2")
        topology.set("threads", "2")
        topology.set("cores", "1")
        self.xml = ET.tostring(root, encoding="unicode")
        with patch("vmcreate.subprocess.run", side_effect=self.run_command), contextlib.redirect_stdout(io.StringIO()):
            vmcreate.redefine_vm(self.vm, self.config, self.project, False, stage_running=True)
        saved = ET.parse(self.project / "state/domains/mvm.xml").find("./cpu/topology")
        self.assertEqual((saved.get("sockets"), saved.get("threads"), saved.get("cores")), ("2", "2", "2"))
