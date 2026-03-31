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

from netbox_discovery.sync.classify import classify_device

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
    lag_members = data.get("lag_members", {})
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

        # --- DeviceRole (auto-classify by model / driver) ---
        driver = (data.get("driver") or "").strip()
        classification = classify_device(model=model, vendor=vendor, driver=driver)
        role = _ensure_role(classification["role"], color=classification["color"])
        log_fn(f"  Auto-classified role: {classification['role']} (model={model})")

        # --- Device: match by hostname, then by primary IP ---
        device, was_created = _get_or_create_device(
            hostname=hostname,
            mgmt_ip=mgmt_ip,
            site=site,
            device_type=dtype,
            role=role,
            serial=serial,
        )
        if was_created:
            _add_journal_entry(
                device,
                (
                    "Discovery created this device from collected facts: "
                    f"hostname={hostname}, vendor={vendor}, model={model}, serial={serial or 'N/A'}, "
                    f"mgmt_ip={mgmt_ip}."
                ),
            )

        if (
            not was_created
            and serial
            and device.serial
            and device.serial != serial
            and device.device_type_id != dtype.pk
        ):
            refreshed = _perform_hardware_refresh(
                old_device=device,
                hostname=hostname,
                new_device_type=dtype,
                role=role,
                serial=serial,
                log_fn=log_fn,
            )
            if refreshed is not None:
                device = refreshed
                was_created = True

        # Update mutable fields regardless of creation
        changed = False
        device_field_changes: List[str] = []
        if device.device_type != dtype:
            old_model = (
                device.device_type.model
                if getattr(device, "device_type", None) is not None
                else "unknown"
            )
            device.device_type = dtype
            changed = True
            device_field_changes.append(f"model '{old_model}' -> '{dtype.model}'")
        if serial and device.serial != serial:
            old_serial = device.serial or "N/A"
            device.serial = serial
            changed = True
            device_field_changes.append(f"serial '{old_serial}' -> '{serial}'")
        if os_version:
            try:
                cf = device.custom_field_data or {}
                if cf.get("os_version") != os_version:
                    old_os = cf.get("os_version") or "N/A"
                    cf["os_version"] = os_version
                    device.custom_field_data = cf
                    changed = True
                    device_field_changes.append(f"os_version '{old_os}' -> '{os_version}'")
            except Exception as exc:
                log_fn(f"  [WARN] Could not set os_version custom field: {exc}")
        # --- Hostname-to-site auto-assignment (only if still on holding site) ---
        if device.site_id == site.pk:
            matched_site = _match_site_by_hostname(hostname, exclude_site_name=holding_site_name)
            if matched_site:
                old_site_name = getattr(device.site, "name", holding_site_name)
                device.site = matched_site
                changed = True
                log_fn(f"  Auto-assigned site '{matched_site.name}' from hostname prefix.")
                device_field_changes.append(f"site '{old_site_name}' -> '{matched_site.name}'")

        # Update role if classification improved (e.g. model was unknown before)
        if device.role_id != role.pk:
            old_role = getattr(getattr(device, "role", None), "name", "unknown")
            device.role = role
            changed = True
            device_field_changes.append(f"role '{old_role}' -> '{role.name}'")

        if changed:
            device.save()
        if device_field_changes:
            _add_journal_entry(
                device,
                "Discovery updated device attributes: " + "; ".join(device_field_changes) + ".",
            )

        # --- Auto-assign tags based on classification ---
        _sync_device_tags(device, classification["tags"], log_fn)

        # --- Interfaces ---
        interfaces_step_status = (data.get("step_status") or {}).get("interfaces")
        prune_stale = interfaces_step_status != "fail"
        interface_stats = _sync_interfaces(device, interfaces_raw, log_fn, prune_stale=prune_stale)
        if (
            interface_stats["created"]
            or interface_stats["updated"]
            or interface_stats["deleted"]
            or interface_stats["delete_failed"]
            or interface_stats["prune_skipped"]
        ):
            parts = []
            if interface_stats["created"]:
                parts.append(f"created={interface_stats['created']}")
            if interface_stats["updated"]:
                parts.append(f"updated={interface_stats['updated']}")
            if interface_stats["deleted"]:
                parts.append(
                    f"deleted={interface_stats['deleted']} ({', '.join(interface_stats['deleted_names'])})"
                )
            if interface_stats["delete_failed"]:
                parts.append(f"delete_failed={interface_stats['delete_failed']}")
            if interface_stats.get("stale_count"):
                parts.append(f"stale_detected={interface_stats['stale_count']}")
            if interface_stats["prune_skipped"]:
                parts.append("prune_skipped=yes (interface collection failed)")
            _add_journal_entry(
                device,
                "Discovery synchronized interfaces: " + "; ".join(parts) + ".",
            )
        _sync_lag_members(device, lag_members, log_fn)

        # --- IP Addresses ---
        primary_ip, ip_stats = _sync_ips(device, interfaces_ip, mgmt_ip, log_fn)
        if ip_stats["created"] or ip_stats["reassigned"] or ip_stats["conflicts"] or ip_stats["mgmt_created"]:
            _add_journal_entry(
                device,
                (
                    "Discovery synchronized IP assignments: "
                    f"created={ip_stats['created']}, reassigned={ip_stats['reassigned']}, "
                    f"conflicts={ip_stats['conflicts']}, mgmt_created={ip_stats['mgmt_created']}."
                ),
            )

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
                        _add_journal_entry(
                            conflict,
                            (
                                "Discovery cleared primary IPv4 during domain-variant conflict auto-resolution: "
                                f"{primary_ip} moved to '{hostname}'."
                            ),
                        )
                        _add_journal_entry(
                            device,
                            (
                                "Discovery auto-resolved primary IPv4 conflict by clearing "
                                f"domain-variant '{conflict.name}' and assigning {primary_ip}."
                            ),
                        )
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
                _add_journal_entry(
                    device,
                    f"Discovery set primary IPv4 to {primary_ip} (mgmt={mgmt_ip}).",
                )

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
                driver=driver,
                vendor=vendor,
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


