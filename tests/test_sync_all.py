import contextlib
import io
import unittest
from argparse import Namespace
from unittest.mock import patch

import vmctl


class SyncAllTests(unittest.TestCase):
    def setUp(self):
        self.config = {"netbox": {"key": "secret"}}
        self.args = Namespace(command="sync", vm=None, dry_run=True, purge=False)

    def test_parser_accepts_sync_without_name(self):
        args = vmctl.build_parser().parse_args(["sync"])
        self.assertIsNone(args.vm)

    def test_sync_only_host_records_and_continues_after_failure(self):
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[{"name": "zeta"}, {"name": "alpha"}]) as records,
            patch("vmctl.provision_command", side_effect=[1, 0]) as provision,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            result = vmctl.sync_all(self.config, self.args)
        self.assertEqual(result, 1)
        records.assert_called_once_with(self.config, 12)
        self.assertEqual([item.args[1].vm for item in provision.call_args_list], ["alpha", "zeta"])
        self.assertTrue(all(item.args[1].dry_run for item in provision.call_args_list))
        self.assertIn("Sync: 2 VM(s), 1 failure(s)", output.getvalue())

    def test_missing_key_stops_before_host_or_local_access(self):
        with (
            patch("vmctl.find_device") as device,
            patch("vmctl.provision_command") as provision,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            result = vmctl.sync_all({"netbox": {}}, self.args)
        self.assertEqual(result, 1)
        device.assert_not_called()
        provision.assert_not_called()
        self.assertIn("sync failed", errors.getvalue())

    def test_duplicate_names_stop_before_any_changes(self):
        with (
            patch("vmctl.find_device", return_value=(12, 7)),
            patch("vmctl.list_vms", return_value=[{"name": "same"}, {"name": "same"}]),
            patch("vmctl.provision_command") as provision,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(vmctl.sync_all(self.config, self.args), 1)
        provision.assert_not_called()


if __name__ == "__main__":
    unittest.main()
