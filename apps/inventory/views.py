from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product
from apps.inventory.models import StockMovement
from apps.inventory.serializers import (
	InventoryProductSerializer,
	StockAdjustmentSerializer,
	StockInSerializer,
	StockMovementSerializer,
)
from apps.inventory.services import create_stock_movement
from apps.organizations.permissions import (
	IsOrganizationMember,
	IsOwnerOrManager,
	OrganizationContextMixin,
)


def filter_boolean(queryset, parameter, value):
	if value is None:
		return queryset
	normalized = value.lower()
	if normalized not in {"true", "false"}:
		raise ValidationError({parameter: ["Use 'true' or 'false'."]})
	return queryset.filter(is_active=normalized == "true")


def filter_category(queryset, value):
	if value is None:
		return queryset
	try:
		category_id = int(value)
	except ValueError as exc:
		raise ValidationError({"category": ["A valid category ID is required."]}) from exc
	return queryset.filter(category_id=category_id)


def filter_movement_date(queryset, parameter, lookup, value):
	if value is None:
		return queryset

	parsed_date = parse_date(value)
	if parsed_date is not None:
		return queryset.filter(**{f"created_at__date__{lookup}": parsed_date})

	parsed_datetime = parse_datetime(value)
	if parsed_datetime is not None:
		if timezone.is_naive(parsed_datetime):
			parsed_datetime = timezone.make_aware(parsed_datetime)
		return queryset.filter(**{f"created_at__{lookup}": parsed_datetime})
	raise ValidationError({parameter: ["Use an ISO date or datetime."]})


class InventoryListView(OrganizationContextMixin, generics.ListAPIView):
	permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
	serializer_class = InventoryProductSerializer

	def get_queryset(self):
		organization = getattr(self.request, "organization", None)
		if organization is None:
			return Product.objects.none()
		queryset = Product.objects.filter(organization=organization).select_related("category")
		search = self.request.query_params.get("search")
		if search:
			search = search.strip()
			queryset = queryset.filter(Q(name__icontains=search) | Q(sku__icontains=search))
		queryset = filter_category(queryset, self.request.query_params.get("category"))
		active = self.request.query_params.get("active")
		if active is None:
			active = self.request.query_params.get("is_active")
		queryset = filter_boolean(queryset, "active", active)
		low_stock = self.request.query_params.get("low_stock")
		if low_stock is not None:
			low_stock = low_stock.lower()
			if low_stock not in {"true", "false"}:
				raise ValidationError({"low_stock": ["Use 'true' or 'false'."]})
			low_stock_products = Q(stock_quantity__lte=F("low_stock_threshold"))
			queryset = queryset.filter(low_stock_products) if low_stock == "true" else queryset.exclude(low_stock_products)
		return queryset

	@extend_schema(
		parameters=[
			OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
			OpenApiParameter("category", OpenApiTypes.INT, OpenApiParameter.QUERY),
			OpenApiParameter("low_stock", OpenApiTypes.BOOL, OpenApiParameter.QUERY),
			OpenApiParameter("active", OpenApiTypes.BOOL, OpenApiParameter.QUERY),
			OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY),
		],
		responses={200: InventoryProductSerializer(many=True)},
		description="Paginated current stock for the active organization. Read access for all active members.",
	)
	def get(self, request, *args, **kwargs):
		return super().get(request, *args, **kwargs)


class StockMutationView(OrganizationContextMixin, APIView):
	permission_classes = [
		permissions.IsAuthenticated,
		IsOrganizationMember,
		IsOwnerOrManager,
	]
	serializer_class = None
	movement_type = None

	def post(self, request, *args, **kwargs):
		serializer = self.serializer_class(
			data=request.data,
			context={"request": request},
		)
		serializer.is_valid(raise_exception=True)
		product = serializer.validated_data["product_id"]
		quantity = serializer.validated_data["quantity"]
		movement_type = self.movement_type
		if movement_type == StockMovement.MovementType.ADJUSTMENT_IN and quantity < 0:
			movement_type = StockMovement.MovementType.ADJUSTMENT_OUT
		elif movement_type == StockMovement.MovementType.ADJUSTMENT_IN:
			movement_type = StockMovement.MovementType.ADJUSTMENT_IN
		movement = create_stock_movement(
			organization=request.organization,
			product_id=product.pk,
			actor=request.user,
			quantity=quantity,
			reason=serializer.validated_data["reason"],
			movement_type=movement_type,
		)
		return Response(
			StockMovementSerializer(movement, context={"request": request}).data,
			status=status.HTTP_201_CREATED,
		)