def _ensure_role(name: str, color: str = "9e9e9e"):
    from dcim.models import DeviceRole

    role = DeviceRole.objects.filter(name__iexact=name).first()
    if role is None:
        role, created = DeviceRole.objects.get_or_create(
            name=name,
            defaults={"slug": make_slug(name), "color": color},
        )
        if created:
            logger.info("Created device role: %s", name)
    return role


def _sync_device_tags(device, tag_slugs: List[str], log_fn: Callable):
    """
    Ensure the device has all auto-classified tags attached.
    Creates tags that don't exist yet. Never removes existing tags.
    """
    from extras.models import Tag

    if not tag_slugs:
        return

    existing_slugs = set(device.tags.values_list("slug", flat=True))
    added = []

    for slug in tag_slugs:
        if slug in existing_slugs:
            continue
        tag = Tag.objects.filter(slug=slug).first()
        if tag is None:
            # Create with a human-readable name (capitalize, replace hyphens)
            display_name = slug.replace("-", " ").title()
            tag, created = Tag.objects.get_or_create(
                slug=slug,
                defaults={"name": display_name},
            )
        device.tags.add(tag)
        added.append(slug)

    if added:
        log_fn(f"  Auto-tagged: {', '.join(added)}")
        _add_journal_entry(
            device,
            f"Discovery auto-tagged device: {', '.join(added)}.",
        )


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


def _device_decommission_status() -> str:
    try:
        from dcim.choices import DeviceStatusChoices

        for attr in ("STATUS_DECOMMISSIONING", "DECOMMISSIONING"):
            if hasattr(DeviceStatusChoices, attr):
                return getattr(DeviceStatusChoices, attr)
    except Exception:
        pass
    return "decommissioning"


