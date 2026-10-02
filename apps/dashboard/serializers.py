from decimal import Decimal

from rest_framework import serializers

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