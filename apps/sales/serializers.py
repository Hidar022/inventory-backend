from decimal import Decimal

from django.db.models import Sum
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.catalog.models import Product
from apps.sales.models import Payment, Sale, SaleItem


class SaleItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaleItem
        fields = [
            "id",
            "product",
            "product_name",
            "product_sku",
            "quantity",
            "unit_price",
            "line_total",
            "created_at",
        ]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id",
            "method",
            "amount",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


class SaleSerializer(serializers.ModelSerializer):
    cashier_email = serializers.EmailField(source="cashier.email", read_only=True)
    items = SaleItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    amount_paid = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = [
            "id",
            "receipt_number",
            "status",
            "subtotal",
            "discount",
            "total",
            "amount_paid",
            "payment_status",
            "cashier",
            "cashier_email",
            "idempotency_key",
            "items",
            "payments",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.DECIMAL)
    def get_amount_paid(self, obj):
        return obj.payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


class CheckoutItemSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(
        min_value=1,
        error_messages={"min_value": "Quantity must be greater than zero."},
    )


class CheckoutPaymentSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=Payment.Method.choices)
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.00"),
    )


class CheckoutSerializer(serializers.Serializer):
    idempotency_key = serializers.CharField(max_length=255, trim_whitespace=True)
    items = CheckoutItemSerializer(many=True)
    discount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.00"),
        default=Decimal("0.00"),
    )
    payments = CheckoutPaymentSerializer(many=True, required=False, default=list)

    def validate_idempotency_key(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("An idempotency key is required.")
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        organization = getattr(self.context.get("request"), "organization", None)
        normalized = []
        seen = {}
        for item in value:
            product_id = int(item["product_id"])
            quantity = int(item["quantity"])
            seen[product_id] = seen.get(product_id, 0) + quantity

        for product_id, quantity in sorted(seen.items()):
            product = Product.objects.filter(pk=product_id, organization=organization).first()
            if product is None:
                raise serializers.ValidationError(
                    {"items": [f"Product {product_id} is unavailable in this organization."]}
                )
            normalized.append({"product_id": product, "quantity": quantity})
        return normalized

    def validate_discount(self, value):
        request = self.context.get("request")
        if getattr(request, "role", None) == "cashier" and value > Decimal("0.00"):
            raise PermissionDenied("Cashiers cannot apply discounts.")
        return value

    def validate(self, attrs):
        items = attrs.get("items", [])
        if not items:
            raise serializers.ValidationError({"items": ["At least one item is required."]})

        subtotal = sum(
            (
                item["product_id"].selling_price * Decimal(str(item["quantity"]))
                for item in items
            ),
            Decimal("0.00"),
        )
        discount = attrs.get("discount", Decimal("0.00"))
        if discount > subtotal:
            raise serializers.ValidationError({"discount": ["Discount cannot exceed the subtotal."]})

        total = subtotal - discount
        if total < 0:
            raise serializers.ValidationError({"discount": ["Discount cannot make the total negative."]})

        if total == 0:
            payment_total = sum(
                (payment["amount"] for payment in attrs.get("payments", [])),
                Decimal("0.00"),
            )
            if payment_total > Decimal("0.00"):
                raise serializers.ValidationError(
                    {"payments": ["Zero-total sales should not accept positive payments."]}
                )
            return attrs

        payments = attrs.get("payments") or []
        if not payments:
            raise serializers.ValidationError({"payments": ["At least one payment is required."]})

        payment_total = sum((payment["amount"] for payment in payments), Decimal("0.00"))
        if payment_total != total:
            raise serializers.ValidationError({"payments": ["Payment total must match the sale total."]})

        return attrs
