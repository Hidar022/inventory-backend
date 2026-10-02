import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.catalog.models import Product
from apps.organizations.models import Organization


class Sale(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        PARTIALLY_RETURNED = "PARTIALLY_RETURNED", "Partially returned"
        FULLY_RETURNED = "FULLY_RETURNED", "Fully returned"

    class PaymentStatus(models.TextChoices):
        PAID = "PAID", "Paid"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        UNPAID = "UNPAID", "Unpaid"
        REFUNDED = "REFUNDED", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="sales",
    )
    receipt_number = models.CharField(max_length=64)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sales_created",
    )
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=28, choices=Status.choices, default=Status.COMPLETED)
    payment_status = models.CharField(
        max_length=28,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PAID,
    )
    idempotency_key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                name="unique_sale_idempotency_key_per_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "receipt_number"],
                name="unique_sale_receipt_number_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(subtotal__gte=0) & Q(discount__gte=0) & Q(total__gte=0),
                name="sale_nonnegative_totals",
            ),
        ]

    def __str__(self):
        return f"{self.receipt_number} - {self.organization.name}"


class SaleItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="sale_items",
    )
    product_name = models.CharField(max_length=200)
    product_sku = models.CharField(max_length=100)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sale_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(line_total__gte=0) & Q(unit_price__gte=0),
                name="sale_item_nonnegative_amounts",
            ),
        ]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        TRANSFER = "transfer", "Transfer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="payment_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(method__in=["cash", "transfer"]),
                name="valid_payment_method",
            ),
        ]

    def __str__(self):
        return f"{self.method} {self.amount}"


class SaleReturn(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="sale_returns",
    )
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="returns",
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sale_returns_processed",
    )
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.COMPLETED)
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2)
    refund_method = models.CharField(max_length=20, choices=Payment.Method.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(refund_amount__gte=0),
                name="sale_return_refund_nonnegative",
            ),
        ]

    def __str__(self):
        return f"Return for {self.sale.receipt_number}"


class SaleReturnItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale_return = models.ForeignKey(
        SaleReturn,
        on_delete=models.PROTECT,
        related_name="items",
    )
    sale_item = models.ForeignKey(
        SaleItem,
        on_delete=models.PROTECT,
        related_name="return_items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="sale_return_items",
    )
    product_name = models.CharField(max_length=200)
    product_sku = models.CharField(max_length=100)
    quantity_returned = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity_returned__gt=0),
                name="sale_return_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0) & Q(refund_amount__gte=0),
                name="sale_return_item_amounts_nonnegative",
            ),
        ]

    def __str__(self):
        return f"{self.product_name} returned x {self.quantity_returned}"
