import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "netbox_discovery" / "sync" / "netbox_sync.py"


def load_module():
    spec = importlib.util.spec_from_file_location("netbox_sync_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None

    fake_pkg = types.ModuleType("netbox_discovery")
    fake_sync_pkg = types.ModuleType("netbox_discovery.sync")
    fake_classify = types.ModuleType("netbox_discovery.sync.classify")
    fake_classify.classify_device = lambda **kwargs: {}

    with mock.patch.dict(
        sys.modules,
        {
            "netbox_discovery": fake_pkg,
            "netbox_discovery.sync": fake_sync_pkg,
            "netbox_discovery.sync.classify": fake_classify,
        },
    ):
        spec.loader.exec_module(module)
    return module


def _matches(obj, criteria):
    for key, expected in criteria.items():
        if key.endswith("__iexact"):
            attr = key[:-8]
            actual = getattr(obj, attr, "")
            if str(actual).lower() != str(expected).lower():
                return False
            continue
        actual = getattr(obj, key, None)
        if actual != expected:
            return False
    return True


class FakeQuerySet(list):
    def first(self):
        return self[0] if self else None

    def exclude(self, **criteria):
        return FakeQuerySet([obj for obj in self if not _matches(obj, criteria)])

    def count(self):
        return len(self)


class FakeVRF:
    objects = None

    def __init__(self, pk, name, rd=""):
        self.pk = pk
        self.name = name
        self.rd = rd
        self.save_calls = 0

    def save(self):
        self.save_calls += 1


class FakeVRFManager:
    def __init__(self, rows):
        self.rows = rows
        self.next_pk = max((row.pk for row in rows), default=0) + 1

    def filter(self, **criteria):
        return FakeQuerySet([row for row in self.rows if _matches(row, criteria)])

    def create(self, **kwargs):
        row = FakeVRF(pk=self.next_pk, name=kwargs["name"], rd=kwargs.get("rd", ""))
        self.next_pk += 1
        self.rows.append(row)
        return row


class SyncVrfsTests(unittest.TestCase):
    def setUp(self):
        self.netbox_sync = load_module()

    def _run_sync(self, rows, vrfs_raw):
        manager = FakeVRFManager(rows)
        FakeVRF.objects = manager

        fake_ipam = types.ModuleType("ipam")
        fake_ipam_models = types.ModuleType("ipam.models")
        fake_ipam_models.VRF = FakeVRF
        messages = []

        with mock.patch.dict(sys.modules, {"ipam": fake_ipam, "ipam.models": fake_ipam_models}):
            self.netbox_sync._sync_vrfs(vrfs_raw, messages.append)

        return messages

    def test_skips_placeholder_rd_when_creating_new_vrf(self):
        rows = []

        self._run_sync(
            rows,
            {"blue": {"state": {"route_distinguisher": "0:0"}}},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "blue")
        self.assertEqual(rows[0].rd, "")

    def test_uses_first_duplicate_name_match_instead_of_raising(self):
        rows = [
            FakeVRF(pk=1, name="Corp", rd=""),
            FakeVRF(pk=2, name="corp", rd=""),
        ]

        messages = self._run_sync(
            rows,
            {"CORP": {"state": {"route_distinguisher": "65000:10"}}},
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].rd, "65000:10")
        self.assertEqual(rows[0].save_calls, 1)
        self.assertTrue(any("Found 2 existing VRFs named 'CORP'" in msg for msg in messages))

    def test_skips_duplicate_rd_used_by_another_vrf(self):
        rows = [FakeVRF(pk=1, name="Shared", rd="65000:99")]

        messages = self._run_sync(
            rows,
            {"Blue": {"state": {"route_distinguisher": "65000:99"}}},
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].name, "Blue")
        self.assertEqual(rows[1].rd, "")
        self.assertTrue(any("Skipping RD '65000:99' for VRF 'Blue'" in msg for msg in messages))
