import contextlib
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import netbox
import inventory
import vmctl
from vmctl import load_config
from vmcreate import create_vm, domain_xml, validate_spec, verify_local_vm


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.vm = validate_spec({"vm": {
            "name": "test-vm", "source": "iso", "memory_mb": 2048,
            "vcpus": 2, "disk_gb": 20, "iso": "/images/installer.iso",
        }})

    def test_iso_xml_boots_installer_and_uses_configured_bridge(self):
        root = ET.fromstring(domain_xml(self.vm, Path("/images/test-vm.qcow2"), "br1"))
        self.assertEqual([item.attrib["dev"] for item in root.findall("./os/boot")], ["cdrom", "hd"])
        self.assertEqual(root.find("./devices/interface/source").attrib["bridge"], "br1")
        self.assertEqual(root.find("./devices/graphics").attrib["listen"], "127.0.0.1")

    def test_cloud_xml_boots_disk_and_attaches_seed(self):
        vm = dict(self.vm, source="cloud_image", image="/images/base.img", user_data="/user-data")
        root = ET.fromstring(domain_xml(vm, Path("/images/test-vm.qcow2"), "br1", Path("/images/seed.iso")))
        self.assertEqual([item.attrib["dev"] for item in root.findall("./os/boot")], ["hd"])
        self.assertEqual(root.find("./devices/disk[@device='cdrom']/source").attrib["file"], "/images/seed.iso")

    def test_create_refuses_to_overwrite_existing_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "installer.iso").touch()
            (path / "test-vm.qcow2").touch()
            vm = dict(self.vm, iso=str(path / "installer.iso"))
            vm["storage_directory"] = directory
            vm["bridge"] = "lo"
            config = {
                "host": {"libvirt_uri": "qemu:///system"},
            }
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "already exist"):
                    create_vm(vm, config, path, False)

    def test_sync_detects_local_drift(self):
        vm = dict(self.vm, storage_directory="/images", bridge="br1")
        xml = domain_xml(vm, Path("/images/test-vm.qcow2"), "br2")
        config = {"host": {"libvirt_uri": "qemu:///system"}}
        with patch("vmcreate.subprocess.run", return_value=SimpleNamespace(stdout=xml)):
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_local_vm(vm, config)

    def test_sync_accepts_matching_local_vm(self):
        vm = dict(self.vm, storage_directory="/images", bridge="br1")
        xml = domain_xml(vm, Path("/images/test-vm.qcow2"), "br1")
        config = {"host": {"libvirt_uri": "qemu:///system"}}
        with patch("vmcreate.subprocess.run", return_value=SimpleNamespace(stdout=xml)):
            verify_local_vm(vm, config)

    def test_inspection_reads_disk_capacity_and_power(self):
        vm = dict(self.vm, storage_directory="/images", bridge="br1")
        xml = domain_xml(vm, Path("/images/test-vm.qcow2"), "br1")
        def command(args, **kwargs):
            if "dumpxml" in args:
                return SimpleNamespace(stdout=xml)
            if "domblkinfo" in args:
                return SimpleNamespace(stdout="Capacity: 21474836480\n")
            if "domstate" in args:
                return SimpleNamespace(stdout="running\n")
            return SimpleNamespace(stdout="Autostart: enable\n")
        with patch("inventory.subprocess.run", side_effect=command):
            observed = inventory.inspect_vm({"host": {"libvirt_uri": "qemu:///system"}}, "test-vm")
        self.assertEqual(observed["disk_mb"], 20480)
        self.assertEqual(observed["status"], "active")
        self.assertTrue(observed["autostart"])


