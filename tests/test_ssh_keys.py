import unittest
from unittest.mock import patch

import netbox


class RootSSHKeysTests(unittest.TestCase):
    def test_collects_three_levels_once_and_context_wins(self):
        record = {"id": 1, "device": {"id": 2}, "cluster": {"id": 3},
                  "local_context_data": {"vmctl": {"cloud_init": {
                      "root_ssh_authorized_keys": ["ssh-ed25519 AAAA explicit comment"]}}}}
        assignment = {"role": {"slug": "coreroot"}, "contact": {"id": 10}}
        other = {"role": {"slug": "support"}, "contact": {"id": 11}}
        contact = {"name": "Иван Петров", "custom_fields": {
            "sshpublickey": "ssh-ed25519 AAAA old comment\nssh-ed25519 BBBB old"}}
        with patch("netbox._list_results", return_value=[assignment, other]) as lists, \
                patch("netbox._request", return_value=contact) as request:
            self.assertEqual(netbox.root_ssh_authorized_keys({}, record), [
                "ssh-ed25519 AAAA explicit comment", "ssh-ed25519 BBBB Иван Петров"])
        self.assertEqual(lists.call_count, 3)
        self.assertIn("object_type=dcim.device&object_id=2", lists.call_args_list[1].args[1])
        self.assertIn("object_type=virtualization.cluster&object_id=3", lists.call_args_list[2].args[1])
        request.assert_called_once_with({}, "GET", "tenancy/contacts/10/")

    def test_rendered_context_and_exact_preservation(self):
        key = "ssh-ed25519 AAAA  original comment  "
        record = {"id": 1, "config_context": {"vmctl": {"cloud_init": {
            "root_ssh_authorized_keys": [key]}}}}
        with patch("netbox._list_results", return_value=[]):
            self.assertEqual(netbox.root_ssh_authorized_keys({}, record), [key])

    def test_invalid_context_keys_are_rejected(self):
        for value in ("ssh-ed25519 AAAA", ["ssh-ed25519 AAAA\nssh-ed25519 BBBB"], [None]):
            record = {"local_context_data": {"vmctl": {"cloud_init": {
                "root_ssh_authorized_keys": value}}}}
            with self.subTest(value=value), self.assertRaises(netbox.NetBoxError):
                netbox.root_ssh_authorized_keys({}, record)
