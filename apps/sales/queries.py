from datetime import datetime, time, timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.sales.models import Sale, SaleItem


def visible_sales_queryset(organization, role, user):
    queryset = Sale.objects.filter(organization=organization)
    if role == "cashier":
        queryset = queryset.filter(cashier=user)
    return queryset


def completed_paid_sales(queryset):
    return queryset.filter(
        status__in=[
            Sale.Status.COMPLETED,
            Sale.Status.PARTIALLY_RETURNED,
            Sale.Status.FULLY_RETURNED,
        ],
        payment_status=Sale.PaymentStatus.PAID,
    )


def local_day_bounds(day):
    local_timezone = timezone.get_default_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), local_timezone)
    next_day_start = timezone.make_aware(
        datetime.combine(day + timedelta(days=1), time.min),
        local_timezone,
    )
    return start, next_day_start


def apply_sales_filters(queryset, filters):
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if date_from:
        start, _ = local_day_bounds(date_from)
        queryset = queryset.filter(created_at__gte=start)
    if date_to:
        _, end = local_day_bounds(date_to)
        queryset = queryset.filter(created_at__lt=end)

    search = filters.get("search", "").strip()
    if search:
        matching_item = SaleItem.objects.filter(sale_id=OuterRef("pk")).filter(
            Q(product_name__icontains=search) | Q(product_sku__icontains=search)
        )
        queryset = queryset.annotate(has_matching_item=Exists(matching_item)).filter(
            Q(receipt_number__icontains=search)
            | Q(cashier__email__icontains=search)
            | Q(cashier__first_name__icontains=search)
            | Q(cashier__last_name__icontains=search)
            | Q(has_matching_item=True)
        )

    if filters.get("payment_method"):
        queryset = queryset.filter(payments__method=filters["payment_method"])
    if filters.get("payment_status"):
        queryset = queryset.filter(payment_status=filters["payment_status"])
    if filters.get("cashier"):
        queryset = queryset.filter(cashier_id=filters["cashier"])
    return queryset.distinct()