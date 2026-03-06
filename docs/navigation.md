# netbox_discovery/navigation.py

## Purpose

Defines the plugin's navigation menu entry that appears in the NetBox sidebar under **Network Discovery**.

---

## Menu Structure

```
Network Discovery  (icon: mdi-radar)
└── Discovery
    ├── Discovery Targets  [+ Add button]   requires: netbox_discovery.view_discoverytarget
    ├── Run History                          requires: netbox_discovery.view_discoveryrun
    └── Duplicate Devices                   requires: dcim.view_device
```

---

## How to Change

- **Add a new menu item**: Add a new `PluginMenuItem(link=..., link_text=..., permissions=[...])` to the group tuple.
- **Add a button to an existing item**: Add a `PluginMenuButton(...)` to the item's `buttons` tuple.
- **Add a new group**: Add a new `("Group Name", (...items...))` tuple inside `groups`.
- **Change the icon**: Update `icon_class` on the `PluginMenu` — uses [Material Design Icons](https://pictogrammers.com/library/mdi/) prefixed with `mdi mdi-`.
- **Permissions**: Specify as `"app_label.action_modelname"` strings. Menu items are hidden from users who lack the permission.
