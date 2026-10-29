import contextlib
import io
import unittest
from unittest.mock import patch

import cloudinit
import creation_report
import netbox


class CreationReportTests(unittest.TestCase):
    def setUp(self):
        self.vm = {"name": "mvm", "uuid": "00000000-0000-4000-8000-000000000001",
                   "source": "cloud_image", "image": "/images-ro/ubuntu.img", "hardware": "small",
                   "vcpus": 4, "memory_mb": 8000, "disk_gb": 60, "bridge": "br4092",
                   "display": {"type": "vnc", "listen": "127.0.0.1", "port": "auto", "password": "DISPLAY_SECRET"},
                   "interfaces": [{"name": "eth0", "bridge": "br4092", "host_dev": "vm-mvm-123456",
                                   "mac_address": "52:54:00:c8:ae:36", "addresses": ["192.0.2.40/27"]}]}
        self.cloud = {"dns_servers": ["192.0.2.53"], "root_ssh_authorized_keys": ["ssh-ed25519 KEY_SECRET admin"],
                      "network": {"br4092": {"gateway4": "192.0.2.33", "gateway6": None}}}

    def test_summary_shows_generated_values_and_access_without_secrets(self):
        self.vm["cloud_init_data"] = cloudinit.cloud_init_data(self.vm, self.cloud)
        self.vm["creation_report"] = {"cluster": {"id": 4, "name": "wine-cluster"},
                                      "device": {"id": 5246, "name": "wine"}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            creation_report.print_creation_summary(self.vm, False)
        text = output.getvalue()
        for value in ("4 vCPU, 8000 МиБ RAM", "small", "wine-cluster", "wine", "192.0.2.40/27",
                      "192.0.2.33", "192.0.2.53", "br4092", "vm-mvm-123456", "52:54:00:c8:ae:36",
                      "уникальных ключей 1", "пустой пароль после финального этапа", "ВМ выключена"):
            self.assertIn(value, text)
        self.assertNotIn("KEY_SECRET", text)
        self.assertNotIn("DISPLAY_SECRET", text)

    def test_report_distinguishes_context_overrides_and_host_route_fallback(self):
        record = {"id": 1, "device": {"id": 5246}, "cluster": {"id": 4},
                  "config_context": {"vmctl": {"cloud_init": self.cloud}},
                  "local_context_data": {"vmctl": {"cloud_init": {
                      "root_password": "ROOT_SECRET", "dns_servers": ["192.0.2.54"],
                      "unsupported_option": "IGNORED_SECRET"}}}}
        host = {"id": 5246, "name": "wine", "local_context_data": {"vmctl": {"cloud_init": {
            "network": {"br4092": {"gateway4": "192.0.2.34"}}}}}}
        with patch("cloudinit._request", return_value=host), \
                patch("cloudinit.root_ssh_authorized_keys", return_value=self.cloud["root_ssh_authorized_keys"]), \
                patch("netbox._list_results", side_effect=netbox.NetBoxError("HTTP 403")):
            self.vm["cloud_init_data"] = cloudinit.cloud_init_from_netbox({}, record, self.vm)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            creation_report.print_creation_summary(self.vm, True)
        self.assertIn("План создания ВМ: mvm", output.getvalue())
        self.assertIn("192.0.2.34 [локальный Context Device: network.br4092.gateway4]", output.getvalue())
        self.assertIn("192.0.2.54 [локальный Context VM: dns_servers]", output.getvalue())
        self.assertIn("Имена Config Contexts: недоступны API", output.getvalue())
        self.assertIn("Неиспользуемые поля cloud_init: unsupported_option", output.getvalue())
        self.assertIn("Консоль root: заданный пароль будет установлен cloud-init; значение скрыто", output.getvalue())
        self.assertNotIn("ROOT_SECRET", output.getvalue())
        self.assertNotIn("IGNORED_SECRET", output.getvalue())
        report = {}
        settings = {**self.cloud, "network": {}}
        with patch("cloudinit.bridge_gateway", return_value="192.0.2.33"):
            cloudinit.cloud_init_data(self.vm, settings, report=report)
        self.assertEqual(report["network_sources"]["eth0"]["gateway4"], "маршруты хоста через br4092")

    def test_context_names_do_not_confuse_cluster_with_cluster_type(self):
        record = {"id": 1, "device": {"id": 5246}, "cluster": {"id": 4}, "site": {"id": 10}}
        base = {"is_active": True, "weight": 1000, "data": {"vmctl": {"cloud_init": self.cloud}}}
        contexts = [{**base, "id": 1, "name": "wine-network", "clusters": [{"id": 4}]},
                    {**base, "id": 2, "name": "other-cluster", "clusters": [{"id": 5}]},
                    {**base, "id": 3, "name": "shared-QEMU-KVM", "cluster_types": [{"id": 2}]},
                    {**base, "id": 4, "name": "wrong-site", "clusters": [{"id": 4}], "sites": [{"id": 20}]},
                    {**base, "id": 5, "name": "inactive", "is_active": False},
                    {**base, "id": 6, "name": "root-console", "clusters": [{"id": 4}],
                     "data": {"vmctl": {"cloud_init": {"root_password": "ROOT_SECRET"}}}}]
        with patch("netbox._list_results", return_value=contexts):
            result = creation_report.context_names({}, record, {})
        self.assertEqual([item["name"] for item in result["matched"]], ["root-console", "wine-network"])
        self.assertNotIn("ROOT_SECRET", str(result))
        self.assertEqual([item["name"] for item in result["unresolved"]], ["shared-QEMU-KVM"])

    def test_hidden_context_names_do_not_block_creation_settings(self):
        with patch("netbox._list_results", side_effect=netbox.NetBoxError("HTTP 403")):
            self.assertFalse(creation_report.context_names({}, {}, {})["available"])

    def test_missing_gateway_and_dns_are_visible(self):
        settings = {"root_ssh_authorized_keys": self.cloud["root_ssh_authorized_keys"],
                    "network": {"eth0": {"gateway4": None}}}
        self.vm["cloud_init_data"] = cloudinit.cloud_init_data(self.vm, settings)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            creation_report.print_creation_summary(self.vm, False)
        self.assertIn("Шлюз IPv4: не задан", output.getvalue())
        self.assertIn("DNS: не заданы", output.getvalue())

    def test_custom_local_user_data_is_not_dumped(self):
        self.vm["user_data"] = "/private/user-data"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            creation_report.print_creation_summary(self.vm, True)
        self.assertIn("/private/user-data; содержимое не выводится", output.getvalue())
