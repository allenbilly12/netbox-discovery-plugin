import logging

from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel

from .config import get_setting
from .choices import (
    NapalmDriverChoices,
    DiscoveryProtocolChoices,
    DiscoveryRunStatusChoices,
)

logger = logging.getLogger("netbox.plugins.netbox_discovery")


def _get_fernet():
    """Return a Fernet instance using the configured encryption key, or None."""
    try:
        from cryptography.fernet import Fernet

        key = get_setting("encryption_key")
        if not key:
            return None
        if isinstance(key, str):
            key = key.encode()
        return Fernet(key)
    except Exception:
        return None


def encrypt_value(raw: str) -> str:
    """Encrypt a string value. Returns raw if no key configured."""
    if not raw:
        return raw
    f = _get_fernet()
    if f is None:
        return raw
    return f.encrypt(raw.encode()).decode()


def decrypt_value(stored: str) -> str:
    """Decrypt a stored value. Returns stored if no key configured."""
    if not stored:
        return stored
    f = _get_fernet()
    if f is None:
        return stored
    try:
        return f.decrypt(stored.encode()).decode()
    except Exception:
        return stored


class DiscoveryTarget(NetBoxModel):
    """
    Defines a set of seed IPs / CIDRs to discover, along with credentials
    and scheduling configuration.
    """

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=500, blank=True)
    targets = models.TextField(
        help_text=(
            "One IP address or CIDR range per line. "
            "Example: 10.0.0.1 or 192.168.1.0/24"
        )
    )
    exclusions = models.TextField(
        blank=True,
        help_text=(
            "IPs or CIDR ranges to exclude from scanning, one per line. "
            "Example: 10.0.0.5 or 192.168.1.0/28"
        ),
    )

    # Credentials (optional — falls back to PLUGINS_CONFIG defaults)
    credential_username = models.CharField(
        max_length=100,
        blank=True,
        help_text="SSH username. Leave blank to use global default.",
    )
    _credential_password = models.CharField(
        max_length=512,
        blank=True,
        db_column="credential_password",
    )
    _enable_secret = models.CharField(
        max_length=512,
        blank=True,
        db_column="enable_secret",
    )

    # NAPALM settings
    napalm_driver = models.CharField(
        max_length=20,
        choices=NapalmDriverChoices.choices,
        default=NapalmDriverChoices.AUTO,
    )

    # Discovery settings
    discovery_protocol = models.CharField(
        max_length=10,
        choices=DiscoveryProtocolChoices.choices,
        default=DiscoveryProtocolChoices.BOTH,
    )
    max_depth = models.PositiveIntegerField(
        default=3,
        help_text="Maximum CDP/LLDP neighbor recursion depth.",
    )
    ssh_timeout = models.PositiveIntegerField(
        default=10,
        help_text="SSH connection timeout in seconds.",
    )
    max_workers = models.PositiveIntegerField(
        default=5,
        help_text="Number of devices to crawl in parallel. Increase for faster discovery on large networks.",
    )

    # Scheduling
    scan_interval = models.PositiveIntegerField(
        default=0,
        help_text="Auto-run interval in minutes. Set to 0 to disable scheduled runs.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Enable or disable scheduled runs for this target.",
    )
    last_run = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Discovery Target"
        verbose_name_plural = "Discovery Targets"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_discovery:discoverytarget", args=[self.pk])

    # Password property accessors
    @property
    def credential_password(self):
        return decrypt_value(self._credential_password)

    @credential_password.setter
    def credential_password(self, raw):
        self._credential_password = encrypt_value(raw)

    @property
    def enable_secret(self):
        return decrypt_value(self._enable_secret)

    @enable_secret.setter
    def enable_secret(self, raw):
        self._enable_secret = encrypt_value(raw)

    @property
    def has_password(self):
        """Template-safe check for whether a per-target password is stored."""
        return bool(self._credential_password)

    @property
    def has_enable_secret(self):
        """Template-safe check for whether a per-target enable secret is stored."""
        return bool(self._enable_secret)

    def get_effective_username(self):
        """Return per-target username or fall back to global config."""
        if self.credential_username:
            return self.credential_username
        return get_setting("default_username")

    def get_effective_password(self):
        """Return per-target password or fall back to global config."""
        pw = self.credential_password
        if pw:
            return pw
        return get_setting("default_password")

    def get_effective_enable_secret(self):
        """Return per-target enable secret or fall back to global config."""
        sec = self.enable_secret
        if sec:
            return sec
        return get_setting("default_enable_secret")

    def get_target_list(self):
        """Return list of non-empty target strings."""
        return [t.strip() for t in self.targets.splitlines() if t.strip()]

    def get_exclusion_list(self):
        """Return list of non-empty exclusion strings."""
        return [e.strip() for e in self.exclusions.splitlines() if e.strip()]


class DiscoveryRun(NetBoxModel):
    """
    Records the outcome of a single discovery execution for a DiscoveryTarget.
    """

    target = models.ForeignKey(
        DiscoveryTarget,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    status = models.CharField(
        max_length=20,
        choices=DiscoveryRunStatusChoices.choices,
        default=DiscoveryRunStatusChoices.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Counters
    hosts_scanned = models.IntegerField(default=0)
    devices_created = models.IntegerField(default=0)
    devices_updated = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)

    # Full log output stored as text
    log = models.TextField(blank=True)

    # Structured per-device results: list of {ip, hostname, status, driver, error}
    device_results = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Discovery Run"
        verbose_name_plural = "Discovery Runs"

    def __str__(self):
        ts = self.started_at.strftime("%Y-%m-%d %H:%M") if self.started_at else "?"
        return f"{self.target.name} @ {ts}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_discovery:discoveryrun", args=[self.pk])

    def append_log(self, message: str):
        if self.log:
            self.log += f"\n{message}"
        else:
            self.log = message


class MacAddressTableEntry(NetBoxModel):
    """
    A single MAC address learned on a device interface.

    Populated from NAPALM get_mac_address_table(). The set of entries for
    a given device is replaced wholesale on each successful discovery run,
    so this table reflects the device's last reported view of L2 forwarding.
    """

    device = models.ForeignKey(
        "dcim.Device",
        on_delete=models.CASCADE,
        related_name="discovered_mac_entries",
    )
    interface = models.ForeignKey(
        "dcim.Interface",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discovered_mac_entries",
    )
    interface_name = models.CharField(
        max_length=100,
        help_text="Raw interface name as reported by the device (preserved for unresolved entries).",
    )
    mac_address = models.CharField(max_length=17, db_index=True)
    vlan = models.ForeignKey(
        "ipam.VLAN",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    vlan_vid = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    is_static = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["device", "vlan_vid", "mac_address"]
        verbose_name = "MAC Address Table Entry"
        verbose_name_plural = "MAC Address Table Entries"
        constraints = [
            models.UniqueConstraint(
                fields=["device", "mac_address", "vlan_vid", "interface_name"],
                name="discovery_mac_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["mac_address", "vlan_vid"]),
        ]

    def __str__(self):
        vid = f" vlan {self.vlan_vid}" if self.vlan_vid else ""
        return f"{self.mac_address}{vid} on {self.device}/{self.interface_name}"

    def get_absolute_url(self):
        # MAC table entries are transient and don't have their own detail page —
        # link through to the resolved interface (or the device) instead.
        if self.interface_id:
            return self.interface.get_absolute_url()
        return self.device.get_absolute_url()
