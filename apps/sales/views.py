from django.db.models import Prefetch
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.permissions import IsOrganizationMember, OrganizationContextMixin
from apps.sales.models import Payment, Sale, SaleItem
from apps.sales.pagination import SalesPagination
from apps.sales.queries import apply_sales_filters, visible_sales_queryset
from apps.sales.serializers import CheckoutSerializer, SaleSerializer, SalesFilterSerializer
from apps.sales.services import checkout_sale


class CheckoutView(OrganizationContextMixin, APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

    @extend_schema(
        request=CheckoutSerializer,
        responses={200: SaleSerializer, 201: SaleSerializer},
        description=(
            "Create a tenant-scoped sale, validate stock, authorize discounts, and record payments "
            "in one atomic transaction."
        ),
    )
    def post(self, request, *args, **kwargs):
        serializer = CheckoutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        sale, created = checkout_sale(request, serializer.validated_data)
        response_serializer = SaleSerializer(sale, context={"request": request})
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SalesListView(OrganizationContextMixin, generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
    serializer_class = SaleSerializer
    pagination_class = SalesPagination

    @extend_schema(
        operation_id="sales_list",
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, description="Page number (1-based)."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Page size, capped at 100."),
            OpenApiParameter("from", OpenApiTypes.DATE, description="Inclusive start date in Africa/Lagos."),
            OpenApiParameter("to", OpenApiTypes.DATE, description="Inclusive end date in Africa/Lagos."),
            OpenApiParameter("search", OpenApiTypes.STR, description="Search receipt, cashier, item name, or SKU."),
            OpenApiParameter("payment_method", OpenApiTypes.STR, enum=[choice[0] for choice in Payment.Method.choices]),
            OpenApiParameter("payment_status", OpenApiTypes.STR, enum=[choice[0] for choice in Sale.PaymentStatus.choices]),
            OpenApiParameter("cashier", OpenApiTypes.INT, description="Filter by a cashier user ID."),
        ],
        responses={400: OpenApiTypes.OBJECT},
        description="Lists sales for the active organization. Cashiers always see only their own sales.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Sale.objects.none()
        filter_data = {
            "date_from": self.request.query_params.get("from"),
            "date_to": self.request.query_params.get("to"),
            "search": self.request.query_params.get("search", ""),
            "payment_method": self.request.query_params.get("payment_method"),
            "payment_status": self.request.query_params.get("payment_status"),
            "cashier": self.request.query_params.get("cashier"),
        }
        filter_data = {key: value for key, value in filter_data.items() if value is not None}
        filter_serializer = SalesFilterSerializer(data=filter_data)
        filter_serializer.is_valid(raise_exception=True)
        queryset = visible_sales_queryset(organization, self.request.role, self.request.user)
        queryset = apply_sales_filters(queryset, filter_serializer.validated_data)
        return queryset.prefetch_related(
            Prefetch("items", queryset=SaleItem.objects.order_by("id")),
            Prefetch("payments", queryset=Payment.objects.order_by("id")),
        ).select_related("cashier", "organization").order_by("-created_at", "-id")


class SaleDetailView(OrganizationContextMixin, generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
    serializer_class = SaleSerializer
    lookup_field = "id"

    @extend_schema(
        operation_id="sale_detail",
        responses=SaleSerializer,
        description="Returns a sale in the active organization; cashiers can retrieve only their own sales.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Sale.objects.none()
        queryset = visible_sales_queryset(organization, self.request.role, self.request.user).prefetch_related(
            Prefetch("items", queryset=SaleItem.objects.order_by("id")),
            Prefetch("payments", queryset=Payment.objects.order_by("id")),
        )
        return queryset.select_related("cashier", "organization")
