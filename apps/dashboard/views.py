from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.dashboard.models import ActivityEvent
from apps.dashboard.serializers import ActivityEventSerializer, DashboardReportSerializer, DashboardSerializer
from apps.dashboard.services import get_activity_feed, get_dashboard_metrics, get_reports_summary
from apps.organizations.permissions import (
	IsOrganizationMember,
	IsOwner,
	IsOwnerOrManager,
	OrganizationContextMixin,
)
from apps.sales.queries import local_day_bounds


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


class DashboardReportsView(OrganizationContextMixin, generics.GenericAPIView):
	permission_classes = [permissions.IsAuthenticated, IsOrganizationMember, IsOwnerOrManager]
	serializer_class = DashboardReportSerializer

	@extend_schema(
		operation_id="dashboard_reports",
		responses=DashboardReportSerializer,
		parameters=[
			OpenApiParameter("from", OpenApiTypes.DATE, OpenApiParameter.QUERY),
			OpenApiParameter("to", OpenApiTypes.DATE, OpenApiParameter.QUERY),
		],
		description="Returns a tenant-scoped, server-authoritative report for the inclusive Africa/Lagos date range. Defaults to the current month.",
	)
	def get(self, request, *args, **kwargs):
		today = timezone.localdate()
		date_from = self.parse_report_date("from", request.query_params.get("from")) or today.replace(day=1)
		date_to = self.parse_report_date("to", request.query_params.get("to")) or today
		if date_from > date_to:
			raise ValidationError({"date_range": "The start date must be on or before the end date."})
		if date_to > today:
			raise ValidationError({"to": "The end date cannot be in the future."})
		local_day_bounds(date_from)
		local_day_bounds(date_to)
		report = get_reports_summary(
			request.organization,
			date_from,
			date_to,
		)
		serializer = self.get_serializer(instance=report)
		return Response(serializer.data)

	@staticmethod
	def parse_report_date(parameter, value):
		if value is None:
			return None
		try:
			parsed = parse_date(value)
		except (TypeError, ValueError):
			parsed = None
		if parsed is None:
			raise ValidationError({parameter: "Use a valid date in YYYY-MM-DD format."})
		return parsed


class ActivityEventListView(OrganizationContextMixin, generics.ListAPIView):
	permission_classes = [permissions.IsAuthenticated, IsOrganizationMember, IsOwner]
	serializer_class = ActivityEventSerializer

	@extend_schema(
		operation_id="dashboard_activity",
		responses=ActivityEventSerializer(many=True),
		description="Returns the tenant-scoped activity feed for the active organization.",
	)
	def get_queryset(self):
		queryset = ActivityEvent.objects.filter(organization=self.request.organization).select_related("actor")
		search = (self.request.query_params.get("search") or "").strip()
		action = (self.request.query_params.get("action") or "").strip()
		date_from = (self.request.query_params.get("date_from") or "").strip()
		date_to = (self.request.query_params.get("date_to") or "").strip()
		queryset = get_activity_feed(
			self.request.organization,
			search=search,
			action=action or None,
			date_from=date_from or None,
			date_to=date_to or None,
		)
		return queryset

	def get(self, request, *args, **kwargs):
		return super().get(request, *args, **kwargs)
