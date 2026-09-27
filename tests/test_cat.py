import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import vmctl


class CatTests(unittest.TestCase):
    def test_cat_prints_persistent_xml_without_extra_text(self):
        xml = "<domain><name>guest</name></domain>\n"
        with (patch("vmctl.subprocess.run", return_value=SimpleNamespace(stdout=xml)) as run,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(vmctl.cat_vm({"host": {"libvirt_uri": "qemu:///system"}}, "guest"), 0)
        self.assertEqual(output.getvalue(), xml)
        self.assertEqual(run.call_args.args[0],
                         ["virsh", "-c", "qemu:///system", "dumpxml", "--inactive", "--security-info", "guest"])

    def test_live_option_uses_running_definition(self):
        args = vmctl.build_parser().parse_args(["cat", "guest", "--live"])
        self.assertTrue(args.live)
        with (patch("vmctl.subprocess.run", return_value=SimpleNamespace(stdout="<domain/>\n")) as run,
              redirect_stdout(io.StringIO())):
            vmctl.cat_vm({"host": {"libvirt_uri": "qemu:///system"}}, "guest", True)
        self.assertNotIn("--inactive", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
