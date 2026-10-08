from decimal import Decimal

from django.db.models import Count, DecimalField, F, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.catalog.models import Product
from apps.dashboard.models import ActivityEvent
from apps.expenses.models import Expense
from apps.inventory.models import StockMovement
from apps.purchases.models import Purchase
from apps.sales.models import Payment, Sale, SaleItem, SaleReturn
from apps.sales.queries import completed_paid_sales, local_day_bounds, visible_sales_queryset

MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY_FIELD)
SENSITIVE_METADATA_PARTS = (
    "password",
    "token",
    "secret",
    "authorization",
    "credential",
    "api_key",
    "private_key",
    "jwt",
)


def _sanitize_activity_metadata(value):
    if isinstance(value, dict):
        return {
            key: _sanitize_activity_metadata(item)
            for key, item in value.items()
            if not any(part in key.lower() for part in SENSITIVE_METADATA_PARTS)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_activity_metadata(item) for item in value]
    return value


def log_activity_event(*, organization, actor, action, entity_type, entity_id, description, metadata=None):
    return ActivityEvent.objects.create(
        organization=organization,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id or ""),
        description=description,
        metadata=_sanitize_activity_metadata(metadata or {}),
    )


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
            ZERO,
            output_field=MONEY_FIELD,
        ),
        sales_count_today=Count("id", filter=today_window),
        period_sales_total=Coalesce(
            Sum("total", filter=month_window),
            ZERO,
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


def get_reports_summary(organization, date_from, date_to):
    start, _ = local_day_bounds(date_from)
    _, end = local_day_bounds(date_to)
    today = timezone.localdate()
    sales_queryset = completed_paid_sales(
        visible_sales_queryset(organization, "owner", None)
    ).filter(created_at__gte=start, created_at__lt=end)
    sales_aggregate = sales_queryset.aggregate(
        total=Coalesce(Sum("total"), ZERO, output_field=MONEY_FIELD),
        count=Count("id"),
    )
    sales_by_day = (
        sales_queryset.annotate(day=TruncDate("created_at", tzinfo=timezone.get_default_timezone()))
        .values("day")
        .annotate(total=Coalesce(Sum("total"), ZERO, output_field=MONEY_FIELD), count=Count("id"))
        .order_by("day")
    )
    month_start, _ = local_day_bounds(today.replace(day=1))
    _, month_end = local_day_bounds(today)
    month_sales = completed_paid_sales(
        visible_sales_queryset(organization, "owner", None)
    ).filter(created_at__gte=month_start, created_at__lt=month_end)
    month_aggregate = month_sales.aggregate(
        total=Coalesce(Sum("total"), ZERO, output_field=MONEY_FIELD),
        count=Count("id"),
    )

    payments = Payment.objects.filter(
        organization=organization,
        created_at__gte=start,
        created_at__lt=end,
        sale__organization=organization,
        sale__status__in=[
            Sale.Status.COMPLETED,
            Sale.Status.PARTIALLY_RETURNED,
            Sale.Status.FULLY_RETURNED,
        ],
        sale__payment_status=Sale.PaymentStatus.PAID,
    )
    payment_methods = {
        row["method"]: row["total"]
        for row in payments.values("method").annotate(
            total=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD)
        )
    }

    returns = SaleReturn.objects.filter(
        organization=organization,
        status=SaleReturn.Status.COMPLETED,
        created_at__gte=start,
        created_at__lt=end,
    )
    return_aggregate = returns.aggregate(
        total=Coalesce(Sum("refund_amount"), ZERO, output_field=MONEY_FIELD),
        count=Count("id"),
    )
    return_methods = {
        row["refund_method"]: row["total"]
        for row in returns.values("refund_method").annotate(
            total=Coalesce(Sum("refund_amount"), ZERO, output_field=MONEY_FIELD)
        )
    }

    expenses = Expense.objects.filter(
        organization=organization,
        expense_date__gte=date_from,
        expense_date__lte=date_to,
    )
    expense_aggregate = expenses.aggregate(
        total=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD),
        count=Count("id"),
    )
    expenses_by_category = expenses.values("category__name").annotate(
        total=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD),
        count=Count("id"),
    ).order_by("category__name")

    purchases = Purchase.objects.filter(
        organization=organization,
        created_at__gte=start,
        created_at__lt=end,
    )
    received_purchases = Purchase.objects.filter(
        organization=organization,
        status=Purchase.Status.RECEIVED,
        received_at__gte=start,
        received_at__lt=end,
    )
    purchase_aggregate = received_purchases.aggregate(
        total=Coalesce(Sum("total"), ZERO, output_field=MONEY_FIELD),
        count=Count("id"),
    )

    products = Product.objects.filter(organization=organization, is_active=True)
    stock_summary = {
        "low_stock": products.filter(stock_quantity__lte=F("low_stock_threshold")).count(),
        "out_of_stock": products.filter(stock_quantity=0).count(),
        "units_on_hand": products.aggregate(total=Coalesce(Sum("stock_quantity"), 0))["total"],
    }
    movements = StockMovement.objects.filter(
        organization=organization,
        created_at__gte=start,
        created_at__lt=end,
    )
    movement_summary = {
        "count": movements.count(),
        "stock_in_units": movements.filter(
            movement_type__in=[
                StockMovement.MovementType.STOCK_IN,
                StockMovement.MovementType.ADJUSTMENT_IN,
                StockMovement.MovementType.SALE_RETURN,
            ]
        ).aggregate(total=Coalesce(Sum("quantity"), 0))["total"],
        "adjustment_out_units": -movements.filter(
            movement_type=StockMovement.MovementType.ADJUSTMENT_OUT
        ).aggregate(total=Coalesce(Sum("quantity"), 0))["total"],
    }

    top_products = (
        SaleItem.objects.filter(sale__in=sales_queryset)
        .values("product_id", "product_name", "product_sku")
        .annotate(
            quantity_sold=Sum("quantity"),
            revenue=Coalesce(Sum("line_total"), ZERO, output_field=MONEY_FIELD),
        )
        .order_by("-revenue", "-quantity_sold")[:5]
    )
    top_products_data = [
        {
            "id": str(item["product_id"]),
            "name": item["product_name"],
            "sku": item["product_sku"],
            "quantity_sold": item["quantity_sold"],
            "revenue": item["revenue"],
        }
        for item in top_products
    ]

    cash_sales = payment_methods.get(Payment.Method.CASH, Decimal("0.00"))
    cash_expenses = expenses.filter(payment_method=Payment.Method.CASH).aggregate(
        total=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD)
    )["total"]
    cash_refunds = return_methods.get(Payment.Method.CASH, Decimal("0.00"))
    cash_out = cash_expenses + cash_refunds
    date_range = {"from": date_from.isoformat(), "to": date_to.isoformat()}

    return {
        "sales_total": sales_aggregate["total"],
        "sales_count": sales_aggregate["count"],
        "purchase_total": purchase_aggregate["total"],
        "expense_total": expense_aggregate["total"],
        "month_total": month_aggregate["total"],
        "month_count": month_aggregate["count"],
        "cash_summary": {
            "cash_sales": cash_sales,
            "cash_expenses": cash_expenses,
            "cash_refunds": cash_refunds,
            "cash_in": cash_sales,
            "cash_out": cash_out,
            "net_cash_change": cash_sales - cash_out,
        },
        "stock_summary": stock_summary,
        "top_products": top_products_data,
        "sales_summary": {
            "by_payment_method": payment_methods,
            "by_day": list(sales_by_day),
        },
        "products_summary": {
            "active_products": products.count(),
            "active_categories": organization.categories.filter(is_active=True).count(),
            "top_products": top_products_data,
        },
        "inventory_summary": {**stock_summary, "movements": movement_summary},
        "returns_summary": {
            "count": return_aggregate["count"],
            "refund_total": return_aggregate["total"],
            "by_method": return_methods,
        },
        "purchasing_summary": {
            "received_total": purchase_aggregate["total"],
            "received_count": purchase_aggregate["count"],
            "created_count": purchases.count(),
            "draft_count": purchases.filter(status=Purchase.Status.DRAFT).count(),
            "cancelled_count": purchases.filter(status=Purchase.Status.CANCELLED).count(),
        },
        "expenses_summary": {
            "total": expense_aggregate["total"],
            "count": expense_aggregate["count"],
            "by_category": list(expenses_by_category),
        },
        "currency": organization.currency,
        "date_range": date_range,
    }


def get_activity_feed(organization, search="", action=None, date_from=None, date_to=None):
    queryset = ActivityEvent.objects.filter(organization=organization).select_related("actor")
    if action:
        queryset = queryset.filter(action=action)
    if search:
        queryset = queryset.filter(
            Q(description__icontains=search)
            | Q(action__icontains=search)
            | Q(entity_type__icontains=search)
            | Q(actor__email__icontains=search)
        )
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)
    return queryset.order_by("-created_at", "-id")