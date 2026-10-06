from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from apps.catalog.models import Product
from apps.purchases.models import Purchase, PurchaseItem, Supplier


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            "id",
            "name",
            "phone",
            "email",
            "address",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "organization", "created_at", "updated_at"]


class PurchaseItemSerializer(serializers.ModelSerializer):
    product = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = PurchaseItem
        fields = [
            "id",
            "product",
            "product_name",
            "product_sku",
            "quantity",
            "unit_cost",
            "line_total",
            "created_at",
        ]
        read_only_fields = fields


class PurchaseItemWriteSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.IntegerField(min_value=1, error_messages={"min_value": "Quantity must be greater than zero."})
    unit_cost = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.00"),
    )

    def validate(self, attrs):
        request = self.context.get("request")
        organization = getattr(request, "organization", None)
        product = attrs["product"]
        if organization is not None and product.organization_id != organization.pk:
            raise serializers.ValidationError({"product": ["This product does not belong to the active organization."]})
        if not product.is_active:
            raise serializers.ValidationError({"product": ["Inactive products cannot be added to a purchase."]})
        return attrs


class PurchaseSerializer(serializers.ModelSerializer):
    supplier = SupplierSerializer(read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    items = PurchaseItemSerializer(many=True, read_only=True)

    class Meta:
        model = Purchase
        fields = [
            "id",
            "organization",
            "supplier",
            "reference_number",
            "status",
            "subtotal",
            "discount",
            "total",
            "notes",
            "created_by",
            "created_by_email",
            "created_by_name",
            "received_at",
            "created_at",
            "updated_at",
            "items",
        ]
        read_only_fields = fields

    def get_created_by_name(self, obj):
        if not obj.created_by:
            return ""
        return " ".join(part for part in [obj.created_by.first_name, obj.created_by.last_name] if part).strip() or obj.created_by.email


class PurchaseCreateSerializer(serializers.Serializer):
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all())
    reference_number = serializers.CharField(max_length=64, required=False, allow_blank=True)
    discount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.00"),
        default=Decimal("0.00"),
    )
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    items = PurchaseItemWriteSerializer(many=True)

    def validate_supplier(self, value):
        request = self.context.get("request")
        organization = getattr(request, "organization", None)
        if organization is None:
            raise serializers.ValidationError("You do not have an active organization.")
        if value.organization_id != organization.pk:
            raise serializers.ValidationError("This supplier does not belong to the active organization.")
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        seen = set()
        for item in value:
            product = item["product"]
            product_id = product.pk
            if product_id in seen:
                raise serializers.ValidationError("Duplicate products are not allowed in the same purchase.")
            seen.add(product_id)
        return value

    def validate(self, attrs):
        from apps.purchases.services import calculate_purchase_totals

        items = attrs.get("items", [])
        subtotal, discount, total = calculate_purchase_totals(
            [{"quantity": item["quantity"], "unit_cost": item["unit_cost"]} for item in items],
            attrs.get("discount", Decimal("0.00")),
        )
        attrs["subtotal"] = subtotal
        attrs["discount"] = discount
        attrs["total"] = total
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get("request")
        organization = getattr(request, "organization", None)
        item_data = validated_data.pop("items")
        discount = validated_data.pop("discount")
        subtotal = validated_data.pop("subtotal")
        total = validated_data.pop("total")
        purchase = Purchase.objects.create(
            organization=organization,
            supplier=validated_data.pop("supplier"),
            reference_number=validated_data.get("reference_number", "").strip(),
            status=Purchase.Status.DRAFT,
            subtotal=subtotal,
            discount=discount,
            total=total,
            notes=validated_data.get("notes", ""),
            created_by=request.user,
        )
        purchase_items = []
        for item in item_data:
            product = item["product"]
            quantity = int(item["quantity"])
            unit_cost = Decimal(str(item["unit_cost"]))
            line_total = Decimal(quantity) * unit_cost
            purchase_items.append(
                PurchaseItem(
                    purchase=purchase,
                    product=product,
                    product_name=product.name,
                    product_sku=product.sku,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    line_total=line_total,
                )
            )
        PurchaseItem.objects.bulk_create(purchase_items)
        return purchase


class PurchaseUpdateSerializer(serializers.ModelSerializer):
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all(), required=False)
    reference_number = serializers.CharField(max_length=64, required=False, allow_blank=True)
    discount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False)
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    items = PurchaseItemWriteSerializer(many=True, required=False)

    class Meta:
        model = Purchase
        fields = [
            "supplier",
            "reference_number",
            "discount",
            "notes",
            "items",
        ]

    def validate(self, attrs):
        purchase = self.instance
        if purchase is None or purchase.status != Purchase.Status.DRAFT:
            raise serializers.ValidationError("Only DRAFT purchases can be edited.")

        item_values = attrs.get("items")
        if item_values is not None:
            if not item_values:
                raise serializers.ValidationError({"items": ["At least one item is required."]})
            seen = set()
            for item in item_values:
                product_id = item["product"].pk
                if product_id in seen:
                    raise serializers.ValidationError({"items": ["Duplicate products are not allowed in the same purchase."]})
                seen.add(product_id)
                if item["product"].organization_id != purchase.organization_id:
                    raise serializers.ValidationError({"items": ["This product does not belong to the active organization."]})
                if not item["product"].is_active:
                    raise serializers.ValidationError({"items": ["Inactive products cannot be added to a purchase."]})

        discount = attrs.get("discount", purchase.discount)
        item_list = item_values if item_values is not None else [
            {"quantity": item.quantity, "unit_cost": item.unit_cost}
            for item in purchase.items.all()
        ]
        subtotal = sum(
            (Decimal(str(item["quantity"])) * Decimal(str(item["unit_cost"])) for item in item_list),
            Decimal("0.00"),
        )
        if discount > subtotal:
            raise serializers.ValidationError({"discount": ["Discount cannot exceed the subtotal."]})
        total = subtotal - discount
        if total < 0:
            raise serializers.ValidationError({"total": ["Purchase total cannot be negative."]})
        attrs["_subtotal"] = subtotal
        attrs["_total"] = total
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        if "supplier" in validated_data:
            supplier = validated_data.pop("supplier")
            if supplier.organization_id != instance.organization_id:
                raise serializers.ValidationError({"supplier": ["This supplier does not belong to the active organization."]})
            instance.supplier = supplier
        if "reference_number" in validated_data:
            instance.reference_number = validated_data.pop("reference_number").strip()
        if "discount" in validated_data:
            instance.discount = validated_data.pop("discount")
        if "notes" in validated_data:
            instance.notes = validated_data.pop("notes")
        if "items" in validated_data:
            items = validated_data.pop("items")
            instance.items.all().delete()
            purchase_items = []
            for item in items:
                product = item["product"]
                quantity = int(item["quantity"])
                unit_cost = Decimal(str(item["unit_cost"]))
                purchase_items.append(
                    PurchaseItem(
                        purchase=instance,
                        product=product,
                        product_name=product.name,
                        product_sku=product.sku,
                        quantity=quantity,
                        unit_cost=unit_cost,
                        line_total=Decimal(quantity) * unit_cost,
                    )
                )
            PurchaseItem.objects.bulk_create(purchase_items)

        subtotal = validated_data.pop("_subtotal", instance.subtotal)
        total = validated_data.pop("_total", instance.total)
        instance.subtotal = subtotal
        instance.total = total
        instance.save(update_fields=["supplier", "reference_number", "discount", "notes", "subtotal", "total", "updated_at"])
        return instance
