from django.db import models


class NapalmDriverChoices(models.TextChoices):
    AUTO = "auto", "Auto-detect"
    IOS = "ios", "Cisco IOS / IOS-XE"
    NXOS = "nxos", "Cisco NX-OS (SSH)"
    NXOS_API = "nxos_ssh", "Cisco NX-OS (NX-API)"
    EOS = "eos", "Arista EOS"
    JUNOS = "junos", "Juniper JunOS"
    FORTIOS = "fortios", "Fortinet FortiOS"


class DiscoveryProtocolChoices(models.TextChoices):
    LLDP = "lldp", "LLDP"
    CDP = "cdp", "CDP"
    BOTH = "both", "Both (LLDP + CDP)"


class DiscoveryRunStatusChoices(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    PARTIAL = "partial", "Partial (some errors)"
