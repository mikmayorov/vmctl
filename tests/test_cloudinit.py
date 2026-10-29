import contextlib
import io
import json
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cloudinit
import netbox
import vmctl
import vmcreate


class CloudInitTests(unittest.TestCase):
    def setUp(self):
        self.vm = {"name": "guest", "uuid": "00000000-0000-0000-0000-000000000001",
                   "source": "cloud_image", "image": "/images-ro/ubuntu.img",
                   "vcpus": 2, "memory_mb": 2048, "disk_gb": 20,
                   "bridge": "br0", "storage_directory": "/images",
                   "interfaces": [{"name": "eth0", "bridge": "br0",
                                   "mac_address": "52:54:00:00:00:01",
                                   "addresses": ["192.0.2.10/24", "2001:db8::10/64"]}]}
        self.settings = {"root_ssh_authorized_keys": ["ssh-ed25519 AAAA user"],
                         "network": {"eth0": {"gateway4": "192.0.2.1", "gateway6": "2001:db8::1",
                                              "dns_servers": ["192.0.2.53"]}}}

    def test_root_keys_static_dual_stack_and_instance_identity(self):
        data = cloudinit.cloud_init_data(self.vm, self.settings)
        user = json.loads(data["user-data"].split("\n", 1)[1])
        self.assertEqual(user["users"][0]["name"], "root")
        self.assertEqual(user["users"][0]["ssh_authorized_keys"], self.settings["root_ssh_authorized_keys"])
        self.assertFalse(user["disable_root"])
        self.assertFalse(user["ssh_pwauth"])
        self.assertIn(["passwd", "-d", "root"], user["runcmd"])
        self.assertEqual(json.loads(data["meta-data"])["instance-id"], self.vm["uuid"])
        nic = json.loads(data["network-config"])["ethernets"]["nic0"]
        self.assertEqual(nic["match"]["macaddress"], "52:54:00:00:00:01")
        self.assertFalse(nic["dhcp4"])
        self.assertFalse(nic["accept-ra"])
        self.assertEqual(len(nic["routes"]), 2)
        self.assertEqual(nic["nameservers"]["addresses"], ["192.0.2.53"])

    def test_absent_or_empty_root_password_selects_passwordless_console(self):
        for settings in (self.settings, {**self.settings, "root_password": ""}):
            with self.subTest(explicit="root_password" in settings):
                data = cloudinit.cloud_init_data(self.vm, settings)
                user = json.loads(data["user-data"].split("\n", 1)[1])
                self.assertEqual(user["runcmd"], [["passwd", "-d", "root"]])
                self.assertNotIn("chpasswd", user)
                self.assertFalse(user["ssh_pwauth"])

    def test_root_password_is_literal_data_not_a_hash_random_password_or_shell_command(self):
        for password in ("RANDOM", '$6$literal: "пароль"; $(touch /tmp/never) '):
            with self.subTest(password=password):
                data = cloudinit.cloud_init_data(self.vm, {**self.settings, "root_password": password})
                user = json.loads(data["user-data"].split("\n", 1)[1])
                self.assertEqual(user["chpasswd"], {"expire": False, "users": [
                    {"name": "root", "password": password, "type": "text"}]})
                self.assertNotIn("runcmd", user)
                self.assertFalse(user["ssh_pwauth"])
                self.assertEqual(user["users"][0]["ssh_authorized_keys"], self.settings["root_ssh_authorized_keys"])

    def test_invalid_root_password_is_rejected_without_echoing_it(self):
        for password in (None, False, 42, "secret\nvalue", "secret\rvalue", "secret\0value"):
            with self.subTest(password=password):
                with self.assertRaisesRegex(netbox.NetBoxError, "vmctl.cloud_init.root_password") as error:
                    cloudinit.cloud_init_data(self.vm, {**self.settings, "root_password": password})
                self.assertNotIn("secret", str(error.exception))

    def test_root_password_context_precedence_and_explicit_empty_override(self):
        for host_password, local, expected in (
            (None, {}, "CLUSTER_SECRET"),
            ("DEVICE_SECRET", {}, "DEVICE_SECRET"),
            ("DEVICE_SECRET", {"root_password": "VM_SECRET"}, "VM_SECRET"),
            ("DEVICE_SECRET", {"root_password": ""}, ""),
        ):
            with self.subTest(host=host_password, local=local):
                record = {"device": {"id": 2}, "config_context": {"vmctl": {"cloud_init": {
                    **self.settings, "root_password": "CLUSTER_SECRET"}}},
                    "local_context_data": {"vmctl": {"cloud_init": local}}}
                host = {"local_context_data": {"vmctl": {"cloud_init": {
                    "root_password": host_password}}}} if host_password else {}
                with patch("cloudinit._request", return_value=host), \
                        patch("cloudinit.root_ssh_authorized_keys", return_value=self.settings["root_ssh_authorized_keys"]), \
                        patch("cloudinit.context_names", return_value={"available": False, "matched": [], "unresolved": []}):
                    data = cloudinit.cloud_init_from_netbox({}, record, self.vm)
                user = json.loads(data["user-data"].split("\n", 1)[1])
                if expected:
                    self.assertEqual(user["chpasswd"]["users"][0]["password"], expected)
                else:
                    self.assertEqual(user["runcmd"], [["passwd", "-d", "root"]])
                self.assertNotIn("root_password", self.vm["creation_report"]["ignored_fields"])
                self.assertNotIn("SECRET", json.dumps(self.vm["creation_report"]))

    def test_legacy_root_password_is_rejected_with_migration_hint(self):
        for context in ({"root_password": "SECRET"},
                        {"root_password": "SECRET", "cloud_init": {"root_password": "NEW_SECRET"}}):
            record = {"local_context_data": {"vmctl": context}}
            with self.subTest(context=context):
                with self.assertRaisesRegex(netbox.NetBoxError, "move vmctl.root_password to vmctl.cloud_init.root_password") as error:
                    cloudinit.cloud_init_from_netbox({}, record, self.vm)
                self.assertNotIn("SECRET", str(error.exception))

    def test_bridge_route_is_gateway_not_bridge_address(self):
        routes = '[{"gateway":"192.0.2.1"},{"gateway":"198.51.100.1"}]'
        with patch("cloudinit.subprocess.run", return_value=SimpleNamespace(stdout=routes)):
            gateway = cloudinit.bridge_gateway("br0", [cloudinit.ipaddress.ip_interface("192.0.2.10/24")], 4)
        self.assertEqual(gateway, "192.0.2.1")

    def test_ambiguous_gateway_fails(self):
        routes = '[{"gateway":"192.0.2.1"},{"gateway":"192.0.2.2"}]'
        with patch("cloudinit.subprocess.run", return_value=SimpleNamespace(stdout=routes)):
            with self.assertRaisesRegex(netbox.NetBoxError, "ambiguous"):
                cloudinit.bridge_gateway("br0", [cloudinit.ipaddress.ip_interface("192.0.2.10/24")], 4)

    def test_missing_key_or_ip_fails(self):
        with self.assertRaisesRegex(netbox.NetBoxError, "SSH public key"):
            cloudinit.cloud_init_data(self.vm, {})
        self.vm["interfaces"][0]["addresses"] = []
        with self.assertRaisesRegex(netbox.NetBoxError, "static IP"):
            cloudinit.cloud_init_data(self.vm, self.settings)

    def test_host_cluster_and_vm_context_precedence(self):
        host = {"config_context": {"vmctl": {"cloud_init": {
            "dns_servers": ["192.0.2.53"], "network": {"eth0": {"gateway4": "192.0.2.254"}}}}}}
        record = {"device": {"id": 2}, "config_context": {"vmctl": {"cloud_init": {
            "network": {"eth0": {"gateway4": "192.0.2.1", "gateway6": None}}}}},
            "local_context_data": {"vmctl": {"cloud_init": {"dns_servers": ["192.0.2.54"]}}}}
        with patch("cloudinit._request", return_value=host), patch(
                "cloudinit.root_ssh_authorized_keys", return_value=["ssh-ed25519 AAAA root"]):
            data = cloudinit.cloud_init_from_netbox({}, record, self.vm)
        nic = json.loads(data["network-config"])["ethernets"]["nic0"]
        self.assertEqual(nic["routes"], [{"to": "0.0.0.0/0", "via": "192.0.2.254"}])
        self.assertEqual(nic["nameservers"]["addresses"], ["192.0.2.54"])

    def test_virt_install_first_boot_xml_gets_persistent_seed_and_tap(self):
        self.vm["cloud_init_data"] = cloudinit.cloud_init_data(self.vm, self.settings)
        command = vmcreate.cloud_install_command(self.vm, "qemu:///system",
                  [{"path": "/images/guest.qcow2"}], Path("/state/guest"))
        self.assertIn("--cloud-init", command)
        self.assertIn("--print-xml=1", command)
        self.assertIn("--import", command)
        boot = command[command.index("--boot") + 1]
        self.assertTrue(boot.startswith("uefi,"))
        self.assertIn("firmware.feature0.name=secure-boot", boot)
        self.assertIn("firmware.feature0.enabled=no", boot)
        self.assertIn("network-config=/state/guest/network-config", command[-1])
        xml = '<domain><os firmware="efi"/><on_reboot>destroy</on_reboot><devices>' \
              '<disk device="cdrom"><source file="/tmp/deleted.iso"/></disk>' \
              '<interface type="bridge"/></devices></domain>'
        root = ET.fromstring(vmcreate.persistent_cloud_xml(xml, self.vm,
                            Path("/images/guest.qcow2"), "br0", Path("/images/guest-seed.iso")))
        self.assertEqual(root.find("./devices/disk/source").get("file"), "/images/guest-seed.iso")
        self.assertEqual(root.find("./devices/interface/alias").get("name"), vmcreate.interface_alias("eth0"))
        self.assertEqual(root.findtext("on_reboot"), "restart")

    def test_dry_run_create_reads_cloud_keys_and_never_writes_or_starts(self):
        record = {"id": 1, "status": "planned", "local_context_data": {"vmctl": {"source": "cloud_image"}}}
        args = Namespace(command="create", target="guest", dry_run=True)
        config = {"host": {"libvirt_uri": "qemu:///system"}, "netbox": {"key": "secret"}}
        with patch("vmctl.find_device", return_value=(2, 3)), \
                patch("vmctl.get_vm", return_value=record), \
                patch("vmctl.local_names", return_value=[]), \
                patch("vmctl.verify_vm_serial", return_value=self.vm["uuid"]), \
                patch("vmctl.vm_interfaces", return_value=[]), \
                patch("vmctl.local_spec_from_netbox", return_value=dict(self.vm)), \
                patch("vmctl.cloud_init_from_netbox", return_value={"user-data": "data"}) as cloud, \
                patch("vmctl.subprocess.run", return_value=SimpleNamespace(stdout="")), \
                patch("vmctl.patch_vm") as write, patch("vmctl.create_vm", return_value=0) as create:
            self.assertEqual(vmctl.provision_command(config, args), 0)
        cloud.assert_called_once()
        write.assert_not_called()
        self.assertEqual(create.call_args.args[0]["cloud_init_data"], {"user-data": "data"})
        self.assertTrue(create.call_args.args[-1])

    def test_creation_writes_all_nocloud_inputs_and_defines_without_start(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / "ubuntu.img"
            source.touch()
            vm = {**self.vm, "image": str(source), "storage_directory": directory,
                  "cloud_init_data": cloudinit.cloud_init_data(self.vm, self.settings)}
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[0] == "xorriso":
                    Path(command[command.index("-o") + 1]).write_bytes(b"seed")
                if command[0] == "virt-install":
                    return SimpleNamespace(stdout=vmcreate.domain_xml(
                        vm, project / "guest.qcow2", "br0", Path("/tmp/temporary.iso")))
                if command[:2] == ["qemu-img", "info"]:
                    return SimpleNamespace(stdout='{"virtual-size":1073741824}')
                return SimpleNamespace(stdout="")

            with patch("vmcreate.subprocess.run", side_effect=run), \
                    patch("vmcreate.shutil.which", return_value="/usr/bin/tool"), \
                    patch.object(Path, "is_dir", return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()):
                vmcreate.create_vm(vm, {"host": {"libvirt_uri": "qemu:///system"}}, project, False)
            seed_dir = project / "state/cloud-init/guest"
            for name, expected in vm["cloud_init_data"].items():
                self.assertEqual((seed_dir / name).read_text(), expected)
                self.assertEqual((seed_dir / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(seed_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((project / "guest-seed.iso").stat().st_mode & 0o777, 0o600)
            xml = ET.parse(project / "state/domains/guest.xml")
            self.assertEqual(xml.find("./devices/disk[@device='cdrom']/source").get("file"),
                             str(project / "guest-seed.iso"))
            self.assertIn("--cloud-init", next(command for command in calls if command[0] == "virt-install"))
            self.assertEqual(calls[-1][:4], ["virsh", "-c", "qemu:///system", "define"])
            self.assertFalse(any("start" in command for command in calls))

    def test_all_netbox_interface_addresses_are_carried_to_cloud_init(self):
        record = {"id": 1, "name": "guest", "serial": self.vm["uuid"], "memory": 2048,
                  "vcpus": 2, "disk": 20480, "local_context_data": {"vmctl": {
                      "version": 4, "source": "cloud_image", "image": self.vm["image"],
                      "storage_directory": "/images", "interfaces": {"eth0": {
                          "host_dev": "vm-guest-123456", "bridge": "br0"}}}}}
        nic = {"id": 10, "name": "eth0", "primary_mac_address": {"mac_address": "52:54:00:00:00:01"}}
        addresses = [{"id": 20, "address": "192.0.2.10/24"}, {"id": 21, "address": "2001:db8::10/64"}]
        with patch("netbox.vm_interfaces", return_value=[nic]), \
                patch("netbox.vm_disks", return_value=[{"name": "guest", "size": 20480}]), \
                patch("netbox.interface_ips", return_value=addresses):
            spec = netbox.local_spec_from_netbox(record, {})
        self.assertEqual(spec["interfaces"][0]["addresses"], [value["address"] for value in addresses])
        self.assertEqual(spec["mounted_media"], [{"path": "/images/guest-seed.iso", "target": "sda"}])

    def test_distro_examples_round_trip_through_netbox_to_nocloud(self):
        project = Path(__file__).resolve().parents[1]
        config = tomllib.loads((project / "config.example.toml").read_text())
        for distro in ("ubuntu", "debian", "arch"):
            with self.subTest(distro=distro):
                request = tomllib.loads((project / "examples" / f"{distro}-cloud.toml").read_text())
                vm = vmctl.resolve_creation_spec(request, config)
                with patch("netbox.find_vm", return_value=None), \
                        patch("netbox._request", side_effect=lambda c, m, p, data: {"id": 1, **data}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    record = netbox.reserve_vm(config, vm, cluster_id=3, device_id=2)
                nic = {"id": 10, "name": "eth0", "primary_mac_address": {"mac_address": "52:54:00:00:00:01"}}
                with patch("netbox.vm_interfaces", return_value=[nic]), \
                        patch("netbox.vm_disks", return_value=[{"name": vm["name"], "size": 20480}]), \
                        patch("netbox.interface_ips", return_value=[{"id": 20, "address": "192.0.2.10/24"}]):
                    spec = netbox.local_spec_from_netbox(record, config)
                self.assertEqual(spec["image"], vm["image"])
                self.assertEqual(spec["hardware"], vm["hardware"])
                data = cloudinit.cloud_init_data(spec, self.settings)
                user = json.loads(data["user-data"].split("\n", 1)[1])
                self.assertEqual(user["users"][0]["name"], "root")
                self.assertEqual(user["users"][0]["ssh_authorized_keys"], self.settings["root_ssh_authorized_keys"])
                self.assertFalse(user["ssh_pwauth"])
                self.assertIn(["passwd", "-d", "root"], user["runcmd"])
                self.assertNotIn("system_info", user)  # The image selects its own distro/backend.
                self.assertEqual(json.loads(data["meta-data"])["local-hostname"], vm["name"])
                network = json.loads(data["network-config"])
                self.assertEqual(network["ethernets"]["nic0"]["addresses"], ["192.0.2.10/24"])
                self.assertEqual(network["ethernets"]["nic0"]["routes"], [
                    {"to": "0.0.0.0/0", "via": "192.0.2.1"}])
