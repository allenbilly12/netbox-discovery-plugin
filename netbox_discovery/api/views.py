import logging

from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import DiscoveryRun, DiscoveryTarget
from .serializers import DiscoveryRunSerializer, DiscoveryTargetSerializer

logger = logging.getLogger("netbox.plugins.netbox_discovery")


class DiscoveryTargetViewSet(NetBoxModelViewSet):
    queryset = DiscoveryTarget.objects.prefetch_related("tags")
    serializer_class = DiscoveryTargetSerializer

    @action(detail=True, methods=["post"], url_path="run")
    def run(self, request, pk=None):
        """Enqueue a DiscoveryJob for this target."""
        target = self.get_object()

        if not target.enabled:
            return Response(
                {"detail": f"Target '{target.name}' is disabled."},
                status=400,
            )

        if not target.get_effective_username() or not target.get_effective_password():
            return Response(
                {"detail": "No credentials configured for this target."},
                status=400,
            )

        try:
            from ..jobs import DiscoveryJob

            from ..jobs import JOB_TIMEOUT
            DiscoveryJob.enqueue(
                data={"target_id": target.pk},
                name=f"Discovery: {target.name}",
                job_timeout=JOB_TIMEOUT,
            )
            return Response({"detail": f"Discovery job enqueued for '{target.name}'."})
        except Exception as exc:
            logger.exception("API: Failed to enqueue job for target %s", target.pk)
            return Response({"detail": str(exc)}, status=500)


class DiscoveryRunViewSet(NetBoxModelViewSet):
    queryset = DiscoveryRun.objects.select_related("target").order_by("-started_at")
    serializer_class = DiscoveryRunSerializer
    http_method_names = ["get", "head", "options"]  # Read-only
