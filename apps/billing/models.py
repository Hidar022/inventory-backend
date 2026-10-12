import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.organizations.models import Organization


class SubscriptionPlan(models.Model):
    class Code(models.TextChoices):
        INITIAL_SETUP_6_MONTHS = "INITIAL_SETUP_6_MONTHS", "Initial setup + 6 months"
        MONTHLY = "MONTHLY", "Monthly"
        TWO_MONTHS = "TWO_MONTHS", "Two months"
        FIVE_MONTHS = "FIVE_MONTHS", "Five months"
        SEMI_ANNUAL = "SEMI_ANNUAL", "Semi-annual"
        ANNUAL = "ANNUAL", "Annual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True, default="")
    months = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default="NGN")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "months", "code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="subscription_plan_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(months__gt=0),
                name="subscription_plan_months_positive",
            ),
        ]

    def __str__(self):
        return f"{self.code} ({self.currency} {self.amount})"

    @property
    def amount_kobo(self):
        return int((self.amount * Decimal("100")).quantize(Decimal("1")))


class OrganizationSubscription(models.Model):
    class Status(models.TextChoices):
        PENDING_ACTIVATION = "PENDING_ACTIVATION", "Pending activation"
        ACTIVE = "ACTIVE", "Active"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="organization_subscriptions",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING_ACTIVATION,
    )
    is_setup = models.BooleanField(default=False)
    start_date = models.DateTimeField()
    expiry_date = models.DateTimeField()
    last_paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(expiry_date__gt=F("start_date")),
                name="subscription_expiry_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.organization.name} - {self.plan.code}"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE and self.expiry_date > timezone.now()

    @property
    def days_remaining(self):
        if self.expiry_date <= timezone.now():
            return 0
        return max(0, (self.expiry_date - timezone.now()).days)


class BillingTransaction(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Successful"
        FAILED = "FAILED", "Failed"
        ABANDONED = "ABANDONED", "Abandoned"
        INVALID = "INVALID", "Invalid"

    class Kind(models.TextChoices):
        INITIAL_SETUP = "INITIAL_SETUP", "Initial setup"
        RENEWAL = "RENEWAL", "Renewal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="billing_transactions",
    )
    subscription = models.ForeignKey(
        OrganizationSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    reference = models.CharField(max_length=255, unique=True, db_index=True)
    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.RENEWAL)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default="NGN")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    email = models.EmailField()
    paystack_access_code = models.CharField(max_length=255, blank=True, default="")
    paystack_transaction_id = models.CharField(max_length=255, blank=True, default="")
    paystack_channel = models.CharField(max_length=64, blank=True, default="")
    is_fulfilled = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="billing_transaction_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["PENDING", "SUCCESS", "FAILED", "ABANDONED", "INVALID"]),
                name="valid_billing_transaction_status",
            ),
        ]

    def __str__(self):
        return f"{self.reference} ({self.status})"

    @property
    def is_successful(self):
        return self.status == self.Status.SUCCESS or self.is_fulfilled

    @property
    def amount_kobo(self):
        return int((self.amount * Decimal("100")).quantize(Decimal("1")))