def _next_available_device_name(base_name: str) -> str:
    from dcim.models import Device

    if not Device.objects.filter(name=base_name).exists():
        return base_name

    idx = 1
    while True:
        candidate = f"{base_name}-old-{idx}"
        if not Device.objects.filter(name=candidate).exists():
            return candidate
        idx += 1


def _perform_hardware_refresh(old_device, hostname: str, new_device_type, role, serial: str, log_fn: Callable):
    from dcim.models import Device, Interface
    from ipam.models import IPAddress
    from django.contrib.contenttypes.models import ContentType

    old_model = old_device.device_type.model if old_device.device_type_id else "Unknown"
    new_model = new_device_type.model
    old_serial = old_device.serial or "N/A"

    # Clear IP ownership from interfaces on the old device so the replacement can
    # safely claim the same addresses during sync (notably management/primary IP).
    iface_ids = list(
        Interface.objects.filter(device=old_device).values_list("pk", flat=True)
    )
    released_ip_count = 0
    if iface_ids:
        iface_ct = ContentType.objects.get_for_model(Interface)
        ips_to_release = IPAddress.objects.filter(
            assigned_object_type=iface_ct,
            assigned_object_id__in=iface_ids,
        )
        released_ip_count = ips_to_release.count()
        ips_to_release.update(assigned_object_type=None, assigned_object_id=None)

    if old_device.primary_ip4_id or getattr(old_device, "primary_ip6_id", None):
        old_device.primary_ip4 = None
        if hasattr(old_device, "primary_ip6"):
            old_device.primary_ip6 = None

    archive_name = _next_available_device_name(old_device.name)
    old_device_name = old_device.name
    old_device.status = _device_decommission_status()
    old_device.name = archive_name
    old_device.save()

    replacement = Device.objects.create(
        name=hostname,
        site=old_device.site,
        device_type=new_device_type,
        role=role,
        serial=serial,
        status="active",
        platform=old_device.platform,
        tenant=old_device.tenant,
        location=old_device.location,
        comments=old_device.comments,
        custom_field_data=old_device.custom_field_data or {},
    )
    replacement.tags.set(old_device.tags.all())

    refresh_note = (
        "Discovery detected hardware refresh (serial and model changed): "
        f"{old_model}/{old_serial} -> {new_model}/{serial}. "
        f"Decommissioned prior device '{archive_name}' and created replacement '{hostname}'."
    )
    _add_journal_entry(old_device, refresh_note)
    _add_journal_entry(replacement, refresh_note)
    log_fn(
        f"  [Refresh] {old_model}/{old_serial} replaced by {new_model}/{serial}. "
        f"Archived '{old_device_name}' as '{archive_name}' (status={old_device.status}, "
        f"released_ips={released_ip_count})."
    )
    return replacement


