import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import netbox
import vmctl
from vmctl import verify_vm_serial


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}
UUID = "00000000-0000-4000-8000-000000000123"


class UUIDSerialTests(unittest.TestCase):
    def test_adopted_vm_posts_local_uuid_to_netbox_serial(self):
        vm = {
            "name": "guest", "uuid": UUID, "status": "active", "vcpus": 2,
            "memory_mb": 2048, "disk_mb": 20480, "autostart": False,
            "disk_paths": ["/images/guest.qcow2"],
            "disks": [{"path": "/images/guest.qcow2", "size_gb": 20}],
            "interfaces": [{"name": "inet", "bridge": "br1", "mac_address": "52:54:00:12:34:56"}],
        }
        with patch("netbox._request", return_value={"id": 42, "name": "guest"}) as request, \
             patch("netbox.add_component", return_value={"id": 8}), \
             patch("netbox.ensure_primary_mac"), redirect_stdout(io.StringIO()):
            netbox.import_vm(CONFIG, vm, 7, 12)
        self.assertEqual(request.call_args.args[3]["serial"], UUID)

    def test_missing_serial_blocks_sync_without_netbox_write(self):
        record = {"id": 42, "name": "guest", "serial": ""}
        with patch("vmctl.local_uuid") as local, patch("vmctl.patch_vm") as update:
            with self.assertRaisesRegex(netbox.NetBoxError, "Serial is empty"):
                verify_vm_serial(CONFIG, record, True)
        local.assert_not_called()
        update.assert_not_called()

    def test_matching_serial_is_read_only(self):
        record = {"id": 42, "name": "guest", "serial": UUID}
        with patch("vmctl.local_uuid", return_value=UUID), patch("vmctl.patch_vm") as update:
            self.assertEqual(verify_vm_serial(CONFIG, record, True), UUID)
        update.assert_not_called()

    def test_conflicting_serial_blocks_update(self):
        record = {"id": 42, "name": "guest", "serial": UUID}
        with patch("vmctl.local_uuid", return_value="00000000-0000-4000-8000-000000000456"), \
             patch("vmctl.patch_vm") as update:
            with self.assertRaisesRegex(netbox.NetBoxError, "differs"):
                verify_vm_serial(CONFIG, record, True)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
