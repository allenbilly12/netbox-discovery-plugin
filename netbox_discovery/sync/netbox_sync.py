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

    hostname = (facts.get("hostname") or mgmt_ip).strip()
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
            cf = device.custom_field_data or {}
            if cf.get("os_version") != os_version:
                cf["os_version"] = os_version
                device.custom_field_data = cf
                changed = True
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


def _get_or_create_device(
    hostname: str,
    mgmt_ip: str,
    site,
    device_type,
    role,
    serial: str,
) -> Tuple[Any, bool]:
    from dcim.models import Device

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

    # 3. Create new device
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
