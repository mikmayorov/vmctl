"""Component reconciliation and NetBox-before-local operation tests."""

import contextlib
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import netbox
import vmctl
from vmcreate import domain_xml, redefine_vm


class ComponentTests(unittest.TestCase):
    def test_netbox_inventory_accepts_multiple_disks_and_interfaces(self):
        record = {"id": 7, "name": "guest", "vcpus": 2, "memory": 2048, "disk": 30720,
                  "local_context_data": {"vmctl": {"version": 3, "source": "iso",
                    "iso": "/iso/install.iso", "storage_directory": "/images", "bridge": "br0",
                    "interface_bridges": {"backup": "br2"}, "display": {"type": "none"}}}}
        disks = [{"name": "guest", "size": 20480}, {"name": "data", "size": 10240}]
        interfaces = [{"id": 8, "name": "inet", "primary_mac_address": {"mac_address": "52:54:00:00:00:01"}},
                      {"id": 9, "name": "backup", "primary_mac_address": {"mac_address": "52:54:00:00:00:02"}}]
        with (patch("netbox.vm_disks", return_value=disks),
              patch("netbox.vm_interfaces", return_value=interfaces),
              patch("netbox.interface_ips", return_value=[])):
            spec = netbox.local_spec_from_netbox(record, {})
        self.assertEqual([(item["name"], item["size_gb"]) for item in spec["disks"]],
                         [("guest", 20), ("data", 10)])
        self.assertEqual({item["name"]: item["bridge"] for item in spec["interfaces"]},
                         {"inet": "br0", "backup": "br2"})

    def test_domain_xml_contains_all_disks_and_interfaces(self):
        vm = {"name": "guest", "source": "iso", "iso": "/iso/install.iso", "memory_mb": 2048,
              "vcpus": 2, "disk_gb": 30, "display": {"type": "none"},
              "disks": [{"name": "guest", "path": "/images/guest.qcow2", "size_gb": 20},
                        {"name": "data", "path": "/images/guest-data.qcow2", "size_gb": 10}],
              "interfaces": [{"name": "inet", "bridge": "br0", "mac_address": "52:54:00:00:00:01"},
                             {"name": "backup", "bridge": "br2", "mac_address": "52:54:00:00:00:02"}]}
        root = ET.fromstring(domain_xml(vm, Path("/images/guest.qcow2"), "br0"))
        self.assertEqual([node.get("file") for node in root.findall("./devices/disk[@device='disk']/source")],
                         ["/images/guest.qcow2", "/images/guest-data.qcow2"])
        self.assertEqual([node.get("bridge") for node in root.findall("./devices/interface/source")],
                         ["br0", "br2"])

    def test_redefine_preserves_uuid_and_adds_disk_without_changing_existing_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            root_disk = storage / "guest.qcow2"
            root_disk.touch()
            spec = {"name": "guest", "source": "existing", "memory_mb": 2048, "vcpus": 2,
                    "disk_gb": 30, "description": "updated", "display": {"type": "none"},
                    "bridge": "br0", "storage_directory": directory,
                    "disks": [{"name": "guest", "path": str(root_disk), "size_gb": 20},
                              {"name": "data", "path": str(storage / "guest-data.qcow2"), "size_gb": 10}],
                    "interfaces": [{"name": "inet", "bridge": "br0", "mac_address": "52:54:00:00:00:01"}]}
            original = dict(spec)
            original["disks"] = spec["disks"][:1]
            original["uuid"] = "00112233-4455-6677-8899-aabbccddeeff"
            xml = domain_xml(original, root_disk, "br0")
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if "domstate" in command:
                    return SimpleNamespace(stdout="shut off\n")
                if "dumpxml" in command:
                    return SimpleNamespace(stdout=xml)
                if "info" in command:
                    return SimpleNamespace(stdout=json.dumps({"format": "qcow2", "virtual-size": 20 * 1024**3}))
                return SimpleNamespace(stdout="")

            with patch("vmcreate.subprocess.run", side_effect=run):
                redefine_vm(spec, {"host": {"libvirt_uri": "qemu:///system"},
                                   "storage": {"directory": directory}, "network": {"bridge": "br0"}},
                            storage, False)
            result = ET.fromstring((storage / "state/domains/guest.xml").read_text())
            self.assertEqual(result.findtext("./uuid"), original["uuid"])
            self.assertEqual(len(result.findall("./devices/disk[@device='disk']")), 2)
            self.assertTrue(any("create" in command for command in calls))

    def test_removed_disk_file_is_kept_unless_purge_is_explicit(self):
        for purge in (False, True):
            with self.subTest(purge=purge), tempfile.TemporaryDirectory() as directory:
                storage = Path(directory)
                root_disk, data_disk = storage / "guest.qcow2", storage / "guest-data.qcow2"
                root_disk.touch()
                data_disk.touch()
                spec = {"name": "guest", "source": "existing", "memory_mb": 2048, "vcpus": 2,
                        "disk_gb": 20, "description": "", "display": {"type": "none"},
                        "bridge": "br0", "storage_directory": directory,
                        "disks": [{"name": "guest", "path": str(root_disk), "size_gb": 20}],
                        "interfaces": []}
                original = dict(spec)
                original["disks"] = spec["disks"] + [{"name": "data", "path": str(data_disk), "size_gb": 10}]
                xml = domain_xml({**original, "iso": "/iso/install.iso"}, root_disk, "br0")

                def run(command, **kwargs):
                    if "domstate" in command:
                        return SimpleNamespace(stdout="shut off\n")
                    if "dumpxml" in command:
                        return SimpleNamespace(stdout=xml)
                    return SimpleNamespace(stdout="")

                with patch("vmcreate.subprocess.run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                    redefine_vm(spec, {"host": {"libvirt_uri": "qemu:///system"},
                                       "storage": {"directory": directory}, "network": {"bridge": "br0"}},
                                storage, False, purge)
                self.assertEqual(data_disk.exists(), not purge)

    def test_managed_disk_add_writes_netbox_before_local(self):
        order = []
        args = Namespace(command="disk", action="add", vm="guest", name="data",
                         size_gb=10, dry_run=False)
        config = {"netbox": {"key": "secret"}, "storage": {"directory": "/images"}}
        record = {"id": 7, "name": "guest"}
        with (patch("vmctl._stopped", return_value=True),
              patch("vmctl.find_device", return_value=(1, 2)),
              patch("vmctl.get_vm", return_value=record),
              patch("vmctl.vm_disks", return_value=[{"id": 3, "name": "guest", "size": 20480}]),
              patch("vmctl.add_component", side_effect=lambda *a: order.append("netbox")),
              patch("vmctl.local_spec_from_netbox", return_value={"name": "guest"}),
              patch("vmctl.redefine_vm", side_effect=lambda *a: order.append("libvirt")),
              patch("vmctl.verify_local_vm")):
            self.assertEqual(vmctl.component_command(config, args), 0)
        self.assertEqual(order, ["netbox", "libvirt"])

    def test_netbox_failure_stops_local_component_change(self):
        args = Namespace(command="disk", action="add", vm="guest", name="data",
                         size_gb=10, dry_run=False)
        config = {"netbox": {"key": "secret"}, "storage": {"directory": "/images"}}
        with (patch("vmctl._stopped", return_value=True),
              patch("vmctl.find_device", return_value=(1, 2)),
              patch("vmctl.get_vm", return_value={"id": 7, "name": "guest"}),
              patch("vmctl.vm_disks", return_value=[]),
              patch("vmctl.add_component", side_effect=netbox.NetBoxError("403")),
              patch("vmctl.redefine_vm") as local):
            with self.assertRaises(netbox.NetBoxError):
                vmctl.component_command(config, args)
        local.assert_not_called()

    def test_keyless_disk_add_changes_only_local_vm(self):
        args = Namespace(command="disk", action="add", vm="guest", name="data",
                         size_gb=10, dry_run=False)
        config = {"host": {"libvirt_uri": "qemu:///system"}, "storage": {"directory": "/images"},
                  "network": {"bridge": "br0"}}
        spec = {"name": "guest", "disks": [{"name": "guest", "path": "/images/guest.qcow2", "size_gb": 20}],
                "interfaces": []}
        with (patch("vmctl._stopped", return_value=True),
              patch("vmctl._local_component_spec", return_value=spec),
              patch("vmctl.redefine_vm") as local,
              patch("vmctl.verify_local_vm"),
              patch("vmctl.find_device") as netbox_lookup):
            self.assertEqual(vmctl.component_command(config, args), 0)
        netbox_lookup.assert_not_called()
        self.assertEqual(spec["disks"][-1]["path"], "/images/guest-data.qcow2")
        local.assert_called_once()

    def test_interface_with_ip_cannot_be_removed(self):
        args = Namespace(command="nic", action="remove", vm="guest", name="inet", dry_run=False)
        config = {"netbox": {"key": "secret"}}
        with (patch("vmctl._stopped", return_value=True),
              patch("vmctl.find_device", return_value=(1, 2)),
              patch("vmctl.get_vm", return_value={"id": 7, "name": "guest"}),
              patch("vmctl.vm_interfaces", return_value=[{"id": 8, "name": "inet"}]),
              patch("vmctl.interface_ips", return_value=[{"id": 9}]),
              patch("vmctl.remove_component") as remove):
            with self.assertRaisesRegex(netbox.NetBoxError, "unassign IP"):
                vmctl.component_command(config, args)
        remove.assert_not_called()

    def test_existing_assigned_mac_is_reused_after_partial_failure(self):
        calls = []
        with (patch("netbox.interface_macs", return_value=[{"id": 19, "mac_address": "52:54:00:00:00:19"}]),
              patch("netbox._request", side_effect=lambda *args: calls.append(args))):
            address = netbox.ensure_primary_mac({}, {"id": 8, "name": "inet"})
        self.assertEqual(address, "52:54:00:00:00:19")
        self.assertEqual([call[1] for call in calls], ["PATCH"])
        self.assertEqual(calls[0][3]["primary_mac_address"], 19)

    def test_delete_marks_netbox_before_undefine_then_deletes_record(self):
        order = []
        args = Namespace(command="delete", vm="guest", purge=False, dry_run=False)
        config = {"netbox": {"key": "secret"}, "host": {"libvirt_uri": "qemu:///system"},
                  "storage": {"directory": "/images"}}
        with (patch("vmctl._stopped", return_value=True),
              patch("vmctl.find_device", return_value=(1, 2)),
              patch("vmctl.get_vm", return_value={"id": 7, "name": "guest"}),
              patch("vmctl.vm_interfaces", return_value=[]),
              patch("vmctl.inspect_vm", return_value={"disk_paths": ["/images/guest.qcow2"]}),
              patch("vmctl.patch_vm", side_effect=lambda *a: order.append("mark")),
              patch("vmctl.subprocess.run", side_effect=lambda *a, **k: order.append("undefine")),
              patch("vmctl._request", side_effect=lambda *a: order.append("delete")),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(vmctl.delete_vm(config, args), 0)
        self.assertEqual(order, ["mark", "undefine", "delete"])

    def test_delete_does_not_undefine_when_netbox_mark_fails(self):
        args = Namespace(command="delete", vm="guest", purge=False, dry_run=False)
        config = {"netbox": {"key": "secret"}, "host": {"libvirt_uri": "qemu:///system"},
                  "storage": {"directory": "/images"}}
        with (patch("vmctl._stopped", return_value=True),
              patch("vmctl.find_device", return_value=(1, 2)),
              patch("vmctl.get_vm", return_value={"id": 7, "name": "guest"}),
              patch("vmctl.vm_interfaces", return_value=[]),
              patch("vmctl.inspect_vm", return_value={"disk_paths": ["/images/guest.qcow2"]}),
              patch("vmctl.patch_vm", side_effect=netbox.NetBoxError("403")),
              patch("vmctl.subprocess.run") as local):
            with self.assertRaises(netbox.NetBoxError):
                vmctl.delete_vm(config, args)
        local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
