import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import netbox


UUID = "00000000-0000-4000-8000-000000000123"
LOCAL = {
    "name": "guest", "uuid": UUID, "vcpus": 4, "memory_mb": 4096,
    "description": "local description", "status": "active", "autostart": True,
    "disks": [{"path": "/images/guest.qcow2", "target": "vda", "size_mb": 20480},
              {"path": "/images/data.qcow2", "target": "vdb", "size_mb": 5121}],
    "interfaces": [{"name": "inet", "bridge": "br0", "mac_address": "52:54:00:00:00:01"}],
    "mounted_media": [{"path": "/images/install.iso", "target": "sda"}],
    "display": {"type": "none"},
}
RECORD = {"id": 42, "name": "guest", "serial": "", "vcpus": 2, "memory": 2048,
          "status": {"value": "offline"}, "start_on_boot": {"value": "off"},
          "description": "old", "local_context_data": {"vmctl": {"version": 2, "source": "existing"}}}


class AdoptReconcileTests(unittest.TestCase):
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
        self.assertEqual(vm_changes["local_context_data"]["vmctl"]["mounted_media"], LOCAL["mounted_media"])
        self.assertEqual(vm_changes["local_context_data"]["vmctl"]["interface_bridges"], {"public": "br0"})
        self.assertIn(("PATCH", "virtualization/virtual-disks/5/", {"size": 20480}), calls)
        self.assertIn(("add", "disk", {"virtual_machine": 42, "name": "disk-vdb", "size": 5121}), calls)
        ips.assert_not_called()
        self.assertFalse(any(item[0] == "add" and item[1] == "nic" for item in calls))

    def test_stale_interface_with_ip_blocks_all_writes(self):
        stale = {"id": 8, "name": "old", "primary_mac_address": None}
        with patch("netbox.vm_disks", return_value=[]), \
             patch("netbox.vm_interfaces", return_value=[stale]), \
             patch("netbox.interface_ips", return_value=[{"id": 7}]), \
             patch("netbox.patch_vm") as update:
            with self.assertRaisesRegex(netbox.NetBoxError, "has IP addresses"):
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