def _sync_interfaces(device, interfaces_raw: Dict, log_fn: Callable, prune_stale: bool = True):
    from dcim.models import Interface

    created_count = 0
    updated_count = 0
    deleted_names: List[str] = []
    delete_failed = 0
    prune_skipped = False
    desired_ifaces = set(interfaces_raw.keys())

    for iface_name, iface_data in interfaces_raw.items():
        iface_type = map_interface_type(iface_name)
        iface, created = Interface.objects.get_or_create(
            device=device,
            name=iface_name,
            defaults={"type": iface_type},
        )
        if created:
            created_count += 1

        changed = False
        enabled = iface_data.get("is_enabled", True)
        description = (iface_data.get("description") or "")[:200]
        mtu = iface_data.get("mtu") or None
        mac = (iface_data.get("mac_address") or "").upper() or None

        if iface.type != iface_type:
            iface.type = iface_type
            changed = True
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
            if not created:
                updated_count += 1

    # Only delete stale interfaces if we received a non-empty interface payload.
    # This protects against accidental mass deletions from empty collector output.
    existing_count = Interface.objects.filter(device=device).count()
    stale_count = 0
    if desired_ifaces and not prune_stale:
        prune_skipped = True
        log_fn(
            f"  [WARN] Interface pruning skipped for {device.name}: "
            "get_interfaces() failed for this device in the collector."
        )
    elif desired_ifaces:
        stale_qs = Interface.objects.filter(device=device).exclude(name__in=desired_ifaces)
        stale_count = stale_qs.count()
        if stale_count:
            log_fn(
                f"  [Interface] Reconciling {device.name}: discovered={len(desired_ifaces)}, "
                f"existing={existing_count}, stale={stale_count}."
            )
        for stale_iface in stale_qs:
            stale_name = stale_iface.name
            try:
                _detach_interface_dependencies(stale_iface, log_fn)
                stale_iface.delete()
                deleted_names.append(stale_name)
                log_fn(f"  [Interface] Deleted stale interface {device.name}/{stale_name}")
            except Exception as exc:
                delete_failed += 1
                log_fn(
                    f"  [WARN] Could not delete stale interface {device.name}/{stale_name}: "
                    f"{type(exc).__name__}: {exc}"
                )

    return {
        "created": created_count,
        "updated": updated_count,
        "deleted": len(deleted_names),
        "deleted_names": deleted_names,
        "delete_failed": delete_failed,
        "prune_skipped": prune_skipped,
        "stale_count": stale_count,
    }


def _detach_interface_dependencies(iface, log_fn: Callable) -> None:
    """Remove references that commonly block stale interface deletion."""
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import IPAddress

    # Remove any child LAG memberships if this interface is a parent LAG.
    if hasattr(Interface, "lag"):
        detached_members = Interface.objects.filter(device=iface.device, lag_id=iface.pk).update(lag=None)
        if detached_members:
            log_fn(
                f"  [Interface] Detached {detached_members} member interface(s) from stale LAG "
                f"{iface.device.name}/{iface.name}."
            )

    # If this stale interface is a LAG member, detach it from its parent.
    if getattr(iface, "lag_id", None):
        iface.lag = None
        iface.save(update_fields=["lag"])
        log_fn(f"  [Interface] Detached stale interface {iface.device.name}/{iface.name} from LAG.")

    # Detach parent/child interface relationships (subinterfaces).
    if hasattr(Interface, "parent"):
        detached_children = Interface.objects.filter(device=iface.device, parent_id=iface.pk).update(parent=None)
        if detached_children:
            log_fn(
                f"  [Interface] Detached {detached_children} child interface(s) from stale parent "
                f"{iface.device.name}/{iface.name}."
            )
        if getattr(iface, "parent_id", None):
            iface.parent = None
            iface.save(update_fields=["parent"])
            log_fn(f"  [Interface] Detached stale interface {iface.device.name}/{iface.name} from parent.")

    # Unassign any IPs from this interface before deletion.
    iface_ct = ContentType.objects.get_for_model(Interface)
    ips_qs = IPAddress.objects.filter(
        assigned_object_type=iface_ct,
        assigned_object_id=iface.pk,
    )
    ip_count = ips_qs.count()
    if ip_count:
        ips_qs.update(assigned_object_type=None, assigned_object_id=None)
        log_fn(f"  [Interface] Unassigned {ip_count} IP(s) from stale interface {iface.device.name}/{iface.name}.")

    # Remove attached cable so interface deletion is not blocked.
    cable = getattr(iface, "cable", None)
    if cable is not None:
        cable.delete()
        log_fn(f"  [Interface] Deleted cable attached to stale interface {iface.device.name}/{iface.name}.")


