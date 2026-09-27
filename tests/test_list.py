import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from netbox import NetBoxError
from vmctl import refresh_guest_links, show_list


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}
VM = {"status": "active", "vcpus": 2, "memory_mb": 2048, "disk_mb": 20480,
      "autostart": True, "display": {"type": "vnc"},
      "disks": [{"size_bytes": 20480 * 1024**2}],
      "interfaces": [{"host_dev": "vnet11"}],
      "uuid": "00000000-0000-4000-8000-000000000001"}


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
             patch("vmctl.list_vms", return_value=[record]), \
             patch("vmctl.refresh_guest_links"), redirect_stdout(output):
            self.assertEqual(show_list(CONFIG), 0)
        table = output.getvalue()
        for expected in ("Имя", "guest", "запущена", "RAM GB", "Диск GB", "vnet11", "21.475",
                         "192.0.2.5/24", "2001:db8::5/64", "vnc://192.0.2.10:5901", VM["uuid"]):
            self.assertIn(expected, table)
        data = table.splitlines()[1].split()
        self.assertEqual(data[3:5], ["2.147", "21.475"])
        self.assertLess(data.index("vnet11"), data.index("192.0.2.5/24"))
        self.assertEqual(data[-1], VM["uuid"])

    def test_local_inventory_survives_netbox_failure(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("vmctl.local_names", return_value=["guest"]), \
             patch("vmctl.inspect_vm", return_value=VM), \
             patch("vmctl.inspect_display", return_value={"type": "vnc", "listen": "::1", "port": 5902}), \
             patch("vmctl.has_netbox_key", return_value=True), \
             patch("vmctl.find_device", side_effect=NetBoxError("offline")), \
             patch("vmctl.refresh_guest_links"), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(show_list(CONFIG), 0)
        self.assertIn("guest", output.getvalue())
        self.assertIn("vnc://[::1]:5902", output.getvalue())
        self.assertIn("NetBox недоступен", errors.getvalue())

    def test_resource_sizes_round_to_three_decimal_gb_from_bytes(self):
        vm = {**VM, "memory_mb": 1537, "disk_mb": 1025,
              "disks": [{"size_bytes": 1025 * 1024**2}]}
        with (patch("vmctl.local_names", return_value=["guest"]),
              patch("vmctl.inspect_vm", return_value=vm),
              patch("vmctl.inspect_display", return_value={"type": "none"}),
              patch("vmctl.has_netbox_key", return_value=False),
              patch("vmctl.refresh_guest_links"),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(show_list(CONFIG), 0)
        columns = output.getvalue().splitlines()[1].split()
        self.assertEqual(columns[3:5], ["1.612", "1.075"])

    def test_current_guest_links_follow_persistent_libvirt_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "vmctl"
            project.mkdir()
            xml_dir = Path(directory) / "libvirt"
            xml_dir.mkdir()
            (xml_dir / "guest.xml").write_text("<domain/>")
            config = {"host": {"libvirt_uri": "qemu:///system",
                               "domain_xml_directory": str(xml_dir)}}
            self.assertEqual(refresh_guest_links(config, ["guest"], project), 0)
            link = project / "current-guest" / "guest"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.readlink(), xml_dir / "guest.xml")
            self.assertEqual(refresh_guest_links(config, [], project), 0)
            self.assertFalse(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
