import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from inventory import inspect_display
from vmctl import check_vm


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}
VM = {"uuid": "test-uuid", "status": "active", "autostart": True, "vcpus": 2,
      "memory_mb": 2048, "disk_mb": 10240, "disks": [], "interfaces": [], "description": "test"}


class DisplayTests(unittest.TestCase):
    def test_live_autoport_uses_assigned_port_and_shows_password(self):
        xml = """<domain><uuid>test-uuid</uuid><memory unit='MiB'>2048</memory><vcpu>2</vcpu><devices>
          <graphics type='vnc' autoport='yes' port='5906' listen='192.0.2.10' passwd='demo1234'/>
        </devices></domain>"""
        output = io.StringIO()
        with patch("inventory._virsh", return_value=xml), \
             patch("vmctl.inspect_vm", return_value=VM), \
             patch("vmctl.has_netbox_key", return_value=False), redirect_stdout(output):
            self.assertEqual(check_vm(CONFIG, "guest"), 0)
        self.assertIn("Дисплей: vnc://192.0.2.10:5906", output.getvalue())
        self.assertIn('пароль "demo1234"', output.getvalue())

    def test_display_without_graphics(self):
        with patch("inventory._virsh", return_value="<domain><devices/></domain>"):
            self.assertEqual(inspect_display(CONFIG, "guest"), {"type": "none"})

    def test_automatic_port_is_not_presented_as_connectable(self):
        xml = "<domain><memory unit='MiB'>2048</memory><vcpu>2</vcpu><devices>" \
              "<graphics type='spice' autoport='yes' port='-1' listen='127.0.0.1'/>" \
              "</devices></domain>"
        output = io.StringIO()
        with patch("inventory._virsh", return_value=xml), \
             patch("vmctl.inspect_vm", return_value=VM), \
             patch("vmctl.has_netbox_key", return_value=False), redirect_stdout(output):
            check_vm(CONFIG, "guest")
        self.assertIn("SPICE 127.0.0.1 (порт при запуске)", output.getvalue())
        self.assertIn("пароль не задан", output.getvalue())


if __name__ == "__main__":
    unittest.main()
