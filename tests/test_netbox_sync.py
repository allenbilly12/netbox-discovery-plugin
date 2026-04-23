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


class FakeM2MManager:
    def __init__(self):
        self._items = []

    def values_list(self, field, flat=False):
        return [getattr(item, field) for item in self._items]

    def add(self, item):
        self._items.append(item)

    def filter(self, **criteria):
        return FakeQuerySet([obj for obj in self._items if _matches(obj, criteria)])

    def all(self):
        return list(self._items)


class FakeVRF:
    objects = None

    def __init__(self, pk, name, rd=""):
        self.pk = pk
        self.name = name
        self.rd = rd
        self.save_calls = 0
        self.import_targets = FakeM2MManager()
        self.export_targets = FakeM2MManager()

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


class FakeRouteTarget:
    objects = None

    def __init__(self, pk, name):
        self.pk = pk
        self.name = name


class FakeRouteTargetManager:
    def __init__(self):
        self.store = {}
        self.next_pk = 1

    def get_or_create(self, name):
        if name in self.store:
            return self.store[name], False
        rt = FakeRouteTarget(pk=self.next_pk, name=name)
        self.next_pk += 1
        self.store[name] = rt
        return rt, True


_FAKE_IFACE_CT = object()  # sentinel — acts as the Interface ContentType


class FakeContentTypeManager:
    def get_for_model(self, model):
        return _FAKE_IFACE_CT


class FakeContentType:
    objects = FakeContentTypeManager()


class FakeDevice:
    def __init__(self, pk=1, name="test-device"):
        self.pk = pk
        self.name = name


