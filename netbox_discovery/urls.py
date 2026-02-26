from django.urls import path

from . import views

app_name = "netbox_discovery"

urlpatterns = [
    # Discovery Targets
    path(
        "targets/",
        views.DiscoveryTargetListView.as_view(),
        name="discoverytarget_list",
    ),
    path(
        "targets/add/",
        views.DiscoveryTargetEditView.as_view(),
        name="discoverytarget_add",
    ),
    path(
        "targets/<int:pk>/",
        views.DiscoveryTargetView.as_view(),
        name="discoverytarget",
    ),
    path(
        "targets/<int:pk>/edit/",
        views.DiscoveryTargetEditView.as_view(),
        name="discoverytarget_edit",
    ),
    path(
        "targets/<int:pk>/delete/",
        views.DiscoveryTargetDeleteView.as_view(),
        name="discoverytarget_delete",
    ),
    path(
        "targets/delete/",
        views.DiscoveryTargetBulkDeleteView.as_view(),
        name="discoverytarget_bulk_delete",
    ),
    path(
        "targets/<int:pk>/run/",
        views.DiscoveryTargetRunView.as_view(),
        name="discoverytarget_run",
    ),
    # Discovery Runs (read-only)
    path(
        "runs/",
        views.DiscoveryRunListView.as_view(),
        name="discoveryrun_list",
    ),
    path(
        "runs/<int:pk>/",
        views.DiscoveryRunView.as_view(),
        name="discoveryrun",
    ),
    path(
        "runs/<int:pk>/delete/",
        views.DiscoveryRunDeleteView.as_view(),
        name="discoveryrun_delete",
    ),
    path(
        "runs/delete/",
        views.DiscoveryRunBulkDeleteView.as_view(),
        name="discoveryrun_bulk_delete",
    ),
]
