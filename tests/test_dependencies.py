import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vmcreate
import vmctl


class CloudDependencyTests(unittest.TestCase):
    def test_check_reports_missing_virt_install_despite_healthy_host_and_netbox(self):
        config = {"host": {"libvirt_uri": "qemu:///system"}, "storage": {"directory": "/images"},
                  "network": {"bridge": "br4092"}, "netbox": {"key": "secret"}}
        with patch("vmctl.shutil.which", side_effect=lambda tool: None if tool == "virt-install" else f"/bin/{tool}"), \
                patch("vmcreate.platform.freedesktop_os_release", return_value={"ID": "gentoo"}), \
                patch("vmctl.local_names", return_value=[f"guest-{index}" for index in range(13)]), \
                patch("vmctl.find_device", return_value=(5246, 4)), \
                patch("vmctl.list_vms", return_value=[]), patch.object(Path, "is_dir", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(vmctl.doctor(config), 1)
        self.assertIn("OK libvirt: 13 VM(s)", output.getvalue())
        self.assertIn("OK NetBox VM read access", output.getvalue())
        self.assertIn("MISSING cloud images: virt-install", output.getvalue())
        self.assertIn("emerge --ask app-emulation/virt-manager", output.getvalue())

    def test_check_accepts_alternative_iso_builder(self):
        config = {"host": {"libvirt_uri": "qemu:///system"}, "storage": {"directory": "/images"},
                  "network": {"bridge": "br0"}}
        available = {"virsh", "qemu-img", "virt-install", "xorriso", "ip", "genisoimage"}
        with patch("vmctl.shutil.which", side_effect=lambda tool: f"/bin/{tool}" if tool in available else None), \
                patch("vmctl.local_names", return_value=[]), patch.object(Path, "is_dir", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(vmctl.doctor(config), 0)
        self.assertIn("OK cloud images: xorrisofs / genisoimage / mkisofs", output.getvalue())

    def test_missing_tools_stop_create_and_dry_run_before_any_disk_or_state_write(self):
        for missing in ({"virt-install"}, {"xorriso"}, {"xorrisofs", "genisoimage", "mkisofs"}, {"ip"}):
            for dry_run in (False, True):
                with self.subTest(missing=missing, dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory)
                    vm = {"name": "guest", "source": "cloud_image", "image": "/images-ro/base.img",
                          "vcpus": 2, "memory_mb": 2048, "disk_gb": 20,
                          "bridge": "br0", "storage_directory": directory}
                    with patch("vmcreate.shutil.which", side_effect=lambda tool: None if tool in missing else f"/bin/{tool}"), \
                            patch("vmcreate.subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(ValueError, "cloud image dependencies missing"):
                            vmcreate.create_vm(vm, {"host": {"libvirt_uri": "qemu:///system"}}, path, dry_run)
                    run.assert_not_called()
                    self.assertFalse((path / "guest.qcow2").exists())
                    self.assertFalse((path / "guest-seed.iso").exists())
                    self.assertFalse((path / "state").exists())

    def test_install_hint_uses_host_distro_and_family(self):
        for release, command in (({"ID": "gentoo"}, "emerge --ask app-emulation/virt-manager"),
                                 ({"ID": "ubuntu"}, "apt install virtinst"),
                                 ({"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}, "apt install virtinst"),
                                 ({"ID": "arch"}, "pacman -S virt-install")):
            with self.subTest(release=release), patch("vmcreate.platform.freedesktop_os_release", return_value=release):
                self.assertIn(command, vmcreate.cloud_dependency_hint())

    def test_missing_os_release_keeps_a_useful_generic_hint(self):
        with patch("vmcreate.platform.freedesktop_os_release", side_effect=OSError("unavailable")):
            self.assertIn("пакетным менеджером хоста", vmcreate.cloud_dependency_hint())

    def test_creation_error_hint_installs_tool_instead_of_auditing_nonexistent_guest(self):
        errors = (ValueError("virt-install is required for cloud images"),
                  ValueError("cloud image dependencies missing: virt-install"),
                  ValueError("virt-install --cloud-init needs xorrisofs, genisoimage or mkisofs"),
                  FileNotFoundError(2, "No such file or directory", "ip"))
        with patch("vmcreate.platform.freedesktop_os_release", return_value={"ID": "gentoo"}):
            for error in errors:
                with self.subTest(error=error):
                    hint = vmctl.error_hint(error, "create", "mvm")
                    self.assertIn("emerge --ask", hint)
                    self.assertIn("vmctl create mvm", hint)
                    self.assertNotIn("audit", hint)