class SyncVrfsTests(unittest.TestCase):
    def setUp(self):
        self.netbox_sync = load_module()

    def _run_sync(self, rows, vrfs_raw, device=None, iface_rows=None, ip_rows=None):
        manager = FakeVRFManager(rows)
        FakeVRF.objects = manager
        rt_manager = FakeRouteTargetManager()
        FakeRouteTarget.objects = rt_manager
        FakeInterface.objects = FakeInterfaceManager(iface_rows or [])
        FakeIPAddress.objects = FakeIPAddressManager(ip_rows or [])

        fake_dcim = types.ModuleType("dcim")
        fake_dcim_models = types.ModuleType("dcim.models")
        fake_dcim_models.Interface = FakeInterface
        fake_ipam = types.ModuleType("ipam")
        fake_ipam_models = types.ModuleType("ipam.models")
        fake_ipam_models.VRF = FakeVRF
        fake_ipam_models.RouteTarget = FakeRouteTarget
        fake_ipam_models.IPAddress = FakeIPAddress
        fake_ct = types.ModuleType("django.contrib.contenttypes")
        fake_ct_models = types.ModuleType("django.contrib.contenttypes.models")
        fake_ct_models.ContentType = FakeContentType
        messages = []

        with mock.patch.dict(sys.modules, {
            "dcim": fake_dcim,
            "dcim.models": fake_dcim_models,
            "ipam": fake_ipam,
            "ipam.models": fake_ipam_models,
            "django.contrib.contenttypes": fake_ct,
            "django.contrib.contenttypes.models": fake_ct_models,
        }):
            self.netbox_sync._sync_vrfs(vrfs_raw, device, messages.append)

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

    def test_creates_route_targets_and_links_to_new_vrf(self):
        rows = []

        messages = self._run_sync(
            rows,
            {
                "BLUE": {
                    "state": {"route_distinguisher": "65000:100"},
                    "route_targets": {
                        "import": ["65000:100", "65000:200"],
                        "export": ["65000:100"],
                    },
                }
            },
        )

        vrf = rows[0]
        self.assertEqual(vrf.name, "BLUE")
        self.assertEqual([rt.name for rt in vrf.import_targets.all()], ["65000:100", "65000:200"])
        self.assertEqual([rt.name for rt in vrf.export_targets.all()], ["65000:100"])
        self.assertTrue(any("route target" in msg for msg in messages))

    def test_links_route_targets_to_existing_vrf(self):
        existing = FakeVRF(pk=1, name="RED", rd="65000:10")
        rows = [existing]

        self._run_sync(
            rows,
            {
                "RED": {
                    "state": {"route_distinguisher": "65000:10"},
                    "route_targets": {
                        "import": ["65000:10"],
                        "export": ["65000:10"],
                    },
                }
            },
        )

        self.assertEqual([rt.name for rt in existing.import_targets.all()], ["65000:10"])
        self.assertEqual([rt.name for rt in existing.export_targets.all()], ["65000:10"])

    def test_does_not_duplicate_already_linked_route_target(self):
        existing = FakeVRF(pk=1, name="GREEN", rd="65000:50")
        rows = [existing]
        vrf_data = {
            "GREEN": {
                "state": {"route_distinguisher": "65000:50"},
                "route_targets": {"import": ["65000:50"], "export": []},
            }
        }

        self._run_sync(rows, vrf_data)
        self.assertEqual(len(existing.import_targets.all()), 1)

        self._run_sync(rows, vrf_data)
        self.assertEqual(len(existing.import_targets.all()), 1)

    def test_no_route_targets_when_key_absent(self):
        rows = []

        self._run_sync(
            rows,
            {"PLAIN": {"state": {"route_distinguisher": "65000:1"}}},
        )

        vrf = rows[0]
        self.assertEqual(vrf.import_targets.all(), [])
        self.assertEqual(vrf.export_targets.all(), [])

    def test_assigns_interface_to_vrf(self):
        device = FakeDevice()
        iface = FakeInterface(pk=10, name="Loopback0", device=device, vrf=None, vrf_id=None)
        rows = []

        messages = self._run_sync(
            rows,
            {
                "BLUE": {
                    "state": {"route_distinguisher": "65000:100"},
                    "interfaces": {"interface": {"Loopback0": {}}},
                }
            },
            device=device,
            iface_rows=[iface],
        )

        vrf = rows[0]
        self.assertIs(iface.vrf, vrf)
        self.assertEqual(iface.save_calls, 1)
        self.assertTrue(any("interface" in msg for msg in messages))

    def test_assigns_ip_to_vrf_via_interface(self):
        device = FakeDevice()
        iface = FakeInterface(pk=10, name="Loopback0", device=device, vrf=None, vrf_id=None)
        ip = FakeIPAddress(
            pk=20,
            address="10.0.0.1/32",
            assigned_object_type=_FAKE_IFACE_CT,
            assigned_object_id=10,
            vrf=None,
            vrf_id=None,
        )
        rows = []

        self._run_sync(
            rows,
            {
                "BLUE": {
                    "state": {"route_distinguisher": "65000:100"},
                    "interfaces": {"interface": {"Loopback0": {}}},
                }
            },
            device=device,
            iface_rows=[iface],
            ip_rows=[ip],
        )

        vrf = rows[0]
        self.assertIs(ip.vrf, vrf)
        self.assertEqual(ip.save_calls, 1)

    def test_does_not_overwrite_interface_already_in_different_vrf(self):
        device = FakeDevice()
        other_vrf = FakeVRF(pk=99, name="OTHER")
        iface = FakeInterface(pk=10, name="Loopback0", device=device, vrf=other_vrf, vrf_id=99)
        rows = []

        self._run_sync(
            rows,
            {
                "BLUE": {
                    "state": {"route_distinguisher": "65000:100"},
                    "interfaces": {"interface": {"Loopback0": {}}},
                }
            },
            device=device,
            iface_rows=[iface],
        )

        self.assertIs(iface.vrf, other_vrf)
        self.assertEqual(iface.save_calls, 0)

    def test_skips_interface_assignment_when_device_is_none(self):
        iface = FakeInterface(pk=10, name="Loopback0", device=None, vrf=None, vrf_id=None)
        rows = []

        self._run_sync(
            rows,
            {
                "BLUE": {
                    "state": {},
                    "interfaces": {"interface": {"Loopback0": {}}},
                }
            },
            device=None,
            iface_rows=[iface],
        )

        self.assertIsNone(iface.vrf)
        self.assertEqual(iface.save_calls, 0)


class JournalMessageTests(unittest.TestCase):
    def setUp(self):
        self.netbox_sync = load_module()

    def test_interface_message_ignores_prune_only_runs(self):
        message = self.netbox_sync._build_interface_journal_message(
            {
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "deleted_names": [],
                "delete_failed": 0,
                "prune_skipped": True,
            }
        )

        self.assertEqual(message, "")

    def test_interface_message_includes_real_changes(self):
        message = self.netbox_sync._build_interface_journal_message(
            {
                "created": 1,
                "updated": 2,
                "deleted": 1,
                "deleted_names": ["Gi1/0/24"],
                "delete_failed": 1,
            }
        )

        self.assertIn("created=1", message)
        self.assertIn("updated=2", message)
        self.assertIn("deleted=1 (Gi1/0/24)", message)
        self.assertIn("delete_failed=1", message)

    def test_ip_message_ignores_conflict_only_runs(self):
        message = self.netbox_sync._build_ip_journal_message(
            {
                "created": 0,
                "reassigned": 0,
                "conflicts": 3,
                "mgmt_created": 0,
            }
        )

        self.assertEqual(message, "")

    def test_ip_message_includes_conflicts_when_other_changes_exist(self):
        message = self.netbox_sync._build_ip_journal_message(
            {
                "created": 2,
                "reassigned": 1,
                "conflicts": 3,
                "mgmt_created": 0,
            }
        )

        self.assertIn("created=2", message)
        self.assertIn("reassigned=1", message)
        self.assertIn("conflicts=3", message)


