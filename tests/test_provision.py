import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import netbox
import vmctl


class ProvisionBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.config = {"host": {"libvirt_uri": "qemu:///system"},
                       "netbox": {"key": "secret", "device": "host"},
                       "storage": {"directory": "/images"}, "network": {"bridge": "br0"}}
        self.uuid = "00000000-0000-4000-8000-000000000001"
        self.record = {"id": 1, "name": "guest", "serial": self.uuid, "status": "planned",
                       "local_context_data": {"vmctl": {"version": 4, "source": "cloud_image"}}}
        self.spec = {"name": "guest", "source": "cloud_image", "image": "/images-ro/base.img",
                     "vcpus": 2, "memory_mb": 2048, "disk_gb": 20,
                     "bridge": "br0", "storage_directory": "/images"}

    def test_cli_distinguishes_prepare_file_create_target_and_sync_name(self):
        parser = vmctl.build_parser()
        self.assertEqual(parser.parse_args(["prepare", "local/guest.toml"]).spec, Path("local/guest.toml"))
        self.assertEqual(parser.parse_args(["create", "guest"]).target, "guest")
        self.assertEqual(parser.parse_args(["create", "local/guest.toml"]).target, "local/guest.toml")
        self.assertEqual(parser.parse_args(["sync", "guest"]).vm, "guest")

    def test_prepare_creates_only_netbox_without_local_or_cloud_init_access(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "guest.toml"
            spec.write_text('[vm]\nname="guest"\nsource="cloud_image"\nimage="/images-ro/base.img"\n'
                            'vcpus=2\nmemory_mb=2048\ndisk_gb=20\n')
            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run), \
                        patch("vmctl.find_device", return_value=(2, 3)), \
                        patch("vmctl.find_vm", return_value=None), \
                        patch("vmctl.get_vm", return_value=self.record), \
                        patch("vmctl.reserve_vm") as reserve, \
                        patch("vmctl.create_vm_components") as components, \
                        patch("vmctl.local_names") as local, \
                        patch("vmctl.create_vm") as create, \
                        patch("vmctl.cloud_init_from_netbox") as cloud, \
                        patch("vmctl.subprocess.run") as run, \
                        contextlib.redirect_stdout(io.StringIO()):
                    args = Namespace(command="prepare", spec=spec, dry_run=dry_run)
                    self.assertEqual(vmctl.provision_command(self.config, args), 0)
                self.assertEqual(reserve.call_count, int(not dry_run))
                self.assertEqual(components.call_count, int(not dry_run))
                local.assert_not_called()
                create.assert_not_called()
                cloud.assert_not_called()
                run.assert_not_called()

    def test_managed_create_does_not_accept_toml_or_fall_back_to_prepare(self):
        with patch("vmctl.reserve_vm") as reserve, patch("vmctl.get_vm") as get, \
                patch("vmctl.create_vm") as create, contextlib.redirect_stderr(io.StringIO()) as errors:
            args = Namespace(command="create", target="local/guest.toml", dry_run=False)
            self.assertEqual(vmctl.provision_command(self.config, args), 1)
        reserve.assert_not_called()
        get.assert_not_called()
        create.assert_not_called()
        self.assertIn("requires a VM name", errors.getvalue())

    def test_create_missing_netbox_record_never_prepares(self):
        with patch("vmctl.find_device", return_value=(2, 3)), \
                patch("vmctl.get_vm", side_effect=netbox.NetBoxError("VM does not exist in NetBox")), \
                patch("vmctl.reserve_vm") as reserve, patch("vmctl.create_vm") as create, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(vmctl.provision_command(self.config, Namespace(
                command="create", target="guest", dry_run=False)), 1)
        reserve.assert_not_called()
        create.assert_not_called()
        self.assertIn("vmctl prepare", errors.getvalue())

    def test_create_existing_local_vm_never_redeploys_even_in_dry_run(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), patch("vmctl.find_device", return_value=(2, 3)), \
                    patch("vmctl.get_vm", return_value=self.record), \
                    patch("vmctl.local_names", return_value=["guest"]), \
                    patch("vmctl.create_vm") as create, patch("vmctl.patch_vm") as write, \
                    patch("vmctl.cloud_init_from_netbox") as cloud, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(vmctl.provision_command(self.config, Namespace(
                    command="create", target="guest", dry_run=dry_run)), 1)
            create.assert_not_called()
            write.assert_not_called()
            cloud.assert_not_called()

    def test_sync_missing_vm_has_no_side_effects_for_any_source(self):
        for source in ("iso", "cloud_image", "existing"):
            for dry_run in (False, True):
                record = {**self.record, "local_context_data": {"vmctl": {"source": source}}}
                with self.subTest(source=source, dry_run=dry_run), \
                        patch("vmctl.find_device", return_value=(2, 3)), \
                        patch("vmctl.get_vm", return_value=record), \
                        patch("vmctl.local_names", return_value=[]), \
                        patch("vmctl.ensure_primary_mac") as mac, \
                        patch("vmctl.create_vm") as create, patch("vmctl.redefine_vm") as redefine, \
                        patch("vmctl.patch_vm") as write, patch("vmctl.cloud_init_from_netbox") as cloud, \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(vmctl.provision_command(self.config, Namespace(
                        command="sync", vm="guest", dry_run=dry_run, purge=True)), 1)
                for operation in (mac, create, redefine, write, cloud):
                    operation.assert_not_called()
                self.assertIn("restore" if source == "existing" else "vmctl create guest", errors.getvalue())

    def test_cloud_create_deploys_once_and_never_starts_even_for_active_status(self):
        record = {**self.record, "status": "active"}
        with patch("vmctl.find_device", return_value=(2, 3)), patch("vmctl.get_vm", return_value=record), \
                patch("vmctl.local_names", return_value=[]), \
                patch("vmctl.local_spec_from_netbox", return_value=dict(self.spec)), \
                patch("vmctl.cloud_init_from_netbox", return_value={"user-data": "keys"}) as cloud, \
                patch("vmctl.create_vm", return_value=0) as create, patch("vmctl.patch_vm") as write, \
                patch("vmctl.reconcile_power") as power, patch("vmctl.reserve_vm") as reserve, \
                patch("vmctl.create_vm_components") as components:
            self.assertEqual(vmctl.provision_command(self.config, Namespace(
                command="create", target="guest", dry_run=False)), 0)
        cloud.assert_called_once()
        create.assert_called_once()
        self.assertEqual(create.call_args.args[0]["uuid"], self.uuid)
        self.assertEqual(create.call_args.args[0]["cloud_init_data"], {"user-data": "keys"})
        for operation in (write, power, reserve, components):
            operation.assert_not_called()

    def test_sync_existing_cloud_guest_does_not_read_keys_or_deploy_image(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), patch("vmctl.find_device", return_value=(2, 3)), \
                    patch("vmctl.get_vm", return_value={**self.record, "status": "staged"}), \
                    patch("vmctl.local_names", return_value=["guest"]), \
                    patch("vmctl.local_uuid", return_value=self.uuid), \
                    patch("vmctl.vm_interfaces", return_value=[]), \
                    patch("vmctl.local_spec_from_netbox", return_value=dict(self.spec)), \
                    patch("vmctl.verify_local_vm"), patch("vmctl.create_vm") as create, \
                    patch("vmctl.cloud_init_from_netbox") as cloud, \
                    patch("vmctl.subprocess.run", return_value=SimpleNamespace(stdout="shut off")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(vmctl.provision_command(self.config, Namespace(
                    command="sync", vm="guest", dry_run=dry_run, purge=False)), 0)
            create.assert_not_called()
            cloud.assert_not_called()
