"""
NetBox synchronization logic.

All operations use get_or_create / update — nothing is ever deleted.
Matches existing devices by hostname first, then by primary IP.
"""

import logging
import logging.handlers
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("netbox.plugins.netbox_discovery")

# ---------------------------------------------------------------------------
# Dedicated conflict logger — writes to /var/log/netbox/discovery_conflicts.log
# so IP assignment issues can be reviewed and corrected independently of the
# main NetBox log.
# ---------------------------------------------------------------------------

def _get_conflict_logger() -> logging.Logger:
    """Return (creating on first call) the dedicated IP-conflict file logger."""
    name = "netbox.plugins.netbox_discovery.conflicts"
    clog = logging.getLogger(name)
    if clog.handlers:
        return clog  # already configured

    clog.setLevel(logging.WARNING)
    clog.propagate = False  # don't bubble up into the main NetBox log

    log_path = "/var/log/netbox/discovery_conflicts.log"
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        clog.addHandler(handler)
    except OSError as exc:
        # If the log directory isn't writable, fall back to the main logger
        logger.warning("Cannot open conflict log %s: %s — conflicts will appear in main log only", log_path, exc)
        clog.addHandler(logging.NullHandler())

    return clog

# ---------------------------------------------------------------------------
# Interface type mapping
# ---------------------------------------------------------------------------

# Maps name-prefix/pattern → NetBox interface type slug
INTERFACE_TYPE_MAP = [
    (r"^(vlan|svi|bvi)\d*", "virtual"),
    (r"^loopback", "virtual"),
    (r"^tunnel", "virtual"),
    (r"^null", "virtual"),
    (r"^(port-channel|bundle|ae|po)\d*", "lag"),
    (r"^(hundredgig|hundredge|hu)\d", "100gbase-x-qsfp28"),
    (r"^(fortygig|fortye|fo)\d", "40gbase-x-qsfpplus"),
    (r"^(twentyfivegig|twentyfivege|twe)\d", "25gbase-x-sfp28"),
    (r"^(tengig|tengige|te|10gige)\d", "10gbase-x-sfpp"),
    (r"^(gigabit|gigabitethernet|gi|ge)\d", "1000base-t"),
    (r"^(fasteth|fastethernet|fa|fe)\d", "100base-tx"),
    (r"^(serial|se)\d", "other"),
    (r"^mgmt", "1000base-t"),
    (r"^management", "1000base-t"),
]


