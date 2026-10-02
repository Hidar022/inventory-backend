from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions
from rest_framework.response import Response

from apps.dashboard.serializers import DashboardSerializer
from apps.dashboard.services import get_dashboard_metrics
from apps.organizations.permissions import IsOrganizationMember, OrganizationContextMixin


class DashboardView(OrganizationContextMixin, generics.RetrieveAPIView):
	permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
	serializer_class = DashboardSerializer

	@extend_schema(
		operation_id="dashboard_summary",
		responses=DashboardSerializer,
		description=(
			"Returns tenant-scoped sales and active-inventory metrics. Cashiers receive "
			"financial metrics and recent sales for their own sales only."
		),
	)
	def get(self, request, *args, **kwargs):
		metrics = get_dashboard_metrics(
			request.organization,
			request.role,
			request.user,
		)
		serializer = self.get_serializer(instance=metrics)
		return Response(serializer.data)
