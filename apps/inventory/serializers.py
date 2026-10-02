from rest_framework import serializers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field

from apps.catalog.models import Product
from apps.inventory.models import StockMovement


class InventoryProductSerializer(serializers.ModelSerializer):
	product_id = serializers.IntegerField(source="pk", read_only=True)
	category = serializers.CharField(source="category.name", read_only=True, allow_null=True)
	quantity = serializers.IntegerField(source="stock_quantity", read_only=True)

	class Meta:
		model = Product
		fields = [
			"id",
			"product_id",
			"name",
			"sku",
			"category",
			"unit",
			"quantity",
			"stock_quantity",
			"low_stock_threshold",
			"is_active",
		]
		read_only_fields = fields


class StockMovementSerializer(serializers.ModelSerializer):
	product_id = serializers.ReadOnlyField()
	product_name = serializers.CharField(source="product.name", read_only=True)
	sku = serializers.CharField(source="product.sku", read_only=True)
	created_by_id = serializers.ReadOnlyField()
	quantity_delta = serializers.IntegerField(source="quantity", read_only=True)
	type = serializers.SerializerMethodField()
	reference = serializers.SerializerMethodField()
	created_by_display = serializers.SerializerMethodField()

	class Meta:
		model = StockMovement
		fields = [
			"id",
			"product",
			"product_id",
			"product_name",
			"sku",
			"movement_type",
			"type",
			"quantity",
			"quantity_delta",
			"previous_quantity",
			"new_quantity",
			"reason",
			"reference_type",
			"reference_id",
			"reference",
			"created_by",
			"created_by_id",
			"created_by_display",
			"created_at",
		]
		read_only_fields = fields

	@extend_schema_field(OpenApiTypes.STR)
	def get_type(self, obj):
		if obj.movement_type == StockMovement.MovementType.STOCK_IN:
			return "stock_in"
		return "adjustment"

	@extend_schema_field(serializers.CharField(allow_null=True))
	def get_reference(self, obj):
		if obj.reference_type and obj.reference_id:
			return f"{obj.reference_type}:{obj.reference_id}"
		return None

	@extend_schema_field(OpenApiTypes.STR)
	def get_created_by_display(self, obj):
		return obj.created_by.get_full_name().strip() or obj.created_by.email


class StockMutationSerializer(serializers.Serializer):
	product_id = serializers.PrimaryKeyRelatedField(
		queryset=Product.objects.none(),
		error_messages={"does_not_exist": "Invalid product for this organization."},
	)
	quantity = serializers.IntegerField()
	reason = serializers.CharField(max_length=500, trim_whitespace=True)

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		request = self.context.get("request")
		organization = getattr(request, "organization", None)
		if organization is not None:
			self.fields["product_id"].queryset = Product.objects.filter(
				organization=organization
			)

	def validate_reason(self, value):
		if not value:
			raise serializers.ValidationError("Reason is required.")
		return value


class StockInSerializer(StockMutationSerializer):
	quantity = serializers.IntegerField(
		min_value=1,
		error_messages={"min_value": "Quantity must be greater than zero."},
	)


class StockAdjustmentSerializer(StockMutationSerializer):
	def validate_quantity(self, value):
		if value == 0:
			raise serializers.ValidationError("Quantity cannot be zero.")
		return value