def _sync_lag_members(device, lag_members: Dict[str, List[str]], log_fn: Callable):
    from dcim.models import Interface

    if not lag_members:
        return

    lag_field_supported = hasattr(Interface, "lag")
    if not lag_field_supported:
        log_fn("  [WARN] Interface LAG relationships are not supported by this NetBox version.")
        return

    managed_lags = {}
    desired_members_by_lag_id: Dict[int, set] = {}

    for lag_name, member_names in lag_members.items():
        lag_iface = _find_interface(device, lag_name)
        if lag_iface is None:
            lag_iface, _ = Interface.objects.get_or_create(
                device=device,
                name=lag_name,
                defaults={"type": "lag"},
            )
        elif lag_iface.type != "lag":
            lag_iface.type = "lag"
            lag_iface.save()

        managed_lags[lag_iface.pk] = lag_iface
        desired_member_names = set()

        for member_name in member_names:
            member_iface = _find_interface(device, member_name)
            if member_iface is None:
                member_iface, _ = Interface.objects.get_or_create(
                    device=device,
                    name=member_name,
                    defaults={"type": map_interface_type(member_name)},
                )
            desired_member_names.add(member_iface.name)

            current_lag = getattr(member_iface, "lag", None)
            if getattr(member_iface, "lag_id", None) != lag_iface.pk:
                old_lag_desc = current_lag.name if current_lag else "none"
                member_iface.lag = lag_iface
                member_iface.save()
                log_fn(
                    f"  [LAG] {device.name}/{member_iface.name} joined {lag_iface.name} "
                    f"(previous={old_lag_desc})."
                )
                _add_journal_entry(
                    member_iface,
                    (
                        f"Discovery updated LAG membership on {device.name}: interface "
                        f"{member_iface.name} moved from {old_lag_desc} to {lag_iface.name}."
                    ),
                )

        desired_members_by_lag_id[lag_iface.pk] = desired_member_names

    for iface in Interface.objects.filter(device=device).exclude(lag__isnull=True).select_related("lag"):
        current_lag_id = getattr(iface, "lag_id", None)
        if current_lag_id not in managed_lags:
            continue
        if iface.name in desired_members_by_lag_id.get(current_lag_id, set()):
            continue

        old_lag = iface.lag
        iface.lag = None
        iface.save()
        log_fn(f"  [LAG] {device.name}/{iface.name} removed from {old_lag.name}.")
        _add_journal_entry(
            iface,
            (
                f"Discovery removed interface {iface.name} from LAG {old_lag.name} "
                f"on {device.name} because it is no longer reported as a member."
            ),
        )


def _sync_ips(device, interfaces_ip: Dict, mgmt_ip: str, log_fn: Callable):
    from dcim.models import Interface
    from ipam.models import IPAddress
    from django.contrib.contenttypes.models import ContentType

    iface_ct = ContentType.objects.get_for_model(Interface)
    primary_ip = None
    stats = {
        "created": 0,
        "reassigned": 0,
        "conflicts": 0,
        "mgmt_created": 0,
    }

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
                if created:
                    stats["created"] += 1

                # Only move an existing IP when it's unassigned or already belongs
                # to this device. Never steal an IP from another device.
                assigned_object = ip_obj.assigned_object
                assigned_device = getattr(assigned_object, "device", None)
                if (
                    assigned_object is not None
                    and assigned_device is not None
                    and assigned_device.pk != device.pk
                ):
                    assigned_desc = f"{assigned_device.name}/{assigned_object.name}"
                    warn_msg = (
                        f"  [WARN] IP conflict — leaving {cidr} on {assigned_desc}; "
                        f"not reassigning it to {device.name}/{iface.name}."
                    )
                    log_fn(warn_msg)
                    stats["conflicts"] += 1
                    _get_conflict_logger().warning(
                        "INTERFACE IP CONFLICT | wanted_device=%s (id=%s) | "
                        "blocking_device=%s (id=%s) | ip=%s (id=%s) | "
                        "wanted_interface=%s | blocking_interface=%s",
                        device.name,
                        device.pk,
                        assigned_device.name,
                        assigned_device.pk,
                        cidr,
                        ip_obj.pk,
                        iface.name,
                        assigned_object.name,
                    )
                    continue

                if (
                    ip_obj.assigned_object_type_id != iface_ct.id
                    or ip_obj.assigned_object_id != iface.pk
                ):
                    ip_obj.assigned_object_type = iface_ct
                    ip_obj.assigned_object_id = iface.pk
                    ip_obj.save()
                    stats["reassigned"] += 1

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
            primary_ip, mgmt_created = IPAddress.objects.get_or_create(address=mgmt_cidr, defaults=kwargs)
            if mgmt_created:
                stats["mgmt_created"] += 1

    return primary_ip, stats