def map_interface_type(name: str) -> str:
    """Map a NAPALM interface name to a NetBox interface type slug."""
    n = name.lower()
    for pattern, iface_type in INTERFACE_TYPE_MAP:
        if re.match(pattern, n):
            return iface_type
    return "other"


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def make_slug(text: str, max_length: int = 100) -> str:
    """Create a URL-safe slug from arbitrary text."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length]


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

def sync_device(
    mgmt_ip: str,
    data: Dict[str, Any],
    holding_site_name: str = "Holding",
    log_fn: Optional[Callable[[str], None]] = None,
) -> Tuple[str, bool]:
    """
    Synchronize discovered device data into NetBox.

    Args:
        mgmt_ip: The management IP used to connect to the device.
        data: Dict from collector.collect_device_data().
        holding_site_name: Name of the holding site (created if needed).
        log_fn: Log callback.

    Returns:
        Tuple of (device_name, was_created).
    """
    # Import NetBox models here to avoid import errors outside NetBox context
    from dcim.models import (
        Device,
        DeviceRole,
        DeviceType,
        Interface,
        Manufacturer,
        Site,
    )
    from ipam.models import IPAddress, VLAN
    from django.db import transaction
    from django.utils.text import slugify

    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)

    facts = data.get("facts", {})
    interfaces_raw = data.get("interfaces", {})
    interfaces_ip = data.get("interfaces_ip", {})
    vlans_raw = data.get("vlans", {})
    stack_members = data.get("stack_members", [])

    raw_hostname = (facts.get("hostname") or "").strip()
    if _is_valid_hostname(raw_hostname):
        hostname = raw_hostname
    else:
        if raw_hostname:
            log_fn(f"  [WARN] Ignoring invalid hostname '{raw_hostname[:60]}' — using IP as identifier")
        hostname = mgmt_ip
    vendor = (facts.get("vendor") or "Unknown").strip()
    model = (facts.get("model") or "Unknown").strip()
    serial = (facts.get("serial_number") or "").strip()
    os_version = (facts.get("os_version") or "").strip()

    log_fn(f"  Syncing: {hostname} ({vendor} {model}, serial={serial or 'N/A'})")

    with transaction.atomic():
        # --- Site (Holding) ---
        site = _ensure_site(holding_site_name)

        # --- Manufacturer ---
        # Use filter().first() to tolerate duplicate rows (raises MultipleObjectsReturned
        # with get_or_create when the DB already has two matching entries).
        mfr = Manufacturer.objects.filter(name__iexact=vendor).first()
        if mfr is None:
            mfr, _ = Manufacturer.objects.get_or_create(
                name=vendor,
                defaults={"slug": make_slug(vendor)},
            )
        if not mfr.slug:
            mfr.slug = make_slug(vendor)
            mfr.save()

        # --- DeviceType ---
        dtype = DeviceType.objects.filter(manufacturer=mfr, model__iexact=model).first()
        if dtype is None:
            dtype, _ = DeviceType.objects.get_or_create(
                manufacturer=mfr,
                model=model,
                defaults={"slug": make_slug(f"{vendor}-{model}")},
            )
        if not dtype.slug:
            dtype.slug = make_slug(f"{vendor}-{model}")
            dtype.save()

        # --- DeviceRole ---
        role = _ensure_role("Network Device")

        # --- Device: match by hostname, then by primary IP ---
        device, was_created = _get_or_create_device(
            hostname=hostname,
            mgmt_ip=mgmt_ip,
            site=site,
            device_type=dtype,
            role=role,
            serial=serial,
        )

        # Update mutable fields regardless of creation
        changed = False
        if device.device_type != dtype:
            device.device_type = dtype
            changed = True
        if serial and device.serial != serial:
            device.serial = serial
            changed = True
        if os_version:
            try:
                cf = device.custom_field_data or {}
                if cf.get("os_version") != os_version:
                    cf["os_version"] = os_version
                    device.custom_field_data = cf
                    changed = True
            except Exception as exc:
                log_fn(f"  [WARN] Could not set os_version custom field: {exc}")
        # --- Hostname-to-site auto-assignment (only if still on holding site) ---
        if device.site_id == site.pk:
            matched_site = _match_site_by_hostname(hostname, exclude_site_name=holding_site_name)
            if matched_site:
                device.site = matched_site
                changed = True
                log_fn(f"  Auto-assigned site '{matched_site.name}' from hostname prefix.")

        if changed:
            device.save()

        # --- Interfaces ---
        _sync_interfaces(device, interfaces_raw, log_fn)

        # --- IP Addresses ---
        primary_ip = _sync_ips(device, interfaces_ip, mgmt_ip, log_fn)

        # --- Set primary IP ---
        if primary_ip and device.primary_ip4 != primary_ip:
            # Guard against the unique constraint on primary_ip4_id:
            # if another device already owns this IP as primary, skip rather than crash.
            conflict = Device.objects.filter(primary_ip4=primary_ip).exclude(pk=device.pk).first()
            if conflict:
                # Gather extra context: what interface is the IP currently assigned to?
                assigned_iface = primary_ip.assigned_object
                assigned_desc = (
                    f"{assigned_iface.device.name}/{assigned_iface.name}"
                    if assigned_iface and hasattr(assigned_iface, "device")
                    else str(assigned_iface) if assigned_iface else "unassigned"
                )

                # Auto-resolve: if the blocking device is a domain-variant of the
                # current device (same base hostname, different suffix), the conflict
                # is a stale duplicate.  Strip the primary-IP claim from the stale
                # device so this device can take it.
                if _base_hostname(conflict.name) == _base_hostname(hostname):
                    try:
                        conflict.primary_ip4 = None
                        conflict.save()
                        device.primary_ip4 = primary_ip
                        device.save()
                        log_fn(
                            f"  Auto-resolved primary IP conflict: cleared primary from "
                            f"domain-variant '{conflict.name}' (id={conflict.pk}), "
                            f"assigned {primary_ip} to '{hostname}'."
                        )
                    except Exception as exc:
                        log_fn(f"  [WARN] Could not auto-resolve IP conflict: {exc}")
                else:
                    warn_msg = (
                        f"  [WARN] Primary IP conflict — cannot assign {primary_ip} to '{hostname}' "
                        f"(mgmt={mgmt_ip}): already primary on '{conflict.name}' "
                        f"(id={conflict.pk}, site={conflict.site}). "
                        f"IP currently assigned to interface: {assigned_desc}."
                    )
                    log_fn(warn_msg)
                    _get_conflict_logger().warning(
                        "PRIMARY IP CONFLICT | wanted_device=%s (id=%s, mgmt=%s) | "
                        "blocking_device=%s (id=%s, site=%s) | "
                        "ip=%s (id=%s) | ip_assigned_to=%s",
                        hostname, device.pk, mgmt_ip,
                        conflict.name, conflict.pk, conflict.site,
                        primary_ip, primary_ip.pk, assigned_desc,
                    )
            else:
                device.primary_ip4 = primary_ip
                device.save()

        # --- VLANs ---
        _sync_vlans(vlans_raw, site, log_fn)

        # --- Virtual Chassis (Cisco StackWise) ---
        if len(stack_members) > 1:
            _sync_virtual_chassis(
                master_device=device,
                hostname=hostname,
                stack_members=stack_members,
                site=site,
                master_dtype=dtype,
                role=role,
                log_fn=log_fn,
            )

    action = "created" if was_created else "updated"
    log_fn(f"  Device '{hostname}' {action}.")
    return hostname, was_created


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _ensure_site(name: str):
    from dcim.models import Site

    site = Site.objects.filter(name__iexact=name).first()
    if site is None:
        site, created = Site.objects.get_or_create(
            name=name,
            defaults={"slug": make_slug(name), "status": "active"},
        )
        if created:
            logger.info("Created holding site: %s", name)
    return site


def _ensure_role(name: str):
    from dcim.models import DeviceRole

    role = DeviceRole.objects.filter(name__iexact=name).first()
    if role is None:
        role, created = DeviceRole.objects.get_or_create(
            name=name,
            defaults={"slug": make_slug(name), "color": "2196f3"},
        )
        if created:
            logger.info("Created device role: %s", name)
    return role


def _base_hostname(name: str) -> str:
    """Return the label before the first '.' (the short hostname), lowercased."""
    return name.split(".")[0].lower() if name else ""


# Hostnames that are known-bad: CLI error artifacts, OS identifiers, generic labels.
_INVALID_HOSTNAME_EXACT = {
    "kernel", "localhost", "router", "switch", "firewall",
    "linux", "ubuntu", "debian", "centos", "redhat",
}
# Substrings/prefixes that indicate a Cisco CLI error was captured as a hostname.
_INVALID_HOSTNAME_FRAGMENTS = [
    "% invalid",
    "% incomplete",
    "% ambiguous",
    "invalid input",
    "incomplete command",
]


def _is_valid_hostname(hostname: str) -> bool:
    """
    Return False if *hostname* looks like a CLI error artifact or a non-network
    device identifier rather than a real network device hostname.
    """
    if not hostname:
        return False
    h = hostname.strip()
    # Reject the raw management IP as a hostname (fallback — we handle it upstream)
    if not h or h.startswith("^"):
        return False
    h_lower = h.lower()
    if h_lower in _INVALID_HOSTNAME_EXACT:
        return False
    for frag in _INVALID_HOSTNAME_FRAGMENTS:
        if frag in h_lower:
            return False
    return True


def _match_site_by_hostname(hostname: str, exclude_site_name: str = "Holding"):
    """
    Find the NetBox site whose name best matches as a prefix of the hostname.

    Strips domain suffix and normalises separators before comparing so that
    e.g. hostname 'GBLON10SWI01' matches site 'GBLON10'.  The longest matching
    prefix wins (most specific site).  Requires at least 4 characters to avoid
    spurious single-letter matches.

    Returns the matching Site object, or None.
    """
    from dcim.models import Site

    short = hostname.split(".")[0].upper()
    if len(short) < 4:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[-_\s]", "", s).upper()

    norm_short = _norm(short)
    best_site = None
    best_len = 0

    for site in Site.objects.exclude(name__iexact=exclude_site_name):
        norm_name = _norm(site.name)
        if len(norm_name) < 4:
            continue
        if norm_short.startswith(norm_name) and len(norm_name) > best_len:
            best_len = len(norm_name)
            best_site = site

    return best_site


def _get_or_create_device(
    hostname: str,
    mgmt_ip: str,
    site,
    device_type,
    role,
    serial: str,
) -> Tuple[Any, bool]:
    from dcim.models import Device
    from django.db.models import Q

    # 1. Try match by exact hostname
    device = Device.objects.filter(name=hostname).first()
    if device:
        return device, False

    # 2. Try match by primary IP
    from ipam.models import IPAddress

    mgmt_cidr = f"{mgmt_ip}/32"
    ip_obj = IPAddress.objects.filter(address=mgmt_cidr).first()
    if ip_obj and ip_obj.assigned_object:
        iface = ip_obj.assigned_object
        if hasattr(iface, "device") and iface.device:
            return iface.device, False

    # 3. Try match by base hostname (same short name, different domain suffix).
    #    e.g. "router1.emea.bcd.local" matches existing "router1.us.bcd.local"
    #    or bare "router1" to avoid creating duplicates under different domains.
    base = _base_hostname(hostname)
    if base:
        device = (
            Device.objects.filter(
                Q(name__iexact=base) | Q(name__istartswith=base + ".")
            )
            .exclude(name=hostname)
            .first()
        )
        if device:
            logger.info(
                "Domain-variant match: incoming '%s' matched existing device '%s' (id=%s)",
                hostname, device.name, device.pk,
            )
            return device, False

    # 4. Create new device
    device = Device.objects.create(
        name=hostname,
        site=site,
        device_type=device_type,
        role=role,
        serial=serial,
        status="active",
    )
    logger.info("Created new device: %s", hostname)
    return device, True


def _sync_interfaces(device, interfaces_raw: Dict, log_fn: Callable):
    from dcim.models import Interface

    for iface_name, iface_data in interfaces_raw.items():
        iface_type = map_interface_type(iface_name)
        iface, created = Interface.objects.get_or_create(
            device=device,
            name=iface_name,
            defaults={"type": iface_type},
        )

        changed = False
        enabled = iface_data.get("is_enabled", True)
        description = (iface_data.get("description") or "")[:200]
        mtu = iface_data.get("mtu") or None
        mac = (iface_data.get("mac_address") or "").upper() or None

        if iface.enabled != enabled:
            iface.enabled = enabled
            changed = True
        if iface.description != description:
            iface.description = description
            changed = True
        if mtu and iface.mtu != mtu:
            iface.mtu = mtu
            changed = True
        if mac and iface.mac_address != mac:
            try:
                iface.mac_address = mac
                changed = True
            except Exception:
                pass

        if changed:
            iface.save()


def _sync_ips(device, interfaces_ip: Dict, mgmt_ip: str, log_fn: Callable):
    from dcim.models import Interface
    from ipam.models import IPAddress
    from django.contrib.contenttypes.models import ContentType

    iface_ct = ContentType.objects.get_for_model(Interface)
    primary_ip = None

    for iface_name, addr_families in interfaces_ip.items():
        # Resolve the interface object
        iface = Interface.objects.filter(device=device, name=iface_name).first()
        if not iface:
            # Create a minimal interface if missing
            iface, _ = Interface.objects.get_or_create(
                device=device,
                name=iface_name,
                defaults={"type": map_interface_type(iface_name)},
            )

        # addr_families is {'ipv4': {'10.0.0.1': {'prefix_length': 24}}, 'ipv6': {...}}
        for family, addrs in addr_families.items():
            for ip_str, ip_info in addrs.items():
                prefix_len = ip_info.get("prefix_length", 32)
                cidr = f"{ip_str}/{prefix_len}"

                ip_obj, created = IPAddress.objects.get_or_create(
                    address=cidr,
                    defaults={
                        "assigned_object_type": iface_ct,
                        "assigned_object_id": iface.pk,
                        "status": "active",
                    },
                )

                # Re-assign if orphaned
                if ip_obj.assigned_object_id != iface.pk:
                    ip_obj.assigned_object_type = iface_ct
                    ip_obj.assigned_object_id = iface.pk
                    ip_obj.save()

                # Track which IP is the management IP for setting as primary
                if ip_str == mgmt_ip:
                    primary_ip = ip_obj

    # If no exact match for mgmt_ip, create a /32 for it
    if primary_ip is None and mgmt_ip:
        mgmt_cidr = f"{mgmt_ip}/32"
        # Try to find any existing /32 for this IP
        primary_ip = IPAddress.objects.filter(address=mgmt_cidr).first()
        if not primary_ip:
            # Attach to mgmt interface if it exists, else leave unassigned
            mgmt_iface = (
                Interface.objects.filter(device=device, name__icontains="mgmt").first()
                or Interface.objects.filter(device=device, name__icontains="management").first()
            )
            kwargs = {"status": "active"}
            if mgmt_iface:
                iface_ct = ContentType.objects.get_for_model(Interface)
                kwargs["assigned_object_type"] = iface_ct
                kwargs["assigned_object_id"] = mgmt_iface.pk
            primary_ip, _ = IPAddress.objects.get_or_create(address=mgmt_cidr, defaults=kwargs)

    return primary_ip


def _sync_virtual_chassis(
    master_device,
    hostname: str,
    stack_members: List,
    site,
    master_dtype,
    role,
    log_fn: Callable,
):
    """
    Create / update a VirtualChassis for a Cisco StackWise stack.

    - One VirtualChassis named after the master device's hostname
    - The master device is assigned vc_position matching its 'active' role
    - Each additional stack member gets its own Device record (named
      '{base_hostname}-sw{position}') with its individual serial and model,
      and is assigned to the same VirtualChassis at its position
    """
    from dcim.models import Device, DeviceType, VirtualChassis

    # Identify the active (master) position
    active_entry = next(
        (m for m in stack_members if m.get("role") in ("active", "standby")),
        stack_members[0],
    )
    master_pos = active_entry["position"]

    # Base name without domain (used for member device names)
    base = hostname.split(".")[0]

    # Create or get the VirtualChassis record
    vc, vc_created = VirtualChassis.objects.get_or_create(
        name=hostname,
        defaults={"domain": hostname},
    )
    if vc_created:
        log_fn(f"  [Stack] Created VirtualChassis '{hostname}'")

    # Assign master device to VC
    master_changed = False
    if master_device.virtual_chassis_id != vc.pk:
        master_device.virtual_chassis = vc
        master_changed = True
    if master_device.vc_position != master_pos:
        master_device.vc_position = master_pos
        master_changed = True
    master_priority = active_entry.get("priority", 15)
    if master_device.vc_priority != master_priority:
        master_device.vc_priority = master_priority
        master_changed = True
    if master_changed:
        master_device.save()

    # Point VC master FK at this device
    if vc.master_id != master_device.pk:
        vc.master = master_device
        vc.save()

    log_fn(
        f"  [Stack] VirtualChassis '{hostname}': {len(stack_members)} member(s), "
        f"master at position {master_pos}"
    )

    # Sync non-master members
    for entry in stack_members:
        pos = entry["position"]
        if pos == master_pos:
            continue  # master already handled above

        member_serial = (entry.get("serial") or "").strip()
        member_model = (entry.get("model") or "").strip()
        member_name = f"{base}-sw{pos}"

        # Resolve DeviceType for this member (may differ from master if mixed-model stack)
        member_dtype = master_dtype
        if member_model and member_model.lower() != master_dtype.model.lower():
            found_dt = DeviceType.objects.filter(
                manufacturer=master_dtype.manufacturer,
                model__iexact=member_model,
            ).first()
            if found_dt:
                member_dtype = found_dt
            else:
                try:
                    member_dtype, _ = DeviceType.objects.get_or_create(
                        manufacturer=master_dtype.manufacturer,
                        model=member_model,
                        defaults={"slug": make_slug(f"{master_dtype.manufacturer.name}-{member_model}")},
                    )
                except Exception:
                    member_dtype = master_dtype  # fallback

        # Find existing member device: prefer by VC position, then by name
        mem_dev = (
            Device.objects.filter(virtual_chassis=vc, vc_position=pos).first()
            or Device.objects.filter(name=member_name).first()
        )

        if mem_dev is None:
            mem_dev = Device.objects.create(
                name=member_name,
                site=master_device.site,
                device_type=member_dtype,
                role=role,
                serial=member_serial,
                status="active",
                virtual_chassis=vc,
                vc_position=pos,
                vc_priority=entry.get("priority", 1),
            )
            log_fn(
                f"  [Stack] Created member '{member_name}' "
                f"pos={pos} serial={member_serial or 'N/A'} model={member_model or 'N/A'}"
            )
        else:
            changed = False
            if mem_dev.virtual_chassis_id != vc.pk:
                mem_dev.virtual_chassis = vc
                changed = True
            if mem_dev.vc_position != pos:
                mem_dev.vc_position = pos
                changed = True
            if member_serial and mem_dev.serial != member_serial:
                mem_dev.serial = member_serial
                changed = True
            if mem_dev.device_type_id != member_dtype.pk:
                mem_dev.device_type = member_dtype
                changed = True
            if mem_dev.site_id != master_device.site_id:
                mem_dev.site = master_device.site
                changed = True
            if changed:
                mem_dev.save()
                log_fn(
                    f"  [Stack] Updated member '{mem_dev.name}' "
                    f"pos={pos} serial={member_serial or 'N/A'}"
                )


def _sync_vlans(vlans_raw: Dict, site, log_fn: Callable):
    from ipam.models import VLAN

    for vid_str, vlan_data in vlans_raw.items():
        try:
            vid = int(vid_str)
        except (ValueError, TypeError):
            continue

        if not (1 <= vid <= 4094):
            continue

        vlan_name = vlan_data.get("name") or f"VLAN{vid}"
        vlan, created = VLAN.objects.get_or_create(
            vid=vid,
            site=site,
            defaults={"name": vlan_name, "status": "active"},
        )
        if not created and vlan.name != vlan_name and vlan.name == f"VLAN{vid}":
            # Update auto-generated names with the real name
            vlan.name = vlan_name
            vlan.save()


# ---------------------------------------------------------------------------
# Cable / connection sync (post-crawl)
# ---------------------------------------------------------------------------

# Map common CDP/LLDP abbreviated interface name prefixes to full names.
# Sorted longest-first so "twe" is tried before "te".
_IFACE_EXPANSIONS = [
    ("twe", "twentyfivegigabitethernet"),
    ("hundredgige", "hundredgigabitethernet"),
    ("hundredge", "hundredgigabitethernet"),
    ("fortygige", "fortygigabitethernet"),
    ("tengige", "tengigabitethernet"),
    ("gigabitethernet", "gigabitethernet"),   # already full — keep for identity
    ("fastethernet", "fastethernet"),
    ("hu", "hundredgigabitethernet"),
    ("fo", "fortygigabitethernet"),
    ("te", "tengigabitethernet"),
    ("gi", "gigabitethernet"),
    ("ge", "gigabitethernet"),
    ("fa", "fastethernet"),
    ("et", "ethernet"),
    ("po", "port-channel"),
    ("ae", "port-channel"),
    ("mg", "management"),
    ("ma", "management"),
    ("lo", "loopback"),
]


def _find_device_by_hostname(hostname: str):
    """Look up a NetBox Device by hostname, with domain-variant fallback."""
    from dcim.models import Device
    from django.db.models import Q

    if not hostname:
        return None
    device = Device.objects.filter(name=hostname).first()
    if device:
        return device
    base = _base_hostname(hostname)
    if base:
        device = Device.objects.filter(
            Q(name__iexact=base) | Q(name__istartswith=base + ".")
        ).first()
    return device


def _find_interface(device, name: str):
    """
    Look up a NetBox Interface on *device* by name.
    Tries exact match first, then expands common CDP/LLDP abbreviations.
    """
    from dcim.models import Interface

    if not name:
        return None

    iface = Interface.objects.filter(device=device, name__iexact=name).first()
    if iface:
        return iface

    lower = name.lower()
    for abbrev, full in _IFACE_EXPANSIONS:
        if lower.startswith(abbrev) and not lower.startswith(full):
            expanded = full + lower[len(abbrev):]
            iface = Interface.objects.filter(device=device, name__iexact=expanded).first()
            if iface:
                return iface
    return None


def sync_cables(neighbor_records: List[Dict], log_fn: Optional[Callable] = None) -> int:
    """
    Create NetBox Cable objects from collected CDP/LLDP neighbor data.

    Should be called once after all devices have been synced (post-crawl),
    so both endpoints are guaranteed to exist in NetBox.

    Returns the count of new cables created.
    """
    from dcim.models import Cable
    from django.db import IntegrityError, transaction

    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)

    created = 0
    # frozenset of (iface_pk_a, iface_pk_b) — deduplicates A→B / B→A within this run
    seen: set = set()

    for record in neighbor_records:
        local_hostname = record.get("hostname", "")
        local_device = _find_device_by_hostname(local_hostname)
        if not local_device:
            continue

        for nbr in record.get("neighbors", []):
            local_iface_name = nbr.get("local_interface", "")
            remote_hostname = nbr.get("remote_hostname", "")
            remote_iface_name = nbr.get("remote_interface", "")

            if not local_iface_name or not remote_hostname or not remote_iface_name:
                continue

            local_iface = _find_interface(local_device, local_iface_name)
            if not local_iface:
                continue

            remote_device = _find_device_by_hostname(remote_hostname)
            if not remote_device:
                continue

            remote_iface = _find_interface(remote_device, remote_iface_name)
            if not remote_iface:
                continue

            # Skip if either interface is already cabled
            if local_iface.cable_id or remote_iface.cable_id:
                continue

            # Deduplicate bidirectional reports (A→B seen from both A and B)
            key = frozenset({local_iface.pk, remote_iface.pk})
            if key in seen:
                continue
            seen.add(key)

            try:
                with transaction.atomic():
                    cable = Cable(
                        a_terminations=[local_iface],
                        b_terminations=[remote_iface],
                        status="connected",
                    )
                    cable.full_clean()
                    cable.save()
                    created += 1
                    log_fn(
                        f"  [Cable] {local_device.name}/{local_iface.name}"
                        f" ↔ {remote_device.name}/{remote_iface.name}"
                    )
            except (IntegrityError, Exception) as exc:
                log_fn(
                    f"  [Cable] SKIP {local_device.name}/{local_iface.name}"
                    f" ↔ {remote_device.name}/{remote_iface.name}: {exc}"
                )

    return created
