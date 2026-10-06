from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from apps.expenses.models import Expense
from apps.sales.models import Payment, Sale, SaleReturn
from apps.sales.queries import local_day_bounds

MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY_FIELD)


def _sum_amount(queryset, field_name="amount"):
    return queryset.aggregate(
        total=Coalesce(Sum(field_name), ZERO, output_field=MONEY_FIELD)
    )["total"]


def get_daily_cash_summary(organization, business_date: date):
    start, end = local_day_bounds(business_date)

    cash_sales = _sum_amount(
        Payment.objects.filter(
            organization=organization,
            method=Payment.Method.CASH,
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
    )
    cash_expenses = _sum_amount(
        Expense.objects.filter(
            organization=organization,
            payment_method=Payment.Method.CASH,
            expense_date=business_date,
        )
    )
    cash_refunds = _sum_amount(
        SaleReturn.objects.filter(
            organization=organization,
            status=SaleReturn.Status.COMPLETED,
            refund_method=Payment.Method.CASH,
            created_at__gte=start,
            created_at__lt=end,
        ),
        field_name="refund_amount",
    )
    cash_out = cash_expenses + cash_refunds

    return {
        "date": business_date,
        "cash_sales": cash_sales,
        "cash_expenses": cash_expenses,
        "cash_refunds": cash_refunds,
        "cash_in": cash_sales,
        "cash_out": cash_out,
        "net_cash_change": cash_sales - cash_out,
    }