class FakeInterface:
    objects = None

    def __init__(self, pk, device=None, name="", enabled=True, type="1000base-t",
                 vrf=None, vrf_id=None):
        self.pk = pk
        self.device = device
        self.name = name
        self.enabled = enabled
        self.type = type
        self.vrf = vrf
        self.vrf_id = vrf_id
        self.saved = False
        self.save_calls = 0

    def save(self):
        self.saved = True
        self.save_calls += 1
        if self.vrf is not None:
            self.vrf_id = self.vrf.pk


class FakeInterfaceManager:
    def __init__(self, rows):
        self.rows = rows
        self.next_pk = max((row.pk for row in rows), default=0) + 1

    def filter(self, **criteria):
        return FakeQuerySet([row for row in self.rows if _matches(row, criteria)])

    def values_list(self, *fields, flat=False):
        result = []
        for row in self.rows:
            if flat and len(fields) == 1:
                result.append(getattr(row, fields[0], None))
            else:
                result.append(tuple(getattr(row, f, None) for f in fields))
        return result

    def get_or_create(self, **kwargs):
        defaults = kwargs.pop("defaults", {})
        existing = self.filter(**kwargs).first()
        if existing:
            return existing, False
        row = FakeInterface(pk=self.next_pk, **kwargs, **defaults)
        self.next_pk += 1
        self.rows.append(row)
        return row, True


class FakeIPAddress:
    objects = None

    def __init__(
        self,
        pk,
        address="",
        assigned_object=None,
        status="active",
        assigned_object_type=None,
        assigned_object_id=None,
        vrf=None,
        vrf_id=None,
    ):
        self.pk = pk
        self.address = address
        self.assigned_object = assigned_object
        self.status = status
        self.assigned_object_type = assigned_object_type
        self.assigned_object_type_id = getattr(assigned_object_type, "id", None)
        self.assigned_object_id = (
            assigned_object_id if assigned_object_id is not None else getattr(assigned_object, "pk", None)
        )
        self.vrf = vrf
        self.vrf_id = vrf_id
        self.saved = False
        self.save_calls = 0

    def save(self):
        self.saved = True
        self.save_calls += 1
        if self.vrf is not None:
            self.vrf_id = self.vrf.pk


class FakeIPAddressManager:
    def __init__(self, rows):
        self.rows = rows
        self.next_pk = max((row.pk for row in rows), default=0) + 1

    def filter(self, **criteria):
        return FakeQuerySet([row for row in self.rows if _matches(row, criteria)])

    def get_or_create(self, address, defaults=None):
        defaults = defaults or {}
        existing = self.filter(address=address).first()
        if existing:
            return existing, False

        assigned_object = None
        assigned_object_id = defaults.get("assigned_object_id")
        if assigned_object_id is not None:
            assigned_object = next(
                (row for row in FakeInterface.objects.rows if row.pk == assigned_object_id),
                None,
            )
        row = FakeIPAddress(
            pk=self.next_pk,
            address=address,
            assigned_object=assigned_object,
            status=defaults.get("status", "active"),
            assigned_object_type=defaults.get("assigned_object_type"),
            assigned_object_id=assigned_object_id,
        )
        self.next_pk += 1
        self.rows.append(row)
        return row, True


