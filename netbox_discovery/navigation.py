from netbox.plugins.navigation import PluginMenu, PluginMenuItem, PluginMenuButton

menu = PluginMenu(
    label="Network Discovery",
    groups=(
        (
            "Discovery",
            (
                PluginMenuItem(
                    link="plugins:netbox_discovery:discoverytarget_list",
                    link_text="Discovery Targets",
                    permissions=["netbox_discovery.view_discoverytarget"],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_discovery:discoverytarget_add",
                            title="Add Target",
                            icon_class="mdi mdi-plus-thick",
                            color="green",
                            permissions=["netbox_discovery.add_discoverytarget"],
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_discovery:discoveryrun_list",
                    link_text="Run History",
                    permissions=["netbox_discovery.view_discoveryrun"],
                ),
                PluginMenuItem(
                    link="plugins:netbox_discovery:duplicate_devices",
                    link_text="Duplicate Devices",
                    permissions=["dcim.view_device"],
                ),
            ),
        ),
    ),
    icon_class="mdi mdi-radar",
)
