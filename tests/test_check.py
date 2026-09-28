import io
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from vmctl import _check_table, check_host, check_vm


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}


class CheckTests(unittest.TestCase):
    def test_check_marks_entire_different_row_red_on_terminal(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with redirect_stdout(Terminal()) as output:
            _check_table([("UUID", "one", "two", "two", True)])
        self.assertIn("\x1b[31m! UUID", output.getvalue())
        self.assertIn("\x1b[0m", output.getvalue())

    def test_check_compares_netbox_persistent_and_live(self):
        persistent = {
            "uuid": "00000000-0000-4000-8000-000000000001", "vcpus": 4,
            "memory_mb": 4096, "description": "new",
            "disks": [{"target": "vda", "path": "/disk.qcow2"}], "mounted_media": [],
            "interfaces": [{"bridge": "br0", "mac_address": "52:54:00:00:00:01",
                            "host_dev": "vm-guest-123456"}],
            "display": {"type": "none"},
        }
        live = {**persistent, "vcpus": 2, "memory_mb": 2048,
                "status": "active", "autostart": True,
                "disks": [{"target": "vda", "path": "/disk.qcow2", "size_mb": 10240}],
                "interfaces": [{"bridge": "br0", "mac_address": "52:54:00:00:00:01",
                                "host_dev": "vnet11"}]}
        desired = {**persistent,
                   "disks": [{"name": "guest", "path": "/disk.qcow2", "size_mb": 10240}],
                   "interfaces": [{"name": "vm-guest-123456", "bridge": "br0",
                                   "mac_address": "52:54:00:00:00:01"}]}
        record = {"name": "guest", "status": {"value": "active"},
                  "start_on_boot": {"value": "on"}}
        with (patch("vmctl.inspect_vm", return_value=live),
              patch("vmctl.inspect_definition", return_value=persistent),
              patch("vmctl.inspect_display", return_value={"type": "none"}),
              patch("vmctl.has_netbox_key", return_value=True),
              patch("vmctl.find_device", return_value=(1, 2)),
              patch("vmctl.list_vms", return_value=[record]),
              patch("vmctl.local_spec_from_netbox", return_value=desired),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(check_vm(CONFIG, "guest"), 0)
        report = output.getvalue()
        self.assertIn("NetBox", report)
        self.assertIn("XML (следующий запуск)", report)
        self.assertIn("Mem (сейчас)", report)
        self.assertIn("DEV-NAME/DEV-MAC", report)
        self.assertIn("Ожидают перезапуска: CPU/RAM/HDD, DEV-NAME/DEV-MAC", report)
        self.assertNotIn("Расхождение NetBox", report)

    def test_host_check_combines_diagnostics_version_pools_and_networks(self):
        output = io.StringIO()
        responses = [subprocess.CompletedProcess([], 0, "libvirt 11\n", ""),
                     subprocess.CompletedProcess([], 0, "pool-a\n", ""),
                     subprocess.CompletedProcess([], 0, "network-a\n", "")]
        with patch("vmctl.doctor", return_value=0), \
             patch("vmctl.subprocess.run", side_effect=responses) as run, \
             redirect_stdout(output):
            self.assertEqual(check_host(CONFIG), 0)
        self.assertEqual([call.args[0][3] for call in run.call_args_list],
                         ["version", "pool-list", "net-list"])
        self.assertIn("pool-a", output.getvalue())
        self.assertIn("network-a", output.getvalue())

    def test_check_detects_disk_capacity_drift(self):
        local = {"status": "offline", "autostart": False,
                 "disks": [{"path": "/disk.qcow2", "size_mb": 1024}]}
        persistent = {"uuid": "u", "vcpus": 2, "memory_mb": 2048,
                      "description": "", "disks": [{"path": "/disk.qcow2"}],
                      "mounted_media": [], "interfaces": [], "display": {"type": "none"}}
        desired = {**persistent, "disks": [{"path": "/disk.qcow2", "size_mb": 2048}]}
        record = {"name": "guest", "status": "offline"}
        with (patch("vmctl.inspect_vm", return_value=local),
              patch("vmctl.inspect_definition", return_value=persistent),
              patch("vmctl.has_netbox_key", return_value=True),
              patch("vmctl.find_device", return_value=(1, 2)),
              patch("vmctl.list_vms", return_value=[record]),
              patch("vmctl.local_spec_from_netbox", return_value=desired),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(check_vm(CONFIG, "guest"), 1)
        self.assertIn("Расхождение NetBox: CPU/RAM/HDD", output.getvalue())


if __name__ == "__main__":
    unittest.main()
