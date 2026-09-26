import io
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import netbox
import vmctl
from vmctl import sync_vm_serial


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}
UUID = "00000000-0000-4000-8000-000000000123"


class UUIDSerialTests(unittest.TestCase):
    def test_serial_only_sync_works_for_legacy_record_without_vmctl_context(self):
        args = Namespace(command="sync", vm="guest", serial_only=True, purge=False,
                         config=Path("config.toml"), dry_run=False)
        record = {"id": 42, "name": "guest", "serial": ""}
        with patch("vmctl.parse_args", return_value=args), \
             patch("vmctl.load_config", return_value={**CONFIG, "netbox": {"key": "test"}}), \
             patch("vmctl.find_device", return_value=(12, 7)), \
             patch("vmctl.get_vm", return_value=record), \
             patch("vmctl.local_names", return_value=["guest"]), \
             patch("vmctl.sync_vm_serial") as serial_sync, \
             patch("vmctl.local_spec_from_netbox") as vm_spec:
            self.assertEqual(vmctl.main(), 0)
        serial_sync.assert_called_once()
        vm_spec.assert_not_called()

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

    def test_backfill_writes_netbox_before_continuing(self):
        record = {"id": 42, "name": "guest", "serial": ""}
        updated = {**record, "serial": UUID}
        order = []
        with patch("vmctl.local_uuid", return_value=UUID), \
             patch("vmctl.patch_vm", side_effect=lambda *args: order.append("patch")), \
             patch("vmctl.find_device", return_value=(12, 7)), \
             patch("vmctl.get_vm", side_effect=lambda *args: (order.append("read") or updated)), \
             redirect_stdout(io.StringIO()):
            result = sync_vm_serial(CONFIG, record, True, False, True)
        self.assertEqual(result["serial"], UUID)
        self.assertEqual(order, ["patch", "read"])

    def test_conflicting_serial_blocks_update(self):
        record = {"id": 42, "name": "guest", "serial": UUID}
        with patch("vmctl.local_uuid", return_value="00000000-0000-4000-8000-000000000456"), \
             patch("vmctl.patch_vm") as update:
            with self.assertRaisesRegex(netbox.NetBoxError, "differs"):
                sync_vm_serial(CONFIG, record, True, False)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
