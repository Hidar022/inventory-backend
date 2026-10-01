from rest_framework import serializers

from apps.catalog.models import Category, Product


def normalize_name(value):
    return " ".join(value.split())


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "is_active", "created_at", "updated_at"]

    def validate_name(self, value):
        name = normalize_name(value)
        if not name:
            raise serializers.ValidationError("Category name is required.")
        organization = getattr(self.context["request"], "organization", None)
        categories = Category.objects.filter(organization=organization, name__iexact=name)
        if self.instance:
            categories = categories.exclude(pk=self.instance.pk)
        if categories.exists():
            raise serializers.ValidationError(
                "A category with this name already exists in this organization."
            )
        return name


class ProductSerializer(serializers.ModelSerializer):
    category = serializers.CharField(source="category.name", read_only=True, allow_null=True)
    category_id = serializers.PrimaryKeyRelatedField(
        source="category",
        queryset=Category.objects.all(),
        required=False,
        allow_null=True,
        write_only=True,
        error_messages={
            "does_not_exist": "Invalid category for this organization.",
        },
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "sku",
            "category",
            "category_id",
            "unit",
            "selling_price",
            "cost_price",
            "stock_quantity",
            "low_stock_threshold",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "stock_quantity",
            "is_active",
            "created_at",
            "updated_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        organization = getattr(request, "organization", None)
        if organization is None:
            self.fields["category_id"].queryset = Category.objects.none()
        else:
            self.fields["category_id"].queryset = Category.objects.filter(
                organization=organization
            )
        if request and getattr(request, "role", None) == "cashier":
            self.fields.pop("cost_price")

    def validate_name(self, value):
        name = normalize_name(value)
        if not name:
            raise serializers.ValidationError("Product name is required.")
        return name

    def validate_sku(self, value):
        sku = value.strip().upper()
        if not sku:
            raise serializers.ValidationError("SKU is required.")
        organization = getattr(self.context["request"], "organization", None)
        products = Product.objects.filter(organization=organization, sku__iexact=sku)
        if self.instance:
            products = products.exclude(pk=self.instance.pk)
        if products.exists():
            raise serializers.ValidationError(
                "A product with this SKU already exists in this organization."
            )
        return sku

    def validate_category_id(self, category):
        if category is None:
            return category
        organization = getattr(self.context["request"], "organization", None)
        if category.organization_id != getattr(organization, "pk", None):
            raise serializers.ValidationError("Category does not belong to your organization.")
        current_category = getattr(self.instance, "category", None)
        if not category.is_active and (
            self.instance is None or current_category != category
        ):
            raise serializers.ValidationError("Inactive categories cannot be assigned to products.")
        return category

    def validate(self, attrs):
        for field in ("selling_price", "cost_price"):
            value = attrs.get(field)
            if value is not None and value < 0:
                raise serializers.ValidationError({field: "Price cannot be negative."})
        return attrs