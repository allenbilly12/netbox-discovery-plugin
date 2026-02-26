"""
NAPALM driver auto-detection.

Tries each driver in priority order (Cisco IOS/NX-OS first per preference).
Returns the first driver that successfully connects and calls get_facts().
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger("netbox.plugins.netbox_discovery")

# Detection order: Cisco-first per user preference, then others
DETECTION_ORDER = ["ios", "nxos_ssh", "eos", "junos", "fortios"]


def _try_driver(
    driver_name: str,
    ip: str,
    username: str,
    password: str,
    enable_secret: str,
    timeout: int,
) -> Optional[object]:
    """
    Attempt to connect using the named NAPALM driver.
    Returns the open driver instance on success, None on failure.
    """
    try:
        from napalm import get_network_driver

        driver_cls = get_network_driver(driver_name)
        optional_args = {}
        if enable_secret:
            optional_args["secret"] = enable_secret

        device = driver_cls(
            hostname=ip,
            username=username,
            password=password,
            timeout=timeout,
            optional_args=optional_args,
        )
        device.open()
        # Quick smoke-test to confirm connectivity
        device.get_facts()
        return device
    except Exception as exc:
        logger.debug("Driver %s failed for %s: %s", driver_name, ip, exc)
        return None


def detect_and_connect(
    ip: str,
    username: str,
    password: str,
    enable_secret: str = "",
    timeout: int = 10,
    preferred_driver: str = "auto",
) -> Tuple[Optional[object], Optional[str]]:
    """
    Attempt to connect to a device using NAPALM.

    Args:
        ip: Device management IP.
        username: SSH username.
        password: SSH password.
        enable_secret: Optional enable/privilege password.
        timeout: Connection timeout in seconds.
        preferred_driver: NAPALM driver name or 'auto' to try all.

    Returns:
        Tuple of (open NAPALM driver instance, driver_name) or (None, None).
    """
    if preferred_driver and preferred_driver != "auto":
        drivers_to_try = [preferred_driver]
    else:
        drivers_to_try = DETECTION_ORDER

    for driver_name in drivers_to_try:
        logger.debug("Trying driver '%s' for %s", driver_name, ip)
        device = _try_driver(driver_name, ip, username, password, enable_secret, timeout)
        if device is not None:
            logger.info("Connected to %s using driver '%s'", ip, driver_name)
            return device, driver_name

    logger.warning("Could not connect to %s with any driver", ip)
    return None, None
