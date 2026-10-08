from django.db import transaction
from django.db.models import Prefetch, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, mixins

from apps.catalog.models import Product
from apps.organizations.permissions import (
    IsOrganizationMember,
    IsOwnerOrManager,
    OrganizationContextMixin,
)
from apps.purchases.models import Purchase, PurchaseItem, Supplier
from apps.purchases.serializers import (
    PurchaseCreateSerializer,
    PurchaseSerializer,
    PurchaseUpdateSerializer,
    SupplierSerializer,
)
from apps.purchases.services import cancel_purchase, receive_purchase


class SupplierViewSet(
    OrganizationContextMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet,
):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer

    def get_permissions(self):
        permissions_list = [permissions.IsAuthenticated(), IsOrganizationMember()]
        if self.request.method not in permissions.SAFE_METHODS:
            permissions_list.append(IsOwnerOrManager())
        return permissions_list

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Supplier.objects.none()
        queryset = Supplier.objects.filter(organization=organization)
        search = self.request.query_params.get("search")
        if search:
            search = search.strip()
            queryset = queryset.filter(Q(name__icontains=search) | Q(email__icontains=search) | Q(phone__icontains=search))
        return queryset.order_by("name", "-created_at")

    def perform_create(self, serializer):
        serializer.save(organization=self.request.organization)

    def perform_update(self, serializer):
        serializer.save()

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        return Response(self.get_serializer(instance).data, status=status.HTTP_200_OK)


@extend_schema_view(
    list=extend_schema(
        description="List purchases in the active organization. All active members can read; only owners and managers can create or modify draft purchases.",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("supplier", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY),
        ],
    ),
    create=extend_schema(description="Owner and Manager only. Create a draft purchase for the active organization."),
    retrieve=extend_schema(description="Retrieve a purchase from the active organization."),
    partial_update=extend_schema(description="Owner and Manager only. Edit a DRAFT purchase."),
)
class PurchaseViewSet(
    OrganizationContextMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet,
):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
    queryset = Purchase.objects.all()
    serializer_class = PurchaseSerializer

    def get_permissions(self):
        permissions_list = [permissions.IsAuthenticated(), IsOrganizationMember()]
        if self.request.method not in permissions.SAFE_METHODS:
            permissions_list.append(IsOwnerOrManager())
        return permissions_list

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Purchase.objects.none()
        queryset = (
            Purchase.objects.filter(organization=organization)
            .select_related("supplier", "created_by", "organization")
            .prefetch_related(
                Prefetch("items", queryset=PurchaseItem.objects.order_by("id"))
            )
        )
        search = self.request.query_params.get("search")
        if search:
            search = search.strip()
            queryset = queryset.filter(
                Q(reference_number__icontains=search)
                | Q(supplier__name__icontains=search)
                | Q(notes__icontains=search)
            )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        supplier_id = self.request.query_params.get("supplier")
        if supplier_id:
            queryset = queryset.filter(supplier_id=supplier_id)
        return queryset.order_by("-created_at", "-id")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PurchaseCreateSerializer
        if self.request.method in {"PATCH", "PUT"}:
            return PurchaseUpdateSerializer
        return PurchaseSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        purchase = serializer.save()
        response_serializer = PurchaseSerializer(purchase, context={"request": request})
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial, context={"request": request})
        serializer.is_valid(raise_exception=True)
        purchase = serializer.save()
        response_serializer = PurchaseSerializer(purchase, context={"request": request})
        return Response(response_serializer.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="receive")
    def receive(self, request, *args, **kwargs):
        purchase = self.get_object()
        purchase = receive_purchase(
            organization=request.organization,
            purchase_id=str(purchase.pk),
            actor=request.user,
        )
        return Response(PurchaseSerializer(purchase, context={"request": request}).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, *args, **kwargs):
        purchase = self.get_object()
        purchase = cancel_purchase(organization=request.organization, purchase_id=str(purchase.pk))
        return Response(PurchaseSerializer(purchase, context={"request": request}).data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        raise ValidationError({"detail": ["Purchases cannot be deleted."]})
