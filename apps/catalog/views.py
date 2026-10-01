from django.db import IntegrityError, transaction
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.catalog.models import Category, Product
from apps.catalog.serializers import CategorySerializer, ProductSerializer
from apps.organizations.permissions import (
	IsOrganizationMember,
	IsOwnerOrManager,
	OrganizationContextMixin,
)


def filter_is_active(queryset, value):
	if value is None:
		return queryset
	normalized = value.lower()
	if normalized not in {"true", "false"}:
		raise ValidationError({"is_active": "Use 'true' or 'false'."})
	return queryset.filter(is_active=normalized == "true")


CATALOG_UNIQUE_CONFLICTS = {
	"unique_category_name_per_organization_ci": (
		"name",
		"A category with this name already exists in this organization.",
	),
	"unique_product_sku_per_organization": (
		"sku",
		"A product with this SKU already exists in this organization.",
	),
}


def get_catalog_unique_conflict(error):
	database_error = error.__cause__ or error
	constraint_name = getattr(getattr(database_error, "diag", None), "constraint_name", None)
	if constraint_name in CATALOG_UNIQUE_CONFLICTS:
		return CATALOG_UNIQUE_CONFLICTS[constraint_name]

	database_message = str(database_error)
	for name, conflict in CATALOG_UNIQUE_CONFLICTS.items():
		if name in database_message:
			return conflict
	return None


def save_catalog_serializer(serializer, **kwargs):
	try:
		with transaction.atomic():
			return serializer.save(**kwargs)
	except IntegrityError as exc:
		conflict = get_catalog_unique_conflict(exc)
		if conflict is None:
			raise
		field, message = conflict
		raise ValidationError({field: [message]}) from exc


class CatalogViewSet(
	OrganizationContextMixin,
	mixins.ListModelMixin,
	mixins.CreateModelMixin,
	mixins.RetrieveModelMixin,
	mixins.UpdateModelMixin,
	viewsets.GenericViewSet,
):
	permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

	def get_permissions(self):
		permissions_list = super().get_permissions()
		if self.request.method not in permissions.SAFE_METHODS:
			permissions_list.append(IsOwnerOrManager())
		return permissions_list

	def get_queryset(self):
		organization = getattr(self.request, "organization", None)
		if organization is None:
			return self.queryset.none()
		return self.queryset.filter(organization=organization)

	def perform_create(self, serializer):
		organization = getattr(self.request, "organization", None)
		if organization is None:
			raise PermissionDenied("You do not have access to an active organization.")
		save_catalog_serializer(serializer, organization=organization)

	def perform_update(self, serializer):
		save_catalog_serializer(serializer)

	def destroy(self, request, *args, **kwargs):
		raise MethodNotAllowed("DELETE", detail="Catalog records cannot be deleted.")

	@action(detail=True, methods=["post"])
	@extend_schema(
		request=None,
		responses={200: OpenApiTypes.OBJECT},
		description="Owner and Manager only. Deactivate without deleting the record.",
	)
	def deactivate(self, request, *args, **kwargs):
		instance = self.get_object()
		instance.is_active = False
		instance.save(update_fields=["is_active", "updated_at"])
		return Response(self.get_serializer(instance).data, status=status.HTTP_200_OK)

	@action(detail=True, methods=["post"])
	@extend_schema(
		request=None,
		responses={200: OpenApiTypes.OBJECT},
		description="Owner and Manager only. Reactivate this catalog record.",
	)
	def reactivate(self, request, *args, **kwargs):
		instance = self.get_object()
		instance.is_active = True
		instance.save(update_fields=["is_active", "updated_at"])
		return Response(self.get_serializer(instance).data, status=status.HTTP_200_OK)


@extend_schema_view(
	list=extend_schema(
		description=(
			"List categories in the active organization. Results are paginated; "
			"search matches category names and is_active accepts true or false. "
			"All active organization members may read; Owner and Manager may mutate."
		),
		parameters=[
			OpenApiParameter("search", str, OpenApiParameter.QUERY),
			OpenApiParameter("is_active", bool, OpenApiParameter.QUERY),
			OpenApiParameter("page", int, OpenApiParameter.QUERY),
		],
	),
	create=extend_schema(
		description="Owner and Manager only. Create a category in the active organization."
	),
	update=extend_schema(description="Owner and Manager only. Update a category."),
	partial_update=extend_schema(description="Owner and Manager only. Update a category."),
)
class CategoryViewSet(CatalogViewSet):
	queryset = Category.objects.all()
	serializer_class = CategorySerializer

	def get_queryset(self):
		queryset = super().get_queryset()
		search = self.request.query_params.get("search")
		if search:
			queryset = queryset.filter(name__icontains=search.strip())
		return filter_is_active(queryset, self.request.query_params.get("is_active"))


@extend_schema_view(
	list=extend_schema(
		description=(
			"List products in the active organization. Supports name/SKU search, "
			"category ID filtering, and is_active filtering. Stock quantity is "
			"read-only here; inventory movements belong to a later API phase. "
			"Cost price is omitted for Cashier members. All active members may "
			"read; Owner and Manager may mutate."
		),
		parameters=[
			OpenApiParameter("search", str, OpenApiParameter.QUERY),
			OpenApiParameter("category", int, OpenApiParameter.QUERY),
			OpenApiParameter("is_active", bool, OpenApiParameter.QUERY),
			OpenApiParameter("page", int, OpenApiParameter.QUERY),
		],
	),
	create=extend_schema(
		description="Owner and Manager only. Create a product in the active organization."
	),
	update=extend_schema(description="Owner and Manager only. Update a product."),
	partial_update=extend_schema(description="Owner and Manager only. Update a product."),
)
class ProductViewSet(CatalogViewSet):
	queryset = Product.objects.select_related("category").all()
	serializer_class = ProductSerializer

	def get_queryset(self):
		queryset = super().get_queryset()
		search = self.request.query_params.get("search")
		if search:
			search = search.strip()
			queryset = queryset.filter(Q(name__icontains=search) | Q(sku__icontains=search))
		category = self.request.query_params.get("category")
		if category:
			try:
				category_id = int(category)
			except ValueError as exc:
				raise ValidationError({"category": "A valid category ID is required."}) from exc
			queryset = queryset.filter(category_id=category_id)
		return filter_is_active(queryset, self.request.query_params.get("is_active"))