class NetBoxTests(unittest.TestCase):
    def test_paginated_inventory_keeps_only_host_vms(self):
        config = {"netbox": {"url": "https://netbox.example", "key": "secret"}}
        with (
            patch("netbox._settings", return_value=("https://netbox.example", "secret")),
            patch("netbox._request", side_effect=[
                {"results": [{"name": "a", "device": {"id": 12}}],
                 "next": "https://netbox.example/api/virtualization/virtual-machines/?offset=100"},
                {"results": [{"name": "b", "device": {"id": 12}},
                             {"name": "foreign", "device": {"id": 99}}], "next": None},
            ]),
        ):
            self.assertEqual([vm["name"] for vm in netbox.list_vms(config, 12)], ["a", "b"])

    def test_cluster_is_derived_from_device(self):
        config = {"netbox": {"device": "hypervisor-01"}}
        with patch("netbox._request", return_value={"results": [
            {"id": 12, "name": "hypervisor-01", "cluster": {"id": 7}},
        ]}) as request:
            self.assertEqual(netbox.find_device(config), (12, 7))
        self.assertEqual(request.call_args.args[2], "dcim/devices/?name=hypervisor-01")

    def test_device_without_cluster_is_rejected(self):
        config = {"netbox": {"device": 12}}
        with patch("netbox._request", return_value={"id": 12, "cluster": None}):
            with self.assertRaisesRegex(netbox.NetBoxError, "not assigned"):
                netbox.find_device(config)

    def test_vm_is_planned_in_netbox_before_local_creation(self):
        vm = {
            "name": "test-vm", "source": "iso", "iso": "/images/installer.iso",
            "vcpus": 2, "memory_mb": 2048, "disk_gb": 20,
        }
        config = {
            "storage": {"directory": "/images"}, "network": {"bridge": "br1"},
        }
        with patch("netbox._request", side_effect=[{"results": []}, {"id": 42}]) as request:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(netbox.reserve_vm(config, vm, 7, 12)["id"], 42)
        self.assertEqual(request.call_args_list[1].args[3], {
            "name": "test-vm", "cluster": 7, "device": 12, "status": "planned", "start_on_boot": "off",
            "vcpus": 2, "memory": 2048, "disk": 20480,
            "local_context_data": {"vmctl": {
                "version": 1, "source": "iso", "iso": "/images/installer.iso",
                "image": None, "user_data": None, "bridge": "br1",
                "storage_directory": "/images",
            }},
        })

    def test_local_sizing_and_install_source_come_from_netbox(self):
        record = {
            "name": "test-vm", "vcpus": "4.00", "memory": 4096, "disk": 32768,
            "local_context_data": {"vmctl": {
                "version": 1, "source": "iso", "iso": "/images/install.iso",
                "bridge": "br1", "storage_directory": "/images",
            }},
        }
        vm = netbox.local_spec_from_netbox(record)
        self.assertEqual((vm["vcpus"], vm["memory_mb"], vm["disk_gb"]), (4, 4096, 32))
        self.assertEqual(vm["iso"], "/images/install.iso")

    def test_config_with_key_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[host]\nlibvirt_uri="qemu:///system"\n'
                '[storage]\ndirectory="/images"\n'
                '[network]\nbridge="br1"\n'
                '[netbox]\nkey="secret"\n', encoding="utf-8",
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                load_config(path)
            path.chmod(0o600)
            self.assertEqual(load_config(path)["netbox"]["key"], "secret")


