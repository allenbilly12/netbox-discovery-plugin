# netbox_discovery/__init__.py

## Purpose

Plugin entry point. Defines the `DiscoveryConfig` class (subclass of `PluginConfig`) that NetBox loads to register the plugin.

Also ensures the `os_version` custom field exists on the `Device` model after every migration run.

## Key Responsibilities

- Declares plugin metadata: name, version, base URL (`/plugins/discovery/`), minimum NetBox version
- Declares `default_config` keys consumed from `PLUGINS_CONFIG['netbox_discovery']`
- Hooks into the `post_migrate` signal to safely call `_ensure_os_version_custom_field()` (avoids `RuntimeWarning` from DB access during app initialisation)

## Config Keys (default_config)

| Key | Default | Description |
|-----|---------|-------------|
| `holding_site_name` | `"Holding"` | Site for newly discovered devices |
| `ssh_timeout` | `10` | Default SSH timeout in seconds |
| `encryption_key` | `""` | Fernet key for credential encryption |
| `default_username` | `""` | Fallback SSH username |
| `default_password` | `""` | Fallback SSH password |
| `default_enable_secret` | `""` | Fallback enable/privilege password |

## os_version Custom Field

`_ensure_os_version_custom_field()` creates a `CustomField` named `os_version` of type `text` on the `Device` content type if it doesn't already exist. This is called once per `post_migrate` signal — not on every request.

## How to Change

- **Add a new config key**: Add it to `default_config`. Read it with `settings.PLUGINS_CONFIG.get('netbox_discovery', {}).get('your_key', default)`.
- **Change plugin version**: Update the `version` attribute.
- **Add another post-migrate action**: Call it from `_on_post_migrate()` (wrap in try/except so a failure doesn't break migrations).
