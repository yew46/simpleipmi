import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from exoanchor_toolkit.errors import ToolkitError
from exoanchor_toolkit.http_device import HttpNetworkProvisioner
from exoanchor_toolkit.network import NetworkProvisioner
from exoanchor_toolkit.network_configure import NetworkConfiguration, _stage_configuration


class NetworkDnsTests(unittest.TestCase):
    def test_invalid_dns_never_reaches_either_transport(self):
        for value in ("0.0.0.0", "0.1.2.3", "127.0.0.1", "224.0.0.1", "240.0.0.1"):
            for field in ("dns_primary", "dns_secondary"):
                with self.subTest(value=value, field=field):
                    console = Mock()
                    console.run.return_value = SimpleNamespace(output="result=ESP_OK")
                    client = Mock()
                    client.post_json.return_value = {"ok": True}
                    for provisioner in (NetworkProvisioner(console), HttpNetworkProvisioner(client)):
                        with self.subTest(transport=type(provisioner).__name__):
                            with self.assertRaises(ToolkitError):
                                provisioner.stage_static("192.0.2.50", "255.255.255.0", **{field: value})
                    console.run.assert_not_called()
                    client.post_json.assert_not_called()

    def test_invalid_dns_is_rejected_before_staging_hostname(self):
        for field in ("dns_primary", "dns_secondary"):
            with self.subTest(field=field):
                provisioner = Mock()
                configuration = NetworkConfiguration(
                    mode="static", hostname="exoanchor-renamed", address="192.0.2.50",
                    netmask="255.255.255.0", **{field: "224.0.0.1"},
                )
                with self.assertRaises(ToolkitError):
                    _stage_configuration(provisioner, configuration)
                self.assertEqual(provisioner.method_calls, [])

    def test_optional_public_and_link_local_dns_remain_supported(self):
        for value, expected in (("", "-"), ("-", "-"), (" NONE ", "-"),
                                (" 1.1.1.1 ", "1.1.1.1"), ("169.254.1.1", "169.254.1.1")):
            with self.subTest(value=value):
                console = Mock()
                console.run.return_value = SimpleNamespace(output="result=ESP_OK")
                NetworkProvisioner(console).stage_static(
                    "192.0.2.50", "255.255.255.0", dns_primary=value,
                )
                console.run.assert_called_once_with(
                    f"network static 192.0.2.50 255.255.255.0 - {expected} -"
                )


if __name__ == "__main__":
    unittest.main()
