import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from netbox import NetBoxError
from vmctl import show_list


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}
VM = {"status": "active", "vcpus": 2, "memory_mb": 2048, "disk_mb": 20480,
      "autostart": True, "display": {"type": "vnc"}}


class ListTests(unittest.TestCase):
    def test_table_includes_local_resources_and_netbox_primary_ips(self):
        output = io.StringIO()
        record = {"name": "guest", "primary_ip4": {"address": "192.0.2.5/24"},
                  "primary_ip6": {"address": "2001:db8::5/64"}}
        with patch("vmctl.local_names", return_value=["guest"]), \
             patch("vmctl.inspect_vm", return_value=VM), \
             patch("vmctl.inspect_display", return_value={"type": "vnc", "listen": "192.0.2.10", "port": 5901}), \
             patch("vmctl.has_netbox_key", return_value=True), \
             patch("vmctl.find_device", return_value=(12, 3)), \
             patch("vmctl.list_vms", return_value=[record]), redirect_stdout(output):
            self.assertEqual(show_list(CONFIG), 0)
        table = output.getvalue()
        for expected in ("Имя", "guest", "запущена", "2048", "20", "192.0.2.5/24",
                         "2001:db8::5/64", "vnc://192.0.2.10:5901"):
            self.assertIn(expected, table)

    def test_local_inventory_survives_netbox_failure(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("vmctl.local_names", return_value=["guest"]), \
             patch("vmctl.inspect_vm", return_value=VM), \
             patch("vmctl.inspect_display", return_value={"type": "vnc", "listen": "::1", "port": 5902}), \
             patch("vmctl.has_netbox_key", return_value=True), \
             patch("vmctl.find_device", side_effect=NetBoxError("offline")), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(show_list(CONFIG), 0)
        self.assertIn("guest", output.getvalue())
        self.assertIn("vnc://[::1]:5902", output.getvalue())
        self.assertIn("NetBox недоступен", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