def _sync_virtual_chassis(
    master_device,
    hostname: str,
    stack_members: List,
    site,
    master_dtype,
    role,
    driver: str = "",
    vendor: str = "",
    log_fn: Callable = None,
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
        _add_journal_entry(
            master_device,
            (
                f"Discovery updated stack membership: virtual_chassis='{vc.name}', "
                f"position={master_device.vc_position}, priority={master_device.vc_priority}."
            ),
        )

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

        # Classify member by its own model (may differ in mixed-model stacks)
        member_cls = classify_device(
            model=member_model or master_dtype.model,
            vendor=vendor,
            driver=driver,
        )
        member_role = _ensure_role(member_cls["role"], color=member_cls["color"])

        if mem_dev is None:
            mem_dev = Device.objects.create(
                name=member_name,
                site=master_device.site,
                device_type=member_dtype,
                role=member_role,
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
            _add_journal_entry(
                mem_dev,
                (
                    "Discovery created stack member device: "
                    f"virtual_chassis='{vc.name}', position={pos}, serial={member_serial or 'N/A'}, "
                    f"model={member_model or member_dtype.model}."
                ),
            )
        else:
            changed = False
            member_changes: List[str] = []
            if mem_dev.virtual_chassis_id != vc.pk:
                mem_dev.virtual_chassis = vc
                changed = True
                member_changes.append(f"virtual_chassis -> '{vc.name}'")
            if mem_dev.vc_position != pos:
                mem_dev.vc_position = pos
                changed = True
                member_changes.append(f"position -> {pos}")
            if member_serial and mem_dev.serial != member_serial:
                mem_dev.serial = member_serial
                changed = True
                member_changes.append(f"serial -> '{member_serial}'")
            if mem_dev.device_type_id != member_dtype.pk:
                mem_dev.device_type = member_dtype
                changed = True
                member_changes.append(f"model -> '{member_dtype.model}'")
            if mem_dev.site_id != master_device.site_id:
                mem_dev.site = master_device.site
                changed = True
                member_changes.append(f"site -> '{master_device.site.name}'")
            if changed:
                mem_dev.save()
                log_fn(
                    f"  [Stack] Updated member '{mem_dev.name}' "
                    f"pos={pos} serial={member_serial or 'N/A'}"
                )
                _add_journal_entry(
                    mem_dev,
                    "Discovery updated stack member attributes: " + "; ".join(member_changes) + ".",
                )

        # Auto-tag member device
        _sync_device_tags(mem_dev, member_cls["tags"], log_fn)


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
        matches = VLAN.objects.filter(vid=vid, site=site)
        vlan = matches.first()
        created = vlan is None
        if vlan is None:
            vlan = VLAN.objects.create(
                vid=vid,
                site=site,
                name=vlan_name,
                status="active",
            )
        elif matches.count() > 1:
            log_fn(
                f"  [WARN] Found {matches.count()} VLAN rows for VID {vid} at site {site}; "
                f"using VLAN id={vlan.pk}."
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


def _describe_termination(term) -> str:
    if term is None:
        return "unassigned"
    device = getattr(term, "device", None)
    if device is not None:
        return f"{device.name}/{term.name}"
    return str(term)


def _as_termination_list(value) -> List[Any]:
    if value is None:
        return []
    if hasattr(value, "all"):
        return list(value.all())
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _get_cable_endpoints(cable) -> Tuple[Optional[Any], Optional[Any]]:
    a_terms = _as_termination_list(getattr(cable, "a_terminations", None))
    b_terms = _as_termination_list(getattr(cable, "b_terminations", None))
    return (a_terms[0] if a_terms else None, b_terms[0] if b_terms else None)


def _set_cable_endpoints(cable, a_term, b_term) -> None:
    for attr, value in (("a_terminations", a_term), ("b_terminations", b_term)):
        current = getattr(cable, attr, None)
        if hasattr(current, "set"):
            current.set([value])
        else:
            setattr(cable, attr, [value])
    cable.full_clean()
    cable.save()


def _journal_kind_info():
    try:
        from extras.choices import JournalEntryKindChoices

        for attr in ("KIND_INFO", "TYPE_INFO", "INFO"):
            if hasattr(JournalEntryKindChoices, attr):
                return getattr(JournalEntryKindChoices, attr)
    except Exception:
        pass
    return "info"


def _add_journal_entry(obj, message: str) -> bool:
    if obj is None or not message:
        return False

    try:
        from extras.models import JournalEntry
    except Exception:
        return False

    kwargs = {
        "assigned_object": obj,
        "kind": _journal_kind_info(),
        "comments": message,
    }
    try:
        JournalEntry.objects.create(**kwargs)
        return True
    except TypeError:
        kwargs.pop("comments", None)
        kwargs["comment"] = message
        try:
            JournalEntry.objects.create(**kwargs)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _update_cable_connection(cable, fixed_term, new_other_term, log_fn: Callable) -> bool:
    a_term, b_term = _get_cable_endpoints(cable)
    if a_term is None or b_term is None:
        return False

    if a_term.pk == fixed_term.pk:
        old_other = b_term
        new_a, new_b = fixed_term, new_other_term
    elif b_term.pk == fixed_term.pk:
        old_other = a_term
        new_a, new_b = new_other_term, fixed_term
    else:
        return False

    if old_other.pk == new_other_term.pk:
        return False

    _set_cable_endpoints(cable, new_a, new_b)

    message = (
        "Discovery updated this connection based on the latest neighbor data: "
        f"{_describe_termination(fixed_term)} moved from {_describe_termination(old_other)} "
        f"to {_describe_termination(new_other_term)}."
    )
    _add_journal_entry(cable, message)
    _add_journal_entry(getattr(fixed_term, "device", None), message)
    _add_journal_entry(getattr(old_other, "device", None), message)
    _add_journal_entry(getattr(new_other_term, "device", None), message)
    log_fn(f"  [Cable] Updated {_describe_termination(fixed_term)}: {_describe_termination(old_other)} -> {_describe_termination(new_other_term)}")
    return True


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
    processed_cables: set = set()

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

            desired_key = frozenset({local_iface.pk, remote_iface.pk})
            if desired_key in seen:
                continue
            seen.add(desired_key)

            if local_iface.cable_id and remote_iface.cable_id:
                if local_iface.cable_id == remote_iface.cable_id:
                    continue
                log_fn(
                    f"  [Cable] SKIP {_describe_termination(local_iface)} ↔ "
                    f"{_describe_termination(remote_iface)}: both interfaces already have different cables."
                )
                continue

            try:
                with transaction.atomic():
                    if local_iface.cable_id:
                        if local_iface.cable_id in processed_cables:
                            continue
                        if _update_cable_connection(local_iface.cable, local_iface, remote_iface, log_fn):
                            processed_cables.add(local_iface.cable_id)
                        continue

                    if remote_iface.cable_id:
                        if remote_iface.cable_id in processed_cables:
                            continue
                        if _update_cable_connection(remote_iface.cable, remote_iface, local_iface, log_fn):
                            processed_cables.add(remote_iface.cable_id)
                        continue

                    cable = Cable(
                        a_terminations=[local_iface],
                        b_terminations=[remote_iface],
                        status="connected",
                    )
                    cable.full_clean()
                    cable.save()
                    created += 1
                    processed_cables.add(cable.pk)
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
