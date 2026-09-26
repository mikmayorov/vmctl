import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from inventory import inspect_display
from vmctl import show_display


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}


class DisplayTests(unittest.TestCase):
    def test_live_autoport_uses_assigned_port_and_shows_password(self):
        xml = """<domain><devices>
          <graphics type='vnc' autoport='yes' port='5906' listen='192.0.2.10' passwd='demo1234'/>
        </devices></domain>"""
        output = io.StringIO()
        with patch("inventory._virsh", return_value=xml), redirect_stdout(output):
            self.assertEqual(show_display(CONFIG, "guest"), 0)
        self.assertIn("Тип: VNC", output.getvalue())
        self.assertIn("IP: 192.0.2.10", output.getvalue())
        self.assertIn("Порт: 5906", output.getvalue())
        self.assertIn('Пароль: "demo1234"', output.getvalue())

    def test_display_without_graphics(self):
        with patch("inventory._virsh", return_value="<domain><devices/></domain>"):
            self.assertEqual(inspect_display(CONFIG, "guest"), {"type": "none"})

    def test_automatic_port_is_not_presented_as_connectable(self):
        xml = "<domain><devices><graphics type='spice' autoport='yes' port='-1' listen='127.0.0.1'/></devices></domain>"
        output = io.StringIO()
        with patch("inventory._virsh", return_value=xml), redirect_stdout(output):
            show_display(CONFIG, "guest")
        self.assertIn("Порт: назначится при запуске ВМ", output.getvalue())
        self.assertIn("Пароль: не задан", output.getvalue())


if __name__ == "__main__":
    unittest.main()
