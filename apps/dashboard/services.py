from decimal import Decimal

from django.db.models import Count, DecimalField, F, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.catalog.models import Product
from apps.sales.models import Payment, Sale, SaleItem
from apps.sales.queries import completed_paid_sales, local_day_bounds, visible_sales_queryset

MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)


def get_dashboard_metrics(organization, role, user):
    today = timezone.localdate()
    today_start, tomorrow_start = local_day_bounds(today)
    month_start, _ = local_day_bounds(today.replace(day=1))
    sales = completed_paid_sales(visible_sales_queryset(organization, role, user))
    today_window = Q(created_at__gte=today_start, created_at__lt=tomorrow_start)
    month_window = Q(created_at__gte=month_start, created_at__lt=tomorrow_start)
    sales_metrics = sales.aggregate(
        sales_today=Coalesce(
            Sum("total", filter=today_window),
            Value(Decimal("0.00")),
            output_field=MONEY_FIELD,
        ),
        sales_count_today=Count("id", filter=today_window),
        period_sales_total=Coalesce(
            Sum("total", filter=month_window),
            Value(Decimal("0.00")),
            output_field=MONEY_FIELD,
        ),
        period_sales_count=Count("id", filter=month_window),
    )

    products = Product.objects.filter(organization=organization, is_active=True)
    product_metrics = products.aggregate(
        active_products=Count("id"),
        low_stock_products=Count("id", filter=Q(stock_quantity__lte=F("low_stock_threshold"))),
        out_of_stock_products=Count("id", filter=Q(stock_quantity=0)),
    )

    recent_sales = sales.select_related("cashier", "organization").prefetch_related(
        Prefetch("items", queryset=SaleItem.objects.order_by("id")),
        Prefetch("payments", queryset=Payment.objects.order_by("id")),
    ).order_by("-created_at", "-id")[:5]

    return {**sales_metrics, **product_metrics, "recent_sales": recent_sales}