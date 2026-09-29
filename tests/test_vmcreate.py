import contextlib
import io
import subprocess
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import netbox
import inventory
import vmctl
from vmctl import load_config, resolve_creation_spec
from vmcreate import create_vm, domain_xml, host_interface_name, validate_spec, verify_local_vm


class CreationTests(unittest.TestCase):
    def test_hardware_profile_supplies_creation_resources(self):
        config = {"hardware": {"small": {"vcpus": 2, "memory_mb": 2048, "disk_gb": 20}}}
        request = {"vm": {"name": "guest", "hardware": "small", "source": "iso", "iso": "/iso/install.iso"}}
        resolved = resolve_creation_spec(request, config)
        self.assertEqual((resolved["vcpus"], resolved["memory_mb"], resolved["disk_gb"]), (2, 2048, 20))

    def test_hardware_profile_rejects_ambiguous_resource_override(self):
        config = {"hardware": {"small": {"vcpus": 2, "memory_mb": 2048, "disk_gb": 20}}}
        request = {"vm": {"name": "guest", "hardware": "small", "vcpus": 4,
                          "source": "iso", "iso": "/iso/install.iso"}}
        with self.assertRaisesRegex(ValueError, "vm.hardware supplies vcpus"):
            resolve_creation_spec(request, config)

    def test_software_profile_is_reserved(self):
        request = {"vm": {"name": "guest", "software": "ubuntu", "source": "iso",
                          "iso": "/iso/install.iso", "vcpus": 2, "memory_mb": 2048, "disk_gb": 20}}
        with self.assertRaisesRegex(ValueError, "not available in this release"):
            resolve_creation_spec(request, {})

    def test_vmctl_entrypoint_runs_through_installed_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "vmctl"
            link.symlink_to(Path(__file__).resolve().parents[1] / "vmctl")
            result = subprocess.run([str(link), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: vmctl", result.stdout)

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

    def test_netbox_description_mac_and_display_reach_libvirt_xml(self):
        vm = dict(self.vm, description="Service VM", uuid="00000000-0000-0000-0000-000000000001",
                  mac_address="52:54:00:12:34:56",
                  display={"type": "spice", "listen": "192.0.2.10", "port": 5930, "password": "example-secret"})
        validate_spec({"vm": vm})
        root = ET.fromstring(domain_xml(vm, Path("/images/test-vm.qcow2"), "br1"))
        self.assertEqual(root.findtext("./description"), "Service VM")
        self.assertEqual(root.findtext("./uuid"), vm["uuid"])
        self.assertEqual(root.find("./devices/interface/mac").get("address"), "52:54:00:12:34:56")
        self.assertEqual(root.find("./devices/graphics").attrib, {
            "type": "spice", "listen": "192.0.2.10", "autoport": "no",
            "port": "5930", "passwd": "example-secret",
        })

    def test_nonlocal_display_requires_password(self):
        vm = dict(self.vm, display={"type": "vnc", "listen": "192.0.2.10", "port": 5901})
        with self.assertRaisesRegex(ValueError, "password is required"):
            validate_spec({"vm": vm})

    def test_vnc_rejects_password_longer_than_libvirt_limit(self):
        vm = dict(self.vm, display={"type": "vnc", "listen": "127.0.0.1", "port": 5999,
                                    "password": "123456789"})
        with self.assertRaisesRegex(ValueError, "at most 8"):
            validate_spec({"vm": vm})

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
            with (
                patch("vmcreate.subprocess.run", return_value=SimpleNamespace(stdout='{"format":"raw","virtual-size":1}')),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(ValueError, "existing disk differs"):
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
            if "vcpucount" in args:
                return SimpleNamespace(stdout="3\n")
            if "dommemstat" in args:
                return SimpleNamespace(stdout="actual 1048576\n")
            return SimpleNamespace(stdout="Autostart: enable\n")
        with patch("inventory.subprocess.run", side_effect=command):
            observed = inventory.inspect_vm({"host": {"libvirt_uri": "qemu:///system"}}, "test-vm")
        self.assertEqual(observed["disk_mb"], 20480)
        self.assertEqual(observed["mounted_media"], [{"path": "/images/installer.iso", "target": "sda", "size_mb": 20480}])
        self.assertEqual(observed["status"], "active")
        self.assertEqual(observed["vcpus"], 3)
        self.assertEqual(observed["memory_bytes"], 1024 * 1024**2)
        self.assertTrue(observed["autostart"])

    def test_persistent_definition_uses_current_memory_and_cpu(self):
        xml = """<domain><name>guest</name><memory unit="MiB">4096</memory>
        <currentMemory unit="MiB">3072</currentMemory><vcpu current="3">4</vcpu>
        <devices><disk device="disk"><source file="/disk.qcow2"/><target dev="vda"/></disk>
        <interface type="bridge"><source bridge="br0"/><mac address="52:54:00:00:00:01"/>
        <target dev="vm-guest-123456"/></interface></devices></domain>"""
        with patch("inventory._virsh", return_value=xml) as virsh:
            observed = inventory.inspect_definition({"host": {"libvirt_uri": "qemu:///system"}}, "guest")
        self.assertEqual((observed["memory_mb"], observed["vcpus"]), (3072, 3))
        self.assertEqual(observed["disks"], [{"target": "vda", "path": "/disk.qcow2"}])
        self.assertIn("--inactive", virsh.call_args.args)

    def test_persistent_definition_ignores_empty_cdrom(self):
        xml = """<domain><memory unit="MiB">2048</memory><vcpu>2</vcpu><devices>
        <disk device="cdrom"><target dev="sda"/></disk></devices></domain>"""
        with patch("inventory._virsh", return_value=xml):
            observed = inventory.inspect_definition({"host": {"libvirt_uri": "qemu:///system"}}, "guest")
        self.assertEqual(observed["mounted_media"], [])

    def test_stopped_inventory_uses_startup_cpu_and_memory(self):
        xml = """<domain><memory unit="MiB">4096</memory>
        <currentMemory unit="MiB">3072</currentMemory><vcpu current="3">4</vcpu>
        <devices/></domain>"""
        def command(*args):
            if "dumpxml" in args:
                return xml
            if "domstate" in args:
                return "shut off"
            return "Autostart: disable"
        with patch("inventory._virsh", side_effect=command):
            observed = inventory.inspect_vm({"host": {"libvirt_uri": "qemu:///system"}}, "guest")
        self.assertEqual((observed["vcpus"], observed["memory_mb"]), (3, 3072))


class NetBoxTests(unittest.TestCase):
    def test_token_file_enables_netbox_and_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "netbox.key"
            key.write_text("nbt_example\n", encoding="utf-8")
            config = {"netbox": {"url": "https://netbox.example.org", "key": ""}}
            with patch.object(netbox, "KEY_FILE", key):
                key.chmod(0o644)
                self.assertTrue(netbox.has_netbox_key(config))
                with self.assertRaisesRegex(netbox.NetBoxError, "chmod 600"):
                    netbox._settings(config)
                key.chmod(0o600)
                self.assertEqual(netbox._settings(config), ("https://netbox.example.org", "nbt_example"))

    def test_same_device_name_is_disambiguated_by_site(self):
        devices = {"results": [
            {"id": 12, "name": "hypervisor-01", "site": {"slug": "site-a"}, "cluster": {"id": 7}},
            {"id": 13, "name": "hypervisor-01", "site": {"slug": "site-b"}, "cluster": {"id": 8}},
        ]}
        with patch("netbox._request", return_value=devices):
            with self.assertRaisesRegex(netbox.NetBoxError, "netbox.site"):
                netbox.find_device({"netbox": {"device": "hypervisor-01"}})
            self.assertEqual(
                netbox.find_device({"netbox": {"device": "hypervisor-01", "site": "site-b"}}),
                (13, 8),
            )

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
        serial = "00000000-0000-4000-8000-000000000001"
        with patch("netbox.uuid.uuid4", return_value=uuid.UUID(serial)), \
             patch("netbox._request", side_effect=[{"results": []}, {"id": 42}]) as request:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(netbox.reserve_vm(config, vm, 7, 12)["id"], 42)
        self.assertEqual(request.call_args_list[1].args[3], {
            "name": "test-vm", "serial": serial, "cluster": 7, "device": 12, "status": "planned", "start_on_boot": "off",
            "vcpus": 2, "memory": 2048, "disk": 20480, "description": "",
            "local_context_data": {"vmctl": {
                "version": 4, "source": "iso", "iso": "/images/installer.iso",
                "image": None, "user_data": None,
                "storage_directory": "/images", "interfaces": {
                    "eth0": {"host_dev": host_interface_name("test-vm", "eth0"), "bridge": "br1"}},
                "mounted_media": [{"name": "media-sda", "target": "sda"}],
                "display": {"type": "vnc", "listen": "127.0.0.1", "port": "auto"},
            }},
        })

    def test_local_sizing_and_install_source_come_from_netbox(self):
        record = {
            "name": "test-vm", "vcpus": "4.00", "memory": 4096, "disk": 32768,
            "serial": "00000000-0000-4000-8000-000000000002",
            "local_context_data": {"vmctl": {
                "version": 1, "source": "iso", "iso": "/images/install.iso",
                "bridge": "br1", "storage_directory": "/images",
            }},
        }
        vm = netbox.local_spec_from_netbox(record)
        self.assertEqual((vm["vcpus"], vm["memory_mb"], vm["disk_gb"]), (4, 4096, 32))
        self.assertEqual(vm["iso"], "/images/install.iso")
        self.assertEqual(vm["uuid"], record["serial"])

    def test_new_vm_reads_disk_mac_and_primary_ips_from_netbox(self):
        record = {
            "id": 42, "name": "test-vm", "vcpus": 2, "memory": 2048, "disk": 20480,
            "description": "Service VM", "primary_ip4": {"id": 71}, "primary_ip6": {"id": 72},
            "local_context_data": {"vmctl": {
                "version": 2, "source": "iso", "iso": "/images/install.iso",
                "bridge": "br1", "storage_directory": "/images", "interface_name": "inet",
                "display": {"type": "vnc", "listen": "127.0.0.1", "port": "auto"},
            }},
        }
        with (
            patch("netbox.vm_interfaces", return_value=[{"id": 8, "name": "inet", "primary_mac_address": {"mac_address": "52:54:00:12:34:56"}}]),
            patch("netbox.vm_disks", return_value=[{"name": "test-vm", "size": 20480}]),
            patch("netbox.interface_ips", return_value=[{"id": 71}, {"id": 72}]),
        ):
            vm = netbox.local_spec_from_netbox(record, {})
        self.assertEqual(vm["mac_address"], "52:54:00:12:34:56")
        self.assertEqual(vm["description"], "Service VM")

    def test_context_maps_guest_interface_to_host_tap_and_bridge(self):
        tap = host_interface_name("test-vm", "enp1s0")
        record = {
            "id": 42, "name": "test-vm", "vcpus": 2, "memory": 2048, "disk": 20480,
            "serial": "00000000-0000-4000-8000-000000000002",
            "primary_ip4": {"id": 71},
            "local_context_data": {"vmctl": {
                "version": 4, "source": "existing",
                "interfaces": {"enp1s0": {"host_dev": tap, "bridge": "br1"}},
                "display": {"type": "none"},
            }},
        }
        with (patch("netbox.vm_interfaces", return_value=[{
                  "id": 8, "name": "enp1s0",
                  "primary_mac_address": {"mac_address": "52:54:00:12:34:56"}}]),
              patch("netbox.vm_disks", return_value=[{
                  "name": "test-vm", "size": 20480, "description": "/images/test-vm.qcow2"}]),
              patch("netbox.interface_ips", return_value=[{"id": 71}])):
            spec = netbox.local_spec_from_netbox(record, {"storage": {"directory": "/images"}})
        self.assertEqual(spec["interfaces"], [{
            "name": "enp1s0", "mac_address": "52:54:00:12:34:56",
            "host_dev": tap, "bridge": "br1"}])
        xml = ET.fromstring(domain_xml(spec, Path("/images/test-vm.qcow2"), "br1"))
        self.assertEqual(xml.find("./devices/interface/target").get("dev"), tap)
        self.assertEqual(xml.find("./devices/interface/source").get("bridge"), "br1")

    def test_prepare_creates_guest_named_interface_for_context_v4(self):
        record = {"id": 42, "name": "test-vm", "disk": 20480,
                  "local_context_data": {"vmctl": {
                      "version": 4, "source": "cloud_image",
                      "interfaces": {"eth0": {
                          "host_dev": host_interface_name("test-vm", "eth0"), "bridge": "br1"}}}}}
        requests = []
        def request(_config, method, path, payload=None):
            requests.append((method, path, payload))
            return {"id": 8, "name": "eth0"}
        with (patch("netbox.vm_disks", return_value=[]),
              patch("netbox.vm_interfaces", return_value=[]),
              patch("netbox._request", side_effect=request),
              patch("netbox.ensure_primary_mac") as mac):
            netbox.create_vm_components({"storage": {"directory": "/images"}}, record, "eth0")
        self.assertIn(("POST", "virtualization/interfaces/", {
            "virtual_machine": 42, "name": "eth0", "enabled": True,
            "description": f"Host interface: {host_interface_name('test-vm', 'eth0')}; bridge: br1"}), requests)
        mac.assert_called_once()

    def test_components_are_created_before_local_vm(self):
        record = {"id": 42, "name": "test-vm", "disk": 20480}
        calls = []

        def request(config, method, path, payload=None):
            calls.append((method, path, payload))
            if path == "virtualization/virtual-disks/":
                return {"id": 3}
            if path == "virtualization/interfaces/":
                return {"id": 8, "name": "inet"}
            if path == "dcim/mac-addresses/":
                return {"id": 9}
            return {}

        with (
            patch("netbox.vm_disks", return_value=[]),
            patch("netbox.vm_interfaces", return_value=[]),
            patch("netbox._request", side_effect=request),
        ):
            netbox.create_vm_components({"storage": {"directory": "/images"}}, record, mac="52:54:00:12:34:56")
        self.assertEqual([item[1].split("?")[0] for item in calls], [
            "virtualization/virtual-disks/", "virtualization/interfaces/",
            "dcim/mac-addresses/", "dcim/mac-addresses/", "virtualization/interfaces/8/",
        ])
        self.assertEqual(calls[3][2]["assigned_object_type"], "virtualization.vminterface")
        self.assertEqual(calls[0][2]["description"], "/images/test-vm.qcow2")
        self.assertEqual(next(payload["name"] for method, path, payload in calls
                              if path == "virtualization/interfaces/"), host_interface_name("test-vm", "inet"))

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

    def test_adopt_reconciles_existing_and_imports_missing_without_local_mutation(self):
        config = {"netbox": {"key": "secret"}}
        vm = {"name": "new", "vcpus": 2, "memory_mb": 2048, "disk_mb": 10240,
              "disk_paths": ["/disk.qcow2"], "bridge": "br1", "status": "active", "autostart": True}
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[{"id": 4, "name": "old"}]),
            patch("vmctl.local_names", return_value=["new", "old"]),
            patch("vmctl.find_vm", return_value=None),
            patch("vmctl.get_vm", return_value={"id": 4, "name": "old"}),
            patch("vmctl.inspect_vm", return_value=vm),
            patch("vmctl.import_vm", return_value={"id": 5}) as imported,
            patch("vmctl.reconcile_adopted_vm") as reconciled,
            patch("vmctl.subprocess.run") as local_change,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(vmctl.adopt(config, False), 0)
        self.assertEqual(imported.call_args.args[1]["name"], "new")
        reconciled.assert_called_once()
        local_change.assert_not_called()

    def test_adopt_named_vm_does_not_inspect_other_domains(self):
        config = {"netbox": {"key": "secret"}}
        vm = {"name": "chosen", "vcpus": 2, "memory_mb": 2048,
              "disks": [], "interfaces": [], "mounted_media": []}
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[]),
            patch("vmctl.local_names", return_value=["chosen", "other"]),
            patch("vmctl.find_vm", return_value=None),
            patch("vmctl.inspect_vm", return_value=vm) as inspect,
            patch("vmctl.import_vm", return_value={"id": 5}) as imported,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(vmctl.adopt(config, False, "chosen"), 0)
        inspect.assert_called_once_with(config, "chosen")
        imported.assert_called_once()

    def test_adopt_continues_after_one_vm_fails(self):
        config = {"netbox": {"key": "secret"}}
        vm = {"name": "good", "vcpus": 2, "memory_mb": 2048,
              "disks": [], "interfaces": [], "mounted_media": []}
        def inspect(_config, name):
            if name == "bad":
                raise ValueError("invalid local XML")
            return vm
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[]),
            patch("vmctl.local_names", return_value=["bad", "good"]),
            patch("vmctl.find_vm", return_value=None),
            patch("vmctl.inspect_vm", side_effect=inspect),
            patch("vmctl.import_vm", return_value={"id": 5}) as imported,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(vmctl.adopt(config, False), 1)
        imported.assert_called_once()
        self.assertIn("ERROR bad: invalid local XML", errors.getvalue())
        self.assertIn("Что сделать: Проверьте virsh dumpxml --inactive bad", errors.getvalue())
        self.assertIn("Adoption: 2 VM(s), 1 successful, 1 failure(s)", output.getvalue())

    def test_release_rejects_cloud_image_before_netbox_or_libvirt_write(self):
        request = {"vm": {"name": "guest", "hardware": "small", "source": "cloud_image",
                          "image": "/images/base.img", "user_data": "/images/user-data"}}
        with self.assertRaisesRegex(ValueError, "cloud_image is not available"):
            vmctl.resolve_creation_spec(request, {"hardware": {"small": {
                "vcpus": 2, "memory_mb": 2048, "disk_gb": 20}}})

    def test_cloud_image_sync_stops_before_mac_or_local_write(self):
        config = {"netbox": {"key": "secret"}}
        args = Namespace(command="sync", vm="guest", dry_run=False, purge=False)
        record = {"id": 5, "name": "guest", "local_context_data": {
            "vmctl": {"version": 4, "source": "cloud_image"}}}
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.get_vm", return_value=record),
            patch("vmctl.verify_vm_serial") as serial,
            patch("vmctl.ensure_primary_mac") as mac,
            patch("vmctl.create_vm") as local,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(vmctl.provision_command(config, args), 1)
        serial.assert_not_called()
        mac.assert_not_called()
        local.assert_not_called()
        self.assertIn("cloud_image is not available", errors.getvalue())

    def test_interface_adopt_error_suggests_mac_and_ip_checks(self):
        hint = vmctl.error_hint(netbox.NetBoxError(
            "VM guest: cannot match NetBox interface enp1s0 with IP addresses"), "adopt", "guest")
        self.assertIn("Primary MAC", hint)
        self.assertIn("IP в NetBox IPAM", hint)
        self.assertIn("vmctl --dry-run adopt guest", hint)

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

    def test_audit_compares_standard_fields_without_vmctl_context(self):
        config = {"netbox": {"key": "secret"}}
        remote = {"name": "old", "serial": "00000000-0000-4000-8000-000000000001",
                  "vcpus": 2, "memory": 2048, "disk": 10240,
                  "status": {"value": "active"}, "start_on_boot": {"value": "on"}}
        local = {"vcpus": 4, "memory_mb": 4096, "disk_mb": 20480,
                 "status": "active", "autostart": True,
                 "uuid": "00000000-0000-4000-8000-000000000002"}
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[remote]),
            patch("vmctl.local_names", return_value=["old"]),
            patch("vmctl.inspect_vm", return_value=local),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(vmctl.audit(config), 1)
        report = output.getvalue()
        self.assertIn("INVALID old: NetBox VM has no supported vmctl context", report)
        self.assertIn("DIFF old uuid:", report)
        self.assertIn("DIFF old vcpus:", report)
        self.assertIn("DIFF old memory_mb:", report)
        self.assertIn("DIFF old disk_mb:", report)

    def test_audit_reports_staged_live_difference_as_pending_restart(self):
        config = {"netbox": {"key": "secret"}}
        serial = "00000000-0000-4000-8000-000000000001"
        remote = {"name": "guest", "serial": serial, "vcpus": 2, "memory": 2048,
                  "disk": 10240, "status": {"value": "active"}, "start_on_boot": {"value": "on"},
                  "local_context_data": {"vmctl": {"version": 3, "source": "existing"}}}
        desired_display = {"type": "vnc", "listen": "127.0.0.1", "port": 5901}
        spec = {"name": "guest", "description": "new", "display": desired_display,
                "disks": [{"path": "/disk.qcow2", "size_mb": 10240}],
                "interfaces": [{"bridge": "br0", "mac_address": "52:54:00:00:00:01"}]}
        local = {"uuid": serial, "vcpus": 2, "memory_mb": 2048, "disk_mb": 10240,
                 "autostart": True, "status": "active", "description": "old",
                 "display": {"type": "vnc", "listen": "127.0.0.1", "port": 5900},
                 "disk_paths": ["/disk.qcow2"],
                 "disks": [{"path": "/disk.qcow2", "size_bytes": 10240 * 1024**2}],
                 "mounted_media": [], "interfaces": [{"bridge": "br0", "mac_address": "52:54:00:00:00:01"}]}
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[remote]),
            patch("vmctl.local_names", return_value=["guest"]),
            patch("vmctl.inspect_vm", return_value=local),
            patch("vmctl.local_spec_from_netbox", return_value=spec),
            patch("vmctl.verify_local_vm", side_effect=[None, ValueError("live differs")]) as verify,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(vmctl.audit(config), 0)
        self.assertEqual(verify.call_count, 2)
        self.assertIn("PENDING RESTART guest", output.getvalue())
        self.assertIn("0 issue(s)", output.getvalue())

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
                patch("vmctl.create_vm_components", side_effect=lambda *a: order.append("netbox-components")),
                patch("vmctl.local_spec_from_netbox", return_value=local_spec),
                patch("vmctl.create_vm", side_effect=lambda *a: order.append("local-create")),
                patch("vmctl.patch_vm", side_effect=lambda *a: order.append("netbox-staged")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(vmctl.main(), 0)
            self.assertEqual(order, ["netbox-create", "netbox-components", "local-create", "netbox-staged"])

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
            patch("vmctl.vm_interfaces", return_value=[]),
            patch("vmctl.local_spec_from_netbox", return_value=local_spec),
            patch("vmctl.verify_vm_serial", return_value=None),
            patch("vmctl.subprocess.run", return_value=SimpleNamespace(stdout="", returncode=0)),
            patch("vmctl.patch_vm", side_effect=lambda *a: order.append("netbox")),
            patch("vmctl.create_vm", side_effect=lambda *a: order.append("local")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(vmctl.main(), 0)
        self.assertEqual(order, ["netbox", "local", "netbox"])

    def test_dry_run_sync_existing_vm_does_not_plan_creation(self):
        args = Namespace(command="sync", vm="test-vm", config=Path("config.toml"), dry_run=True)
        config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
        record = {"id": 42, "name": "test-vm", "status": {"value": "active"}}
        plan = {"name": "test-vm", "source": "iso", "memory_mb": 2048,
                "vcpus": 2, "disk_gb": 20, "iso": "/images/install.iso"}

        def local(command, **kwargs):
            return SimpleNamespace(stdout="shut off\n" if "domstate" in command else "test-vm\n")

        with (
            patch("vmctl.parse_args", return_value=args),
            patch("vmctl.load_config", return_value=config),
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.get_vm", return_value=record),
            patch("vmctl.vm_interfaces", return_value=[]),
            patch("vmctl.local_spec_from_netbox", return_value=plan),
            patch("vmctl.verify_vm_serial", return_value=None),
            patch("vmctl.verify_local_vm") as verify,
            patch("vmctl.create_vm") as create,
            patch("vmctl.subprocess.run", side_effect=local),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(vmctl.main(), 0)
        verify.assert_called_once()
        create.assert_not_called()
        self.assertIn("Local VM definition matches NetBox", output.getvalue())
        self.assertIn("virsh -c qemu:///system start test-vm", output.getvalue())

    def test_sync_applies_changed_netbox_definition_to_existing_vm(self):
        order = []
        args = Namespace(command="sync", vm="test-vm", config=Path("config.toml"), dry_run=False)
        config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
        record = {"id": 42, "name": "test-vm", "status": {"value": "staged"},
                  "local_context_data": {"vmctl": {"version": 2, "source": "iso"}}}
        plan = {"name": "test-vm", "source": "iso", "memory_mb": 2048,
                "vcpus": 2, "disk_gb": 20, "iso": "/images/install.iso"}
        with (
            patch("vmctl.parse_args", return_value=args),
            patch("vmctl.load_config", return_value=config),
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.get_vm", return_value=record),
            patch("vmctl.vm_interfaces", return_value=[]),
            patch("vmctl.local_spec_from_netbox", return_value=plan),
            patch("vmctl.verify_vm_serial", return_value=None),
            patch("vmctl.verify_local_vm", side_effect=[ValueError("drift"), None]) as verify,
            patch("vmctl.patch_vm", side_effect=lambda *a: order.append("netbox")),
            patch("vmctl.redefine_vm", side_effect=lambda *a, **kw: order.append("libvirt")) as redefine,
            patch("vmctl.create_vm") as create,
            patch("vmctl.subprocess.run", return_value=SimpleNamespace(stdout="test-vm\n")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(vmctl.main(), 0)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(order, ["netbox", "libvirt"])
        redefine.assert_called_once()
        create.assert_not_called()

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
