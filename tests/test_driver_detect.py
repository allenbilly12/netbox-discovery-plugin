import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "netbox_discovery" / "discovery" / "driver_detect.py"


def load_module():
    spec = importlib.util.spec_from_file_location("driver_detect_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DriverDetectTests(unittest.TestCase):
    def setUp(self):
        self.driver_detect = load_module()
        self.driver_detect._UNAVAILABLE_DRIVERS.clear()

    def test_missing_driver_is_marked_unavailable(self):
        fake_napalm = types.ModuleType("napalm")

        def fake_get_network_driver(_name):
            raise ModuleNotFoundError('Cannot import "fortios". Is the library installed?')

        fake_napalm.get_network_driver = fake_get_network_driver
        messages = []

        with mock.patch.dict(sys.modules, {"napalm": fake_napalm}):
            result = self.driver_detect._try_driver(
                "fortios",
                "192.0.2.1",
                "user",
                "pass",
                "",
                10,
                messages.append,
            )

        self.assertIsNone(result)
        self.assertIn("fortios", self.driver_detect._UNAVAILABLE_DRIVERS)
        self.assertTrue(any("unavailable" in message for message in messages))

    def test_auto_detect_skips_cached_unavailable_driver(self):
        attempted = []
        self.driver_detect._UNAVAILABLE_DRIVERS.add("fortios")

        def fake_try_driver_timed(driver_name, *args, **kwargs):
            attempted.append(driver_name)
            return None

        with mock.patch.object(self.driver_detect, "_try_driver_timed", side_effect=fake_try_driver_timed):
            device, driver = self.driver_detect.detect_and_connect(
                ip="192.0.2.10",
                username="user",
                password="pass",
                timeout=5,
                log_fn=lambda msg: None,
            )

        self.assertIsNone(device)
        self.assertIsNone(driver)
        self.assertNotIn("fortios", attempted)
        self.assertEqual(attempted, ["ios", "nxos_ssh", "junos", "eos"])


if __name__ == "__main__":
    unittest.main()
