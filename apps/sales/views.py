from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.permissions import IsOrganizationMember, OrganizationContextMixin
from apps.sales.models import Payment, Sale, SaleItem
from apps.sales.serializers import CheckoutSerializer, SaleSerializer
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

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Sale.objects.none()
        queryset = Sale.objects.filter(organization=organization).prefetch_related(
            Prefetch("items", queryset=SaleItem.objects.order_by("id")),
            Prefetch("payments", queryset=Payment.objects.order_by("id")),
        )
        if self.request.role == "cashier":
            queryset = queryset.filter(cashier=self.request.user)
        return queryset.select_related("cashier", "organization").order_by("-created_at")


class SaleDetailView(OrganizationContextMixin, generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
    serializer_class = SaleSerializer
    lookup_field = "id"

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Sale.objects.none()
        queryset = Sale.objects.filter(organization=organization).prefetch_related(
            Prefetch("items", queryset=SaleItem.objects.order_by("id")),
            Prefetch("payments", queryset=Payment.objects.order_by("id")),
        )
        if self.request.role == "cashier":
            queryset = queryset.filter(cashier=self.request.user)
        return queryset.select_related("cashier", "organization")
