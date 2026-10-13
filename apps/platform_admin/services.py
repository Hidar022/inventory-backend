import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.billing.models import BillingTransaction, OrganizationSubscription
from apps.organizations.models import Membership, Organization
from apps.platform_admin.models import PlatformAuditEvent
from apps.platform_admin.permissions import PLATFORM_ADMIN_GROUP

User = get_user_model()

SENSITIVE_METADATA_PARTS = (
    "password",
    "token",
    "secret",
    "authorization",
    "credential",
    "api_key",
    "private_key",
    "jwt",
    "access_code",
    "webhook",
    "token_hash",
)
SENSITIVE_INLINE_VALUE = re.compile(
    r"(?i)(password|token|secret|authorization|credential|api[_-]?key|private[_-]?key|webhook)\s*[:=]\s*[^\s,;]+"
)
JWT_VALUE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def sanitize_audit_data(value):
    if isinstance(value, dict):
        return {
            str(key): sanitize_audit_data(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in SENSITIVE_METADATA_PARTS)
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_audit_data(item) for item in value]
    if isinstance(value, str):
        value = SENSITIVE_INLINE_VALUE.sub(r"\1=[REDACTED]", value)
        return JWT_VALUE.sub("[REDACTED]", value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def record_platform_event(
    *,
    actor,
    action,
    target_type,
    target_id,
        reason="",
    organization=None,
    before=None,
    after=None,
):
    return PlatformAuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
            reason=sanitize_audit_data(reason),
        organization=organization,
        before=sanitize_audit_data(before or {}),
        after=sanitize_audit_data(after or {}),
    )


def get_platform_overview(*, date_from, date_to, currency):
    now = timezone.now()
    payment_period = BillingTransaction.objects.filter(
        status=BillingTransaction.Status.SUCCESS,
        is_fulfilled=True,
        currency=currency,
        paid_at__date__gte=date_from,
        paid_at__date__lte=date_to,
    )
    subscription_queryset = OrganizationSubscription.objects.select_related(
        "organization", "plan"
    )
    active_subscriptions = subscription_queryset.filter(
        status=OrganizationSubscription.Status.ACTIVE,
        expiry_date__gt=now,
    )
    expired_subscriptions = subscription_queryset.filter(
        Q(status=OrganizationSubscription.Status.EXPIRED)
        | Q(status=OrganizationSubscription.Status.ACTIVE, expiry_date__lte=now)
    )
    recent_organizations = Organization.objects.annotate(
        member_count=Count("memberships", distinct=True),
        active_member_count=Count(
            "memberships",
            filter=Q(memberships__is_active=True, memberships__user__is_active=True),
            distinct=True,
        ),
    ).order_by("-created_at")[:5]

    return {
        "organizations_total": Organization.objects.count(),
        "organizations_active": Organization.objects.filter(is_active=True).count(),
        "organizations_suspended": Organization.objects.filter(is_active=False).count(),
        "users_total": User.objects.count(),
        "users_active": User.objects.filter(is_active=True).count(),
        "users_deactivated": User.objects.filter(is_active=False).count(),
        "subscriptions_active": active_subscriptions.count(),
        "subscriptions_expired": expired_subscriptions.count(),
        "subscriptions_expiring_30_days": active_subscriptions.filter(
            expiry_date__lte=now + timedelta(days=30)
        ).count(),
        "payments_pending": BillingTransaction.objects.filter(
            status=BillingTransaction.Status.PENDING
        ).count(),
        "payments_successful": BillingTransaction.objects.filter(
            status=BillingTransaction.Status.SUCCESS,
            is_fulfilled=True,
        ).count(),
        "revenue_received": payment_period.aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00"),
        "currency": currency,
        "date_from": date_from,
        "date_to": date_to,
        "recent_organizations": recent_organizations,
        "recent_payments": BillingTransaction.objects.filter(
            status=BillingTransaction.Status.SUCCESS,
            is_fulfilled=True,
        ).select_related("organization").order_by("-paid_at", "-created_at")[:5],
        "subscriptions_requiring_attention": active_subscriptions.filter(
            expiry_date__lte=now + timedelta(days=30)
        ).order_by("expiry_date")[:5],
        "recent_administrative_activity": PlatformAuditEvent.objects.select_related(
            "actor", "organization"
        ).order_by("-created_at", "-id")[:8],
    }


def get_platform_finance_summary(*, date_from, date_to, currency):
    payments = BillingTransaction.objects.filter(
        currency=currency,
        paid_at__date__gte=date_from,
        paid_at__date__lte=date_to,
    )
    verified = payments.filter(
        status=BillingTransaction.Status.SUCCESS,
        is_fulfilled=True,
    )
    return {
        "payments_pending": BillingTransaction.objects.filter(
            status=BillingTransaction.Status.PENDING,
        ).count(),
        "payments_successful": BillingTransaction.objects.filter(
            status=BillingTransaction.Status.SUCCESS,
            is_fulfilled=True,
        ).count(),
        "revenue_received": verified.aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00"),
        "currency": currency,
        "date_from": date_from,
        "date_to": date_to,
    }


def lock_active_platform_admins():
    group = Group.objects.select_for_update().filter(name=PLATFORM_ADMIN_GROUP).first()
    if group is None:
        return []
    return list(
        User.objects.select_for_update()
        .filter(
            is_active=True,
            groups=group,
            groups__permissions__content_type__app_label="platform_admin",
            groups__permissions__codename="manage_platform_team",
        )
        .order_by("pk")
        .distinct()
    )


def assert_other_active_platform_admin_remains(user):
    if not user.groups.filter(
        name=PLATFORM_ADMIN_GROUP,
        permissions__content_type__app_label="platform_admin",
        permissions__codename="manage_platform_team",
    ).exists():
        return
    active_admins = lock_active_platform_admins()
    if user.is_active and len(active_admins) <= 1:
        from rest_framework.exceptions import ValidationError

        raise ValidationError(
            {"detail": "The final active platform administrator cannot be deactivated."}
        )


def assert_organization_retains_active_owner(user):
    owner_memberships = Membership.objects.select_for_update().filter(
        user=user,
        role=Membership.Role.OWNER,
        is_active=True,
    ).select_related("organization")
    for membership in owner_memberships:
        Organization.objects.select_for_update().get(pk=membership.organization_id)
        other_owner_exists = Membership.objects.filter(
            organization_id=membership.organization_id,
            role=Membership.Role.OWNER,
            is_active=True,
            user__is_active=True,
        ).exclude(user=user).exists()
        if not other_owner_exists:
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {
                    "detail": (
                        f"Transfer ownership of {membership.organization.name} "
                        "before deactivating its last active owner."
                    )
                }
            )