class SyncIpsTests(unittest.TestCase):
    def setUp(self):
        self.netbox_sync = load_module()

    def _run_sync_ips(self, interface_names, interfaces_ip, mgmt_ip):
        device = types.SimpleNamespace(pk=1, name="edge-01")
        iface_rows = [
            FakeInterface(pk=index, device=device, name=iface_name)
            for index, iface_name in enumerate(interface_names, start=1)
        ]
        ip_rows = []
        FakeInterface.objects = FakeInterfaceManager(iface_rows)
        FakeIPAddress.objects = FakeIPAddressManager(ip_rows)

        fake_dcim = types.ModuleType("dcim")
        fake_dcim_models = types.ModuleType("dcim.models")
        fake_dcim_models.Interface = FakeInterface

        fake_ipam = types.ModuleType("ipam")
        fake_ipam_models = types.ModuleType("ipam.models")
        fake_ipam_models.IPAddress = FakeIPAddress

        fake_django = types.ModuleType("django")
        fake_contrib = types.ModuleType("django.contrib")
        fake_contenttypes = types.ModuleType("django.contrib.contenttypes")
        fake_contenttypes_models = types.ModuleType("django.contrib.contenttypes.models")

        class FakeContentType:
            id = 42

        class FakeContentTypeManager:
            @staticmethod
            def get_for_model(_model):
                return FakeContentType()

        fake_contenttypes_models.ContentType = types.SimpleNamespace(objects=FakeContentTypeManager())

        with mock.patch.dict(
            sys.modules,
            {
                "dcim": fake_dcim,
                "dcim.models": fake_dcim_models,
                "ipam": fake_ipam,
                "ipam.models": fake_ipam_models,
                "django": fake_django,
                "django.contrib": fake_contrib,
                "django.contrib.contenttypes": fake_contenttypes,
                "django.contrib.contenttypes.models": fake_contenttypes_models,
            },
        ):
            return self.netbox_sync._sync_ips(
                device,
                interfaces_ip,
                mgmt_ip=mgmt_ip,
                log_fn=lambda _msg: None,
            )

    def test_prefers_active_management_interface_ip_over_seed_ip(self):
        primary_ip, stats = self._run_sync_ips(
            ["Management1", "GigabitEthernet1/0/1"],
            {
                "GigabitEthernet1/0/1": {"ipv4": {"10.0.0.10": {"prefix_length": 24}}},
                "Management1": {"ipv4": {"192.0.2.10": {"prefix_length": 24}}},
            },
            mgmt_ip="10.0.0.10",
        )

        self.assertIsNotNone(primary_ip)
        self.assertEqual(primary_ip.address, "192.0.2.10/24")
        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["mgmt_created"], 0)

    def test_treats_catalyst_gigabitethernet0_0_as_management_interface(self):
        primary_ip, _stats = self._run_sync_ips(
            ["GigabitEthernet0/0", "GigabitEthernet1/0/1"],
            {
                "GigabitEthernet0/0": {"ipv4": {"192.0.2.10": {"prefix_length": 24}}},
                "GigabitEthernet1/0/1": {"ipv4": {"10.0.0.10": {"prefix_length": 24}}},
            },
            mgmt_ip="10.0.0.10",
        )

        self.assertIsNotNone(primary_ip)
        self.assertEqual(primary_ip.address, "192.0.2.10/24")

    def test_treats_nexus_mgmt0_as_management_interface(self):
        primary_ip, _stats = self._run_sync_ips(
            ["mgmt0", "Ethernet1/1"],
            {
                "mgmt0": {"ipv4": {"198.51.100.10": {"prefix_length": 24}}},
                "Ethernet1/1": {"ipv4": {"10.0.0.10": {"prefix_length": 24}}},
            },
            mgmt_ip="10.0.0.10",
        )

        self.assertIsNotNone(primary_ip)
        self.assertEqual(primary_ip.address, "198.51.100.10/24")


class PrimaryPreservationTests(unittest.TestCase):
    def setUp(self):
        self.netbox_sync = load_module()

    def test_management_candidate_overrides_non_management_primary(self):
        device = types.SimpleNamespace(pk=1, name="edge-01")
        existing_iface = FakeInterface(pk=1, device=device, name="Loopback255")
        candidate_iface = FakeInterface(pk=2, device=device, name="GigabitEthernet0/0")
        existing_primary = FakeIPAddress(pk=1, address="192.168.1.1/32", assigned_object=existing_iface)
        candidate_primary = FakeIPAddress(pk=2, address="10.10.10.10/24", assigned_object=candidate_iface)

        self.assertFalse(
            self.netbox_sync._should_preserve_existing_primary(existing_primary, candidate_primary)
        )

    def test_preserves_existing_primary_when_candidate_is_not_management(self):
        device = types.SimpleNamespace(pk=1, name="edge-01")
        existing_iface = FakeInterface(pk=1, device=device, name="Loopback255")
        candidate_iface = FakeInterface(pk=2, device=device, name="Vlan10")
        existing_primary = FakeIPAddress(pk=1, address="192.168.1.1/32", assigned_object=existing_iface)
        candidate_primary = FakeIPAddress(pk=2, address="10.10.10.10/24", assigned_object=candidate_iface)

        self.assertTrue(
            self.netbox_sync._should_preserve_existing_primary(existing_primary, candidate_primary)
        )