class OperationOrderTests(unittest.TestCase):
    def test_keyless_create_is_local_only(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "vm.toml"
            spec.write_text('[vm]\nname="test-vm"\nsource="iso"\nmemory_mb=2048\n'
                            'vcpus=2\ndisk_gb=20\niso="/images/install.iso"\n')
            args = Namespace(command="create", spec=spec, config=spec, dry_run=False)
            config = {"host": {"libvirt_uri": "qemu:///system"},
                      "network": {"bridge": "br1"}, "storage": {"directory": "/images"}}
            with (
                patch("vmctl.parse_args", return_value=args),
                patch("vmctl.load_config", return_value=config),
                patch("vmctl.create_vm", return_value=0) as local,
                patch("vmctl.find_device") as device,
            ):
                self.assertEqual(vmctl.main(), 0)
            local.assert_called_once()
            device.assert_not_called()

    def test_keyless_start_never_contacts_netbox(self):
        args = Namespace(command="start", vm="test-vm", config=Path("config.toml"), dry_run=False)
        config = {"host": {"libvirt_uri": "qemu:///system"}}
        with (
            patch("vmctl.parse_args", return_value=args),
            patch("vmctl.load_config", return_value=config),
            patch("vmctl.find_device") as device,
            patch("vmctl.subprocess.run", return_value=SimpleNamespace(returncode=0)) as local,
        ):
            self.assertEqual(vmctl.main(), 0)
        device.assert_not_called()
        self.assertEqual(local.call_args.args[0][-2:], ["start", "test-vm"])

    def test_adopt_requires_existing_netbox_host_before_inspection(self):
        config = {"netbox": {"key": "secret"}}
        with (
            patch("vmctl.find_device", side_effect=netbox.NetBoxError("missing host")),
            patch("vmctl.local_names") as names,
        ):
            with self.assertRaisesRegex(netbox.NetBoxError, "missing host"):
                vmctl.adopt(config, False)
            names.assert_not_called()

    def test_adopt_imports_only_missing_vms_and_is_read_only_locally(self):
        config = {"netbox": {"key": "secret"}}
        vm = {"name": "new", "vcpus": 2, "memory_mb": 2048, "disk_mb": 10240,
              "disk_paths": ["/disk.qcow2"], "bridge": "br1", "status": "active", "autostart": True}
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[{"id": 4, "name": "old"}]),
            patch("vmctl.local_names", return_value=["new", "old"]),
            patch("vmctl.find_vm", return_value=None),
            patch("vmctl.inspect_vm", return_value=vm),
            patch("vmctl.import_vm", return_value={"id": 5}) as imported,
            patch("vmctl.subprocess.run") as local_change,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(vmctl.adopt(config, False), 0)
        self.assertEqual(imported.call_args.args[1]["name"], "new")
        local_change.assert_not_called()

    def test_audit_reports_missing_and_different_vms(self):
        config = {"netbox": {"key": "secret"}}
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[{"name": "remote", "device": {"id": 12}}]),
            patch("vmctl.local_names", return_value=["local"]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(vmctl.audit(config), 1)
        self.assertIn("MISSING NETBOX: local", output.getvalue())
        self.assertIn("MISSING LOCAL: remote", output.getvalue())

    def test_reconcile_power_writes_netbox_before_start(self):
        order = []
        config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
        record = {"id": 42, "name": "test-vm"}

        def local(command, **kwargs):
            if "domstate" in command:
                return SimpleNamespace(stdout="shut off\n")
            order.append("virsh")
            return SimpleNamespace(returncode=0)

        with (
            patch("vmctl.subprocess.run", side_effect=local),
            patch("vmctl.patch_vm", side_effect=lambda *a: order.append("netbox")),
        ):
            vmctl.reconcile_power(config, record, "active")
        self.assertEqual(order, ["netbox", "virsh"])

    def test_create_writes_netbox_before_local_vm(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "vm.toml"
            spec.write_text(
                '[vm]\nname="test-vm"\nsource="iso"\nmemory_mb=2048\n'
                'vcpus=2\ndisk_gb=20\niso="/images/install.iso"\n', encoding="utf-8",
            )
            order = []
            local_spec = {
                "name": "test-vm", "source": "iso", "memory_mb": 2048,
                "vcpus": 2, "disk_gb": 20, "iso": "/images/install.iso",
                "bridge": "br1", "storage_directory": "/images",
            }
            args = Namespace(command="create", spec=spec, config=spec, dry_run=False)
            config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
            with (
                patch("vmctl.parse_args", return_value=args),
                patch("vmctl.load_config", return_value=config),
                patch("vmctl.find_device", return_value=(12, 7)),
                patch("vmctl.reserve_vm", side_effect=lambda *a: order.append("netbox-create")),
                patch("vmctl.get_vm", return_value={"id": 42, "status": {"value": "planned"}}),
                patch("vmctl.local_spec_from_netbox", return_value=local_spec),
                patch("vmctl.create_vm", side_effect=lambda *a: order.append("local-create")),
                patch("vmctl.patch_vm", side_effect=lambda *a: order.append("netbox-staged")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(vmctl.main(), 0)
            self.assertEqual(order, ["netbox-create", "local-create", "netbox-staged"])

    def test_start_updates_netbox_before_virsh(self):
        order = []
        args = Namespace(command="start", vm="test-vm", config=Path("config.toml"), dry_run=False)
        config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
        with (
            patch("vmctl.parse_args", return_value=args),
            patch("vmctl.load_config", return_value=config),
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.get_vm", return_value={"id": 42}),
            patch("vmctl.patch_vm", side_effect=lambda *a: order.append("netbox")),
            patch("vmctl.subprocess.run", side_effect=lambda *a, **kw: (order.append("virsh") or SimpleNamespace(returncode=0))),
        ):
            self.assertEqual(vmctl.main(), 0)
        self.assertEqual(order, ["netbox", "virsh"])

    def test_sync_updates_netbox_before_local_create(self):
        order = []
        args = Namespace(command="sync", vm="test-vm", config=Path("config.toml"), dry_run=False)
        config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
        local_spec = {
            "name": "test-vm", "source": "iso", "memory_mb": 2048,
            "vcpus": 2, "disk_gb": 20, "iso": "/images/install.iso",
            "bridge": "br1", "storage_directory": "/images",
        }
        with (
            patch("vmctl.parse_args", return_value=args),
            patch("vmctl.load_config", return_value=config),
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.get_vm", return_value={"id": 42, "status": {"value": "planned"}}),
            patch("vmctl.local_spec_from_netbox", return_value=local_spec),
            patch("vmctl.subprocess.run", return_value=SimpleNamespace(stdout="", returncode=0)),
            patch("vmctl.patch_vm", side_effect=lambda *a: order.append("netbox")),
            patch("vmctl.create_vm", side_effect=lambda *a: order.append("local")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(vmctl.main(), 0)
        self.assertEqual(order, ["netbox", "local", "netbox"])

    def test_start_does_not_run_virsh_when_netbox_fails(self):
        args = Namespace(command="start", vm="test-vm", config=Path("config.toml"), dry_run=False)
        config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
        with (
            patch("vmctl.parse_args", return_value=args),
            patch("vmctl.load_config", return_value=config),
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.get_vm", return_value={"id": 42}),
            patch("vmctl.patch_vm", side_effect=netbox.NetBoxError("unavailable")),
            patch("vmctl.subprocess.run") as local,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(vmctl.main(), 1)
            local.assert_not_called()

    def test_create_does_not_change_libvirt_when_netbox_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "vm.toml"
            spec.write_text(
                '[vm]\nname="test-vm"\nsource="iso"\nmemory_mb=2048\n'
                'vcpus=2\ndisk_gb=20\niso="/images/install.iso"\n', encoding="utf-8",
            )
            args = Namespace(command="create", spec=spec, config=spec, dry_run=False)
            with (
                patch("vmctl.parse_args", return_value=args),
                patch("vmctl.load_config", return_value={"netbox": {"key": "secret"}}),
                patch("vmctl.find_device", return_value=(12, 7)),
                patch("vmctl.reserve_vm", side_effect=netbox.NetBoxError("unavailable")),
                patch("vmctl.create_vm") as local,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(vmctl.main(), 1)
                local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
