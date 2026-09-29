import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import netbox
from vmcreate import host_interface_name


UUID = "00000000-0000-4000-8000-000000000123"
LOCAL = {
    "name": "guest", "uuid": UUID, "vcpus": 4, "memory_mb": 4096,
    "description": "local description", "status": "active", "autostart": True,
    "disks": [{"path": "/images/guest.qcow2", "target": "vda", "size_mb": 20480},
              {"path": "/images/data.qcow2", "target": "vdb", "size_mb": 5121}],
    "interfaces": [{"name": "inet", "bridge": "br0", "mac_address": "52:54:00:00:00:01"}],
    "mounted_media": [{"path": "/images/install.iso", "target": "sda", "size_mb": 1024}],
    "display": {"type": "none"},
}
RECORD = {"id": 42, "name": "guest", "serial": "", "vcpus": 2, "memory": 2048,
          "status": {"value": "offline"}, "start_on_boot": {"value": "off"},
          "description": "old", "local_context_data": {"vmctl": {"version": 2, "source": "existing"}}}


class AdoptReconcileTests(unittest.TestCase):
    def test_interface_only_migration_preserves_other_netbox_state(self):
        tap = host_interface_name("guest", "inet")
        record = {"id": 42, "name": "guest", "local_context_data": {"vmctl": {
            "version": 3, "source": "existing",
            "display": {"type": "vnc", "listen": "192.0.2.10", "port": 5901},
            "interface_name": "inet", "interface_bridges": {"inet": "br0"}}}}
        vm = {"name": "guest", "interfaces": [{"name": "inet", "host_dev": tap,
              "bridge": "br0", "mac_address": "52:54:00:00:00:01"}]}
        nic = {"id": 8, "name": "inet", "primary_mac_address": {"mac_address": "52:54:00:00:00:01"}}
        with (patch("netbox.vm_interfaces", return_value=[nic]),
              patch("netbox.patch_vm") as update,
              patch("netbox._request") as request,
              redirect_stdout(io.StringIO())):
            self.assertTrue(netbox.normalize_vm_interface_names({}, record, vm, False))
        data = update.call_args.args[2]["local_context_data"]["vmctl"]
        self.assertEqual(data["display"], record["local_context_data"]["vmctl"]["display"])
        self.assertEqual(data["version"], 4)
        self.assertEqual(data["interfaces"], {"inet": {"host_dev": tap, "bridge": "br0"}})
        self.assertNotIn("interface_bridges", data)
        request.assert_called_once_with({}, "PATCH", "virtualization/interfaces/8/", {
            "description": f"Host interface: {tap}; bridge: br0"})

    def test_interface_only_migration_keeps_handwritten_description(self):
        tap = host_interface_name("guest", "inet")
        record = {"id": 42, "name": "guest", "local_context_data": {"vmctl": {
            "version": 4, "source": "existing",
            "interfaces": {"inet": {"host_dev": tap, "bridge": "br0"}}}}}
        vm = {"name": "guest", "interfaces": [{"name": "inet", "host_dev": tap,
              "bridge": "br0", "mac_address": "52:54:00:00:00:01"}]}
        nic = {"id": 8, "name": "inet", "description": "Administrator note",
               "primary_mac_address": {"mac_address": "52:54:00:00:00:01"}}
        with (patch("netbox.vm_interfaces", return_value=[nic]),
              patch("netbox.patch_vm") as update,
              patch("netbox._request") as request,
              redirect_stdout(io.StringIO())):
            self.assertFalse(netbox.normalize_vm_interface_names({}, record, vm, False))
        update.assert_not_called()
        request.assert_not_called()

    def test_netbox_spec_excludes_iso_from_libvirt_writable_disks(self):
        record = {**RECORD, "serial": UUID, "disk": 21504,
                  "local_context_data": {"vmctl": {"version": 3, "source": "existing",
                    "mounted_media": [{"name": "media-sda", "target": "sda"}]}}}
        disks = [{"id": 5, "name": "guest", "size": 20480,
                  "description": "/images/guest.qcow2"},
                 {"id": 6, "name": "media-sda", "size": 1024,
                  "description": "/images/install.iso"}]
        with patch("netbox.vm_disks", return_value=disks), \
             patch("netbox.vm_interfaces", return_value=[]):
            spec = netbox.local_spec_from_netbox(record, {})
        self.assertEqual([item["path"] for item in spec["disks"]], ["/images/guest.qcow2"])
        self.assertEqual(spec["disk_gb"], 20)
        self.assertEqual(spec["mounted_media"], [{"name": "media-sda", "path": "/images/install.iso",
                                                   "target": "sda", "size_mb": 1024}])

    def test_prepare_registers_iso_as_virtual_disk_with_path(self):
        with tempfile.TemporaryDirectory() as directory:
            iso = Path(directory) / "install.iso"
            iso.write_bytes(b"iso")
            record = {"id": 42, "name": "guest", "disk": 20480,
                      "local_context_data": {"vmctl": {"source": "iso", "iso": str(iso)}}}
            requests = []
            def request(_config, method, path, payload=None):
                requests.append((method, path, payload))
                if path == "virtualization/interfaces/":
                    return {"id": 8, "name": "inet"}
                if path == "dcim/mac-addresses/":
                    return {"id": 9}
                return {}
            with patch("netbox.vm_disks", return_value=[]), \
                 patch("netbox.vm_interfaces", return_value=[]), \
                 patch("netbox._request", side_effect=request):
                netbox.create_vm_components({"storage": {"directory": directory}}, record,
                                            mac="52:54:00:00:00:01", root_size_mb=20480)
        media = next(item[2] for item in requests if item[1] == "virtualization/virtual-disks/"
                     and item[2]["name"] == "media-sda")
        self.assertEqual((media["description"], media["size"]), (str(iso), 1))

    def test_dry_run_plans_existing_vm_with_media_without_writes(self):
        with patch("netbox.vm_disks", return_value=[]), \
             patch("netbox.vm_interfaces", return_value=[]), \
             patch("netbox.patch_vm") as update, \
             patch("netbox.add_component") as add, \
             redirect_stdout(io.StringIO()) as output:
            changed = netbox.reconcile_adopted_vm({}, RECORD, LOCAL, True)
        self.assertTrue(changed)
        self.assertIn("WOULD UPDATE guest", output.getvalue())
        self.assertIn("ISO/media 1", output.getvalue())
        update.assert_not_called()
        add.assert_not_called()

    def test_reconcile_updates_sizing_uuid_disks_and_preserves_ip_interface(self):
        disk = {"id": 5, "name": "guest", "size": 10240}
        nic = {"id": 8, "name": "public", "primary_mac_address":
               {"id": 10, "mac_address": "52:54:00:00:00:01"}}
        calls = []
        with patch("netbox.vm_disks", return_value=[disk]), \
             patch("netbox.vm_interfaces", return_value=[nic]), \
             patch("netbox.patch_vm", side_effect=lambda *args: calls.append(("vm", args[2]))), \
             patch("netbox._request", side_effect=lambda *args: calls.append((args[1], args[2], args[3]))), \
             patch("netbox.add_component", side_effect=lambda *args: calls.append(("add", args[1], args[2]))), \
             patch("netbox.interface_ips") as ips, \
             redirect_stdout(io.StringIO()):
            netbox.reconcile_adopted_vm({}, RECORD, LOCAL, False)
        vm_changes = calls[0][1]
        self.assertEqual(vm_changes["serial"], UUID)
        self.assertEqual((vm_changes["vcpus"], vm_changes["memory"]), (4, 4096))
        self.assertEqual(vm_changes["local_context_data"]["vmctl"]["mounted_media"],
                         [{"name": "media-sda", "target": "sda"}])
        tap = host_interface_name("guest", "public")
        self.assertEqual(vm_changes["local_context_data"]["vmctl"]["interfaces"],
                         {"public": {"host_dev": tap, "bridge": "br0"}})
        self.assertFalse(any(item[0] == "PATCH" and item[1] == "virtualization/interfaces/8/"
                             and "name" in item[2] for item in calls))
        self.assertIn(("PATCH", "virtualization/virtual-disks/5/",
                       {"size": 20480, "description": "/images/guest.qcow2"}), calls)
        self.assertIn(("add", "disk", {"virtual_machine": 42, "name": "disk-vdb", "size": 5121,
                                        "description": "/images/data.qcow2"}), calls)
        self.assertIn(("add", "disk", {"virtual_machine": 42, "name": "media-sda", "size": 1024,
                                        "description": "/images/install.iso"}), calls)
        ips.assert_not_called()
        self.assertFalse(any(item[0] == "add" and item[1] == "nic" for item in calls))

    def test_adopt_reuses_single_ip_interface_without_matching_name_or_mac(self):
        nic = {"id": 8, "name": "enp1s0", "primary_mac_address": None}
        calls = []
        with (patch("netbox.vm_disks", return_value=[]),
              patch("netbox.vm_interfaces", return_value=[nic]),
              patch("netbox.interface_ips", return_value=[{"id": 7}]) as ips,
              patch("netbox.interface_macs", return_value=[]),
              patch("netbox.patch_vm") as update,
              patch("netbox._request", side_effect=lambda *args: calls.append((args[1], args[2], args[3])) or {"id": 9}),
              patch("netbox.add_component", side_effect=lambda *args: {"id": 10}),
              redirect_stdout(io.StringIO()) as output):
            self.assertTrue(netbox.reconcile_adopted_vm({}, RECORD, LOCAL, False))
        tap = host_interface_name("guest", "enp1s0")
        self.assertEqual(update.call_args.args[2]["local_context_data"]["vmctl"]["interfaces"],
                         {"enp1s0": {"host_dev": tap, "bridge": "br0"}})
        self.assertIn(("POST", "dcim/mac-addresses/", {
            "mac_address": "52:54:00:00:00:01",
            "assigned_object_type": "virtualization.vminterface", "assigned_object_id": 8,
        }), calls)
        self.assertFalse(any(method == "DELETE" and "interfaces/8/" in path for method, path, _ in calls))
        ips.assert_not_called()
        self.assertIn("KEEP VM Interface enp1s0", output.getvalue())

    def test_adopt_reuses_assigned_nonprimary_mac_on_ip_interface(self):
        nic = {"id": 8, "name": "enp1s0", "primary_mac_address": None}
        calls = []
        with (patch("netbox.vm_disks", return_value=[]),
              patch("netbox.vm_interfaces", return_value=[nic]),
              patch("netbox.interface_ips", return_value=[{"id": 7}]) as ips,
              patch("netbox.interface_macs", return_value=[{
                  "id": 9, "mac_address": "52:54:00:00:00:99"}]),
              patch("netbox.patch_vm"),
              patch("netbox._request", side_effect=lambda *args: calls.append((args[1], args[2], args[3])) or {}),
              patch("netbox.add_component", return_value={"id": 10}),
              redirect_stdout(io.StringIO())):
            netbox.reconcile_adopted_vm({}, RECORD, LOCAL, False)
        self.assertIn(("PATCH", "dcim/mac-addresses/9/", {"mac_address": "52:54:00:00:00:01"}), calls)
        self.assertIn(("PATCH", "virtualization/interfaces/8/", {"primary_mac_address": 9}), calls)
        self.assertFalse(any(method == "POST" and path == "virtualization/interfaces/" for method, path, _ in calls))
        ips.assert_not_called()

    def test_stale_interface_with_ip_blocks_all_writes(self):
        stale = {"id": 8, "name": "old", "primary_mac_address": None}
        with patch("netbox.vm_disks", return_value=[]), \
             patch("netbox.vm_interfaces", return_value=[stale]), \
             patch("netbox.interface_ips", return_value=[{"id": 7}]), \
             patch("netbox.patch_vm") as update:
            with self.assertRaisesRegex(netbox.NetBoxError, "cannot match NetBox interface old with IP addresses"):
                netbox.reconcile_adopted_vm({}, RECORD, {**LOCAL, "interfaces": []}, False)
        update.assert_not_called()

    def test_conflicting_uuid_blocks_all_writes(self):
        record = {**RECORD, "serial": "00000000-0000-4000-8000-000000000456"}
        with patch("netbox.vm_disks") as disks, patch("netbox.patch_vm") as update:
            with self.assertRaisesRegex(netbox.NetBoxError, "differs"):
                netbox.reconcile_adopted_vm({}, record, LOCAL, False)
        disks.assert_not_called()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