class StockInView(StockMutationView):
	serializer_class = StockInSerializer
	movement_type = StockMovement.MovementType.STOCK_IN

	@extend_schema(
		request=StockInSerializer,
		responses={201: StockMovementSerializer},
		description="Owner and Manager only. Atomically add stock and record an immutable movement.",
	)
	def post(self, request, *args, **kwargs):
		return super().post(request, *args, **kwargs)


class StockAdjustmentView(StockMutationView):
	serializer_class = StockAdjustmentSerializer
	movement_type = StockMovement.MovementType.ADJUSTMENT_IN

	@extend_schema(
		request=StockAdjustmentSerializer,
		responses={201: StockMovementSerializer},
		description="Owner and Manager only. Apply a signed adjustment atomically; negative stock is rejected.",
	)
	def post(self, request, *args, **kwargs):
		return super().post(request, *args, **kwargs)


class StockMovementListView(OrganizationContextMixin, generics.ListAPIView):
	permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
	serializer_class = StockMovementSerializer

	def get_queryset(self):
		organization = getattr(self.request, "organization", None)
		if organization is None:
			return StockMovement.objects.none()
		queryset = StockMovement.objects.filter(organization=organization).select_related(
			"product", "created_by"
		)
		product = self.request.query_params.get("product")
		if product is not None:
			try:
				product_id = int(product)
			except ValueError as exc:
				raise ValidationError({"product": ["A valid product ID is required."]}) from exc
			queryset = queryset.filter(product_id=product_id)
		movement_type = self.request.query_params.get("movement_type")
		if movement_type:
			movement_type = movement_type.lower()
			valid_types = StockMovement.MovementType.values
			if movement_type not in valid_types:
				raise ValidationError({"movement_type": ["Select a valid movement type."]})
			queryset = queryset.filter(movement_type=movement_type)
		actor = self.request.query_params.get("created_by") or self.request.query_params.get("actor")
		if actor is not None:
			try:
				actor_id = int(actor)
			except ValueError as exc:
				raise ValidationError({"created_by": ["A valid user ID is required."]}) from exc
			queryset = queryset.filter(created_by_id=actor_id)
		search = self.request.query_params.get("search")
		if search:
			search = search.strip()
			queryset = queryset.filter(
				Q(product__name__icontains=search)
				| Q(product__sku__icontains=search)
				| Q(reason__icontains=search)
			)
		for parameter, lookup in (("from", "gte"), ("to", "lte")):
			value = self.request.query_params.get(parameter)
			if value is None:
				continue
			queryset = filter_movement_date(
				queryset,
				parameter,
				lookup,
				value=value,
			)
		return queryset

	@extend_schema(
		parameters=[
			OpenApiParameter("product", OpenApiTypes.INT, OpenApiParameter.QUERY),
			OpenApiParameter("movement_type", OpenApiTypes.STR, OpenApiParameter.QUERY),
			OpenApiParameter("created_by", OpenApiTypes.INT, OpenApiParameter.QUERY),
			OpenApiParameter("from", OpenApiTypes.STR, OpenApiParameter.QUERY),
			OpenApiParameter("to", OpenApiTypes.STR, OpenApiParameter.QUERY),
			OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
			OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY),
		],
		responses={200: StockMovementSerializer(many=True)},
		description="Paginated immutable movement history for the active organization; readable by all active members.",
	)
	def get(self, request, *args, **kwargs):
		return super().get(request, *args, **kwargs)
