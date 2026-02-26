from netbox.plugins import PluginConfig


class DiscoveryConfig(PluginConfig):
    name = "netbox_discovery"
    verbose_name = "Network Discovery"
    description = "Discovers network devices via CDP/LLDP and NAPALM, syncing facts into NetBox"
    version = "1.0.0"
    author = "NetBox Discovery Contributors"
    author_email = "noreply@example.com"
    base_url = "discovery"
    min_version = "4.0.0"

    default_config = {
        "holding_site_name": "Holding",
        "ssh_timeout": 10,
        "encryption_key": "",
        "default_username": "",
        "default_password": "",
        "default_enable_secret": "",
    }

    required_config = []


config = DiscoveryConfig
