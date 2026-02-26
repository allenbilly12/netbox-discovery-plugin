from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.register("targets", views.DiscoveryTargetViewSet)
router.register("runs", views.DiscoveryRunViewSet)

urlpatterns = router.urls
