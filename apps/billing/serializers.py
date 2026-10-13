from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.billing.models import BillingTransaction, OrganizationSubscription, SubscriptionPlan
from apps.billing.services import ensure_default_plans, get_latest_subscription


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=14, decimal_places=2, source="amount", read_only=True)

    class Meta:
        model = SubscriptionPlan
        fields = [
            "code",
            "name",
            "description",
            "months",
            "price",
            "currency",
            "sort_order",
        ]


class PaymentInitializationSerializer(serializers.Serializer):
    plan_code = serializers.CharField(max_length=32)

    def validate_plan_code(self, value):
        plans = ensure_default_plans()
        plan = next((item for item in plans if item.code == value), None)
        if plan is None:
            raise serializers.ValidationError("Select a valid billing plan.")
        return plan


class SubscriptionSummarySerializer(serializers.ModelSerializer):
    plan_code = serializers.CharField(source="plan.code", read_only=True)
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    amount = serializers.DecimalField(source="plan.amount", max_digits=14, decimal_places=2, read_only=True)
    status = serializers.SerializerMethodField()
    days_remaining = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationSubscription
        fields = [
            "plan_code",
            "plan_name",
            "status",
            "start_date",
            "expiry_date",
            "amount",
            "days_remaining",
            "is_active",
            "is_setup",
        ]

    def get_status(self, obj):
        if obj.expiry_date <= timezone.now() and obj.status == OrganizationSubscription.Status.ACTIVE:
            return OrganizationSubscription.Status.EXPIRED
        return obj.status

    def get_days_remaining(self, obj):
        return obj.days_remaining

    def get_is_active(self, obj):
        return obj.is_active


class BillingTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingTransaction
        fields = [
            "id",
            "reference",
            "plan",
            "amount",
            "currency",
            "status",
            "kind",
            "created_at",
            "paid_at",
            "fulfilled_at",
        ]
        read_only_fields = fields


def build_subscription_summary(organization):
    subscription = get_latest_subscription(organization)
    initial_transactions = BillingTransaction.objects.filter(
        organization=organization,
        kind=BillingTransaction.Kind.INITIAL_SETUP,
    )
    if (
        OrganizationSubscription.objects.filter(
            organization=organization,
            is_setup=True,
        ).exists()
        or initial_transactions.filter(is_fulfilled=True).exists()
    ):
        initial_activation = {"status": "COMPLETED", "reference": None}
    else:
        pending_initial = initial_transactions.filter(
            status__in=[BillingTransaction.Status.PENDING, BillingTransaction.Status.SUCCESS],
            is_fulfilled=False,
        ).order_by("-created_at").first()
        if pending_initial:
            initial_activation = {
                "status": "PENDING",
                "reference": pending_initial.reference,
            }
        else:
            latest_initial = initial_transactions.order_by("-created_at").first()
            initial_activation = {
                "status": (
                    "FAILED"
                    if latest_initial
                    and latest_initial.status
                    in [
                        BillingTransaction.Status.FAILED,
                        BillingTransaction.Status.ABANDONED,
                        BillingTransaction.Status.INVALID,
                    ]
                    else "NOT_STARTED"
                ),
                "reference": latest_initial.reference if latest_initial else None,
            }

    if subscription is None:
        return {
            "status": "NO_SUBSCRIPTION",
            "plan_code": None,
            "plan_name": "No active subscription",
            "start_date": None,
            "expiry_date": None,
            "days_remaining": 0,
            "is_active": False,
            "is_setup": False,
            "amount": Decimal("0.00"),
            "initial_activation": initial_activation,
        }
    serializer = SubscriptionSummarySerializer(subscription)
    return {**serializer.data, "initial_activation": initial_activation}
