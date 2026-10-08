from decimal import Decimal

from rest_framework import serializers

from apps.dashboard.models import ActivityEvent
from apps.sales.serializers import SaleSerializer


class DashboardSerializer(serializers.Serializer):
    sales_today = serializers.DecimalField(max_digits=14, decimal_places=2)
    sales_count_today = serializers.IntegerField()
    period_sales_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    period_sales_count = serializers.IntegerField()
    active_products = serializers.IntegerField()
    low_stock_products = serializers.IntegerField()
    out_of_stock_products = serializers.IntegerField()
    recent_sales = SaleSerializer(many=True)

    def to_representation(self, instance):
        instance.setdefault("sales_today", Decimal("0.00"))
        instance.setdefault("sales_count_today", 0)
        instance.setdefault("period_sales_total", Decimal("0.00"))
        instance.setdefault("period_sales_count", 0)
        instance.setdefault("active_products", 0)
        instance.setdefault("low_stock_products", 0)
        instance.setdefault("out_of_stock_products", 0)
        instance.setdefault("recent_sales", [])
        return super().to_representation(instance)


class ActivityEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    actor_email = serializers.SerializerMethodField()
    organization = serializers.SerializerMethodField()

    class Meta:
        model = ActivityEvent
        fields = (
            "id",
            "organization",
            "actor",
            "actor_name",
            "actor_email",
            "action",
            "entity_type",
            "entity_id",
            "description",
            "metadata",
            "created_at",
        )

    def get_actor_name(self, obj):
        if obj.actor.first_name or obj.actor.last_name:
            return f"{obj.actor.first_name} {obj.actor.last_name}".strip()
        return obj.actor.email

    def get_actor_email(self, obj):
        return obj.actor.email

    def get_organization(self, obj):
        return str(obj.organization_id)


class DashboardReportSerializer(serializers.Serializer):
    sales_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    sales_count = serializers.IntegerField()
    purchase_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    expense_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    month_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    month_count = serializers.IntegerField()
    cash_summary = serializers.DictField()
    stock_summary = serializers.DictField()
    top_products = serializers.ListField(child=serializers.DictField())
    sales_summary = serializers.DictField()
    products_summary = serializers.DictField()
    inventory_summary = serializers.DictField()
    returns_summary = serializers.DictField()
    purchasing_summary = serializers.DictField()
    expenses_summary = serializers.DictField()
    currency = serializers.CharField()
    date_range = serializers.DictField()

    @staticmethod
    def _format_money(value):
        if isinstance(value, Decimal):
            return format(value.quantize(Decimal("0.01")), "f")
        return value

    def to_representation(self, instance):
        instance.setdefault("sales_total", Decimal("0.00"))
        instance.setdefault("sales_count", 0)
        instance.setdefault("purchase_total", Decimal("0.00"))
        instance.setdefault("expense_total", Decimal("0.00"))
        instance.setdefault("month_total", Decimal("0.00"))
        instance.setdefault("month_count", 0)
        instance.setdefault("cash_summary", {})
        instance.setdefault("stock_summary", {"low_stock": 0, "out_of_stock": 0})
        instance.setdefault("top_products", [])
        instance.setdefault("sales_summary", {})
        instance.setdefault("products_summary", {})
        instance.setdefault("inventory_summary", {})
        instance.setdefault("returns_summary", {})
        instance.setdefault("purchasing_summary", {})
        instance.setdefault("expenses_summary", {})
        instance.setdefault("currency", "NGN")
        instance.setdefault("date_range", {"from": None, "to": None})

        data = super().to_representation(instance)
        return self._normalize_values(data)

    def _normalize_values(self, value):
        if isinstance(value, Decimal):
            return self._format_money(value)
        if isinstance(value, dict):
            return {key: self._normalize_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._normalize_values(item) for item in value]
        return value