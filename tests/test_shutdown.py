import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from netbox import NetBoxError
from vmctl import shutdown_vm


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}


class ShutdownTests(unittest.TestCase):
    def test_graceful_shutdown_succeeds_only_after_guest_stops(self):
        states = iter(["running\n", "shut off\n"])
        commands = []

        def virsh(command, **kwargs):
            commands.append(command[3])
            return SimpleNamespace(stdout=next(states) if command[3] == "domstate" else "",
                                   returncode=0)

        with patch("vmctl.has_netbox_key", return_value=False), \
             patch("vmctl.subprocess.run", side_effect=virsh), \
             patch("builtins.print"):
            self.assertEqual(shutdown_vm(CONFIG, "guest", False, False), 0)
        self.assertEqual(commands, ["domstate", "shutdown", "domstate"])

    def test_graceful_request_reports_when_guest_stays_running(self):
        commands = []

        def virsh(command, **kwargs):
            commands.append(command[3])
            return SimpleNamespace(stdout="running\n", returncode=0)

        errors = io.StringIO()
        with patch("vmctl.has_netbox_key", return_value=False), \
             patch("vmctl.subprocess.run", side_effect=virsh), \
             patch("vmctl.time.monotonic", side_effect=[0, 61]), \
             redirect_stderr(errors):
            self.assertEqual(shutdown_vm(CONFIG, "guest", False, False), 1)
        self.assertEqual(commands, ["domstate", "shutdown", "domstate"])
        self.assertIn("--force", errors.getvalue())

    def test_forced_shutdown_writes_netbox_first(self):
        order = []
        states = iter(["running\n", "shut off\n"])

        def virsh(command, **kwargs):
            if command[3] == "domstate":
                return SimpleNamespace(stdout=next(states), returncode=0)
            order.append(command[3])
            return SimpleNamespace(returncode=0)

        with patch("vmctl.has_netbox_key", return_value=True), \
             patch("vmctl.find_device", return_value=(12, 7)), \
             patch("vmctl.get_vm", return_value={"id": 42}), \
             patch("vmctl.patch_vm", side_effect=lambda *args: order.append("netbox")), \
             patch("vmctl.subprocess.run", side_effect=virsh), \
             patch("builtins.print"):
            self.assertEqual(shutdown_vm(CONFIG, "guest", True, False), 0)
        self.assertEqual(order, ["netbox", "destroy"])

    def test_netbox_failure_prevents_local_shutdown(self):
        with patch("vmctl.has_netbox_key", return_value=True), \
             patch("vmctl.find_device", return_value=(12, 7)), \
             patch("vmctl.get_vm", return_value={"id": 42}), \
             patch("vmctl.patch_vm", side_effect=NetBoxError("denied")), \
             patch("vmctl.subprocess.run") as virsh:
            with self.assertRaises(NetBoxError):
                shutdown_vm(CONFIG, "guest", False, False)
        virsh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
