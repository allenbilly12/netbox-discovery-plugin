# netbox-discovery

A NetBox 4.x community plugin that automatically discovers network devices using
IP addresses and CIDR ranges, crawls CDP/LLDP neighbors, and populates NetBox with
device facts, interfaces, IP addresses, and VLANs.

---

## Features

- **Seed-based discovery**: Provide IP addresses or CIDR ranges as starting points
- **ICMP ping sweep**: Finds live hosts before attempting NAPALM connections
- **NAPALM integration**: Collects device facts (hostname, vendor, model, serial, OS version),
  all interfaces, L3 IP addresses (including VLAN SVIs), and VLAN lists
- **CDP/LLDP neighbor crawling**: Recursively discovers adjacent devices up to a configurable depth
- **Safe NetBox sync**: Uses `get_or_create` everywhere — **never deletes** existing records
- **"Holding" site**: Newly discovered devices are placed in a configurable holding site
- **Existing device detection**: Matches by hostname first, then by management IP
- **Scheduled runs**: Built-in periodic job scheduler (configurable per-target interval)
- **Manual runs**: "Run Now" button in the GUI
- **Full NetBox GUI**: List/detail/edit views integrated into NetBox navigation
- **REST API**: Full CRUD API plus `POST /targets/{id}/run/` to trigger jobs

---

## Requirements

- NetBox 4.0+
- Python 3.10+
- `nmap` binary installed on the NetBox server (for ping sweep)

---

## Installation

### 1. Install the plugin

```bash
pip install netbox-discovery
# or for development:
pip install -e /path/to/netbox-discovery-plugin
```

### 2. Install system dependencies

```bash
# Ubuntu / Debian
sudo apt-get install nmap

# RHEL / CentOS
sudo yum install nmap
```

### 3. Generate an encryption key

Credentials are stored Fernet-encrypted. Generate a key once:

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

### 4. Update NetBox configuration

Add to your NetBox `configuration.py`:

```python
PLUGINS = ['netbox_discovery']

PLUGINS_CONFIG = {
    'netbox_discovery': {
        # Site name for newly discovered devices (created automatically)
        'holding_site_name': 'Holding',

        # SSH connection timeout in seconds
        'ssh_timeout': 10,

        # Fernet encryption key for stored credentials (required)
        'encryption_key': 'your-generated-key-here==',

        # Global credential fallbacks (used when per-target fields are blank)
        'default_username': '',
        'default_password': '',
        'default_enable_secret': '',
    }
}
```

### 5. Run database migrations

```bash
python manage.py migrate netbox_discovery
```

### 6. Restart NetBox and workers

```bash
sudo systemctl restart netbox netbox-rq
```

---

## Usage

### Creating a Discovery Target

1. Navigate to **Plugins → Network Discovery → Discovery Targets**
2. Click **Add Target**
3. Fill in:
   - **Name**: A descriptive label
   - **Targets**: One IP address or CIDR range per line (e.g., `10.0.0.1`, `192.168.1.0/24`)
   - **Credentials**: SSH username/password (or leave blank to use global defaults)
   - **NAPALM Driver**: `Auto-detect` tries Cisco IOS → NX-OS → EOS → JunOS → FortiOS
   - **Discovery Protocol**: LLDP, CDP, or Both
   - **Max Depth**: How many neighbor hops to follow (default: 3)
   - **Auto-run Interval**: Minutes between automatic runs (0 = manual only)
4. Click **Save**

### Running Discovery

**Manually**: Open a Discovery Target and click **Run Now**.

**Automatically**: Set `scan_interval > 0` and `enabled = True` on the target.
The built-in scheduler checks every 5 minutes and enqueues jobs for due targets.

**Via API**: `POST /api/plugins/discovery/targets/{id}/run/`

### Viewing Results

- **Run History**: Plugins → Network Discovery → Run History
- Each run shows: hosts scanned, devices created/updated, error count, and full log output
- Discovered devices appear in **Devices** with site set to the holding site
- Interfaces, IPs, and VLANs are created/updated on each device

---

## How Data is Synced to NetBox

| NAPALM data | NetBox object |
|-------------|---------------|
| `get_facts()` → hostname, vendor, model, serial | `dcim.Device`, `dcim.DeviceType`, `dcim.Manufacturer` |
| `get_interfaces()` → name, enabled, description, MTU | `dcim.Interface` |
| `get_interfaces_ip()` → IP/prefix per interface | `ipam.IPAddress` (assigned to interface) |
| `get_vlans()` → VID, name | `ipam.VLAN` (scoped to holding site) |
| Management IP (seed IP) | `dcim.Device.primary_ip4` |

### Matching logic (no deletions)

1. Check for existing `Device` with the same **hostname**
2. If not found, check for existing `IPAddress` with the management IP and follow its assignment
3. If not found, create a new device in the holding site
4. On match: update `device_type`, `serial`, and any changed interface/IP data

---

## Supported NAPALM Drivers

| Driver | Vendor / OS |
|--------|------------|
| `ios` | Cisco IOS / IOS-XE |
| `nxos_ssh` | Cisco NX-OS |
| `eos` | Arista EOS |
| `junos` | Juniper JunOS |
| `fortios` | Fortinet FortiOS |

Auto-detect tries them in the order above (Cisco-first).

---

## REST API

All endpoints are under `/api/plugins/discovery/`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/targets/` | List discovery targets |
| `POST` | `/targets/` | Create a target |
| `GET` | `/targets/{id}/` | Target detail |
| `PUT/PATCH` | `/targets/{id}/` | Update a target |
| `DELETE` | `/targets/{id}/` | Delete a target |
| `POST` | `/targets/{id}/run/` | Enqueue a discovery job |
| `GET` | `/runs/` | List discovery runs |
| `GET` | `/runs/{id}/` | Run detail with full log |

---

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `holding_site_name` | `"Holding"` | Site for newly discovered devices |
| `ssh_timeout` | `10` | Default SSH timeout (seconds) |
| `encryption_key` | `""` | Fernet key for credential encryption (**required**) |
| `default_username` | `""` | Fallback SSH username |
| `default_password` | `""` | Fallback SSH password |
| `default_enable_secret` | `""` | Fallback enable password |

---

## Troubleshooting

**Jobs not running?**
- Ensure `netbox-rq` worker is running: `systemctl status netbox-rq`
- Check NetBox system jobs in the admin panel

**Can't connect to devices?**
- Verify `nmap` is installed: `which nmap`
- Test manually: `napalm --user admin --password secret --vendor ios get_facts <ip>`
- Check SSH reachability from the NetBox server

**Wrong interfaces types?**
- Interface type is auto-detected from the interface name
- You can manually update types in NetBox after discovery

---

## License

Apache 2.0
