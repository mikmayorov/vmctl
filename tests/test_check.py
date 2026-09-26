import io
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from vmctl import check_host


CONFIG = {"host": {"libvirt_uri": "qemu:///system"}}


class CheckTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
