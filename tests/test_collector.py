import importlib.util
import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "netbox_discovery" / "discovery" / "collector.py"


def load_module():
    spec = importlib.util.spec_from_file_location("collector_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ParseCdpNeighborsTests(unittest.TestCase):
    def setUp(self):
        self.collector = load_module()

    def test_prefers_management_address_over_entry_address(self):
        output = """
Device ID: edge-switch-1
Entry address(es):
  IP address: 192.168.26.3
Platform: cisco C9200-48P,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/1,  Port ID (outgoing port): Gi1/0/24
Management address(es):
  IP address: 10.70.52.3
"""

        parsed = self.collector._parse_cdp_neighbors(output)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["remote_ip"], "10.70.52.3")
        self.assertNotIn("_management_ips", parsed[0])
        self.assertNotIn("_candidate_ips", parsed[0])

    def test_falls_back_to_entry_address_when_management_missing(self):
        output = """
Device ID: access-switch-1
Entry address(es):
  IP address: 10.20.30.40
Platform: cisco WS-C2960,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/2,  Port ID (outgoing port): Gi0/1
"""

        parsed = self.collector._parse_cdp_neighbors(output)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["remote_ip"], "10.20.30.40")


if __name__ == "__main__":
    unittest.main()
