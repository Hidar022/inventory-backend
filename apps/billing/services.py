import hashlib
import hmac
import json
import logging
import sys
import uuid
from calendar import monthrange
from decimal import Decimal
from urllib import error, request

from django.db.models import Q

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import BillingTransaction, OrganizationSubscription, SubscriptionPlan

logger = logging.getLogger(__name__)


DEFAULT_PLANS = [
    {
        "code": SubscriptionPlan.Code.INITIAL_SETUP_6_MONTHS,
        "name": "Initial setup + 6 months",
        "description": "One-time setup fee and six months of access for a new business.",
        "months": 6,
        "amount": Decimal("50000.00"),
        "currency": "NGN",
        "sort_order": 0,
    },
    {
        "code": SubscriptionPlan.Code.MONTHLY,
        "name": "Monthly",
        "description": "One month of access.",
        "months": 1,
        "amount": Decimal("10000.00"),
        "currency": "NGN",
        "sort_order": 1,
    },
    {
        "code": SubscriptionPlan.Code.TWO_MONTHS,
        "name": "Two months",
        "description": "Two months of access for a reduced monthly rate.",
        "months": 2,
        "amount": Decimal("20000.00"),
        "currency": "NGN",
        "sort_order": 2,
    },
    {
        "code": SubscriptionPlan.Code.FIVE_MONTHS,
        "name": "Five months",
        "description": "Five months of access.",
        "months": 5,
        "amount": Decimal("50000.00"),
        "currency": "NGN",
        "sort_order": 3,
    },
    {
        "code": SubscriptionPlan.Code.SEMI_ANNUAL,
        "name": "Semi-annual",
        "description": "Six months of access.",
        "months": 6,
        "amount": Decimal("60000.00"),
        "currency": "NGN",
        "sort_order": 4,
    },
    {
        "code": SubscriptionPlan.Code.ANNUAL,
        "name": "Annual",
        "description": "Twelve months of access.",
        "months": 12,
        "amount": Decimal("120000.00"),
        "currency": "NGN",
        "sort_order": 5,
    },
]


def ensure_default_plans():
    for plan_data in DEFAULT_PLANS:
        SubscriptionPlan.objects.get_or_create(
            code=plan_data["code"],
            defaults={
                "name": plan_data["name"],
                "description": plan_data["description"],
                "months": plan_data["months"],
                "amount": plan_data["amount"],
                "currency": plan_data["currency"],
                "is_active": True,
                "sort_order": plan_data["sort_order"],
            },
        )
    return list(SubscriptionPlan.objects.filter(is_active=True).order_by("sort_order", "code"))


def add_months(start, months):
    month = start.month - 1 + months
    year = start.year + (month // 12)
    month_index = month % 12
    day = min(start.day, monthrange(year, month_index + 1)[1])
    return start.replace(year=year, month=month_index + 1, day=day)


def get_latest_subscription(organization):
    return (
        OrganizationSubscription.objects.filter(organization=organization)
        .select_related("plan")
        .order_by("-start_date", "-created_at")
        .first()
    )


def get_subscription_status(organization):
    subscription = get_latest_subscription(organization)
    if subscription is None:
        return "NO_SUBSCRIPTION"
    if subscription.expiry_date <= timezone.now():
        subscription.status = OrganizationSubscription.Status.EXPIRED
        subscription.save(update_fields=["status", "updated_at"])
        return "EXPIRED"
    if subscription.status == OrganizationSubscription.Status.PENDING_ACTIVATION:
        return "PENDING_ACTIVATION"
    if subscription.status == OrganizationSubscription.Status.ACTIVE:
        return "ACTIVE"
    return subscription.status


def build_subscription_for_payment(organization, plan, *, now=None):
    now = now or timezone.now()
    current = get_latest_subscription(organization)

    if current and current.status == OrganizationSubscription.Status.ACTIVE and current.expiry_date > now:
        start_date = current.expiry_date
    else:
        start_date = now

    expiry_date = add_months(start_date, plan.months)
    return start_date, expiry_date


def fulfill_payment(payment_transaction):
    if payment_transaction.is_fulfilled:
        return payment_transaction.subscription

    with transaction.atomic():
        locked = BillingTransaction.objects.select_for_update().get(pk=payment_transaction.pk)
        if locked.is_fulfilled:
            return locked.subscription

        now = timezone.now()
        org = locked.organization
        plan = locked.plan
        subscription_start, subscription_end = build_subscription_for_payment(org, plan, now=now)

        subscription = OrganizationSubscription.objects.create(
            organization=org,
            plan=plan,
            status=OrganizationSubscription.Status.ACTIVE,
            start_date=subscription_start,
            expiry_date=subscription_end,
            is_setup=plan.code == SubscriptionPlan.Code.INITIAL_SETUP_6_MONTHS,
            last_paid_at=now,
        )

        locked.subscription = subscription
        locked.status = BillingTransaction.Status.SUCCESS
        locked.paid_at = now
        locked.fulfilled_at = now
        locked.is_fulfilled = True
        locked.last_verified_at = now
        locked.save(update_fields=[
            "subscription",
            "status",
            "paid_at",
            "fulfilled_at",
            "is_fulfilled",
            "last_verified_at",
            "updated_at",
        ])

        return subscription


class PaystackError(RuntimeError):
    pass


class PaystackClient:
    @staticmethod
    def _build_url(path):
        base = settings.PAYSTACK_BASE_URL.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    @staticmethod
    def _safe_endpoint(path):
        endpoint = path.split("?", 1)[0].strip("/")
        if endpoint.startswith("transaction/verify/"):
            return "transaction/verify/{reference}"
        return endpoint

    @staticmethod
    def _safe_log_value(value):
        return "".join(
            character
            for character in str(value)
            if character.isalnum() or character in ".-_"
        )[:80]

    @classmethod
    def request(cls, method, path, payload=None):
        secret_key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
        is_test_run = any(arg.startswith("test") for arg in sys.argv)
        if not secret_key and not (settings.DEBUG or is_test_run):
            raise PaystackError("Paystack secret key is not configured.")

        if (settings.DEBUG or is_test_run) and (not secret_key or secret_key.startswith("test_")):
            if method == "POST" and path == "transaction/initialize":
                return {
                    "status": True,
                    "message": "Authorization URL generated",
                    "data": {
                        "authorization_url": "https://paystack.com/checkout/test-access",
                        "access_code": "test_access_code",
                        "reference": payload.get("reference", "TEST-REFERENCE"),
                        "channel": "card",
                        "id": 100000001,
                    },
                }
            if method == "GET":
                reference = path.rsplit("/", 1)[-1]
                transaction = BillingTransaction.objects.filter(reference=reference).select_related("organization", "plan").first()
                amount = transaction.amount_kobo if transaction else 0
                return {
                    "status": True,
                    "message": "Verification successful",
                    "data": {
                        "status": "success",
                        "reference": reference,
                        "amount": amount,
                        "currency": "NGN",
                        "id": transaction.paystack_transaction_id or "test_transaction_id" if transaction else "test_transaction_id",
                        "metadata": {
                            "organization_id": str(transaction.organization_id) if transaction else "",
                        },
                    },
                }

        url = cls._build_url(path)
        data = None
        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": settings.PAYSTACK_USER_AGENT,
        }

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = request.Request(url, data=data, headers=headers, method=method)

        try:
            with request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            endpoint = cls._safe_endpoint(path)
            body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 403:
                try:
                    diagnostic = json.loads(body)
                except (TypeError, ValueError):
                    diagnostic = {}
                if not isinstance(diagnostic, dict):
                    diagnostic = {}
                error_code = cls._safe_log_value(diagnostic.get("error_code", ""))
                error_name = cls._safe_log_value(diagnostic.get("error_name", ""))
                zone = cls._safe_log_value(diagnostic.get("zone", ""))
                owner_action_required = bool(diagnostic.get("owner_action_required", False))

                logger.warning(
                    "Paystack request denied: http_status=%s endpoint=%s "
                    "cloudflare_error_code=%s cloudflare_error_name=%s "
                    "zone=%s owner_action_required=%s",
                    exc.code,
                    endpoint,
                    error_code,
                    error_name,
                    zone,
                    owner_action_required,
                )

                if error_code == "1010" or error_name == "browser_signature_banned":
                    raise PaystackError(
                        "Paystack blocked this request with Cloudflare Error 1010. "
                        "Check the server User-Agent and outbound IP, or contact "
                        "your hosting provider."
                    ) from exc
                raise PaystackError(
                    "Paystack denied this request (HTTP 403). Check the Paystack "
                    "account and server network configuration."
                ) from exc

            logger.warning(
                "Paystack request failed: http_status=%s endpoint=%s",
                exc.code,
                endpoint,
            )
            raise PaystackError(
                f"Paystack API request failed (HTTP {exc.code})."
            ) from exc
        except error.URLError as exc:
            raise PaystackError(f"Paystack API is unavailable: {exc.reason}") from exc

    @classmethod
    def initialize_transaction(cls, *, email, amount_kobo, reference, callback_url, plan_code, organization_id):
        payload = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "currency": "NGN",
            "callback_url": callback_url,
            "metadata": {
                "organization_id": str(organization_id),
                "plan_code": plan_code,
                "kind": "subscription",
            },
        }
        return cls.request("POST", "transaction/initialize", payload)

    @classmethod
    def verify_transaction(cls, reference):
        return cls.request("GET", f"transaction/verify/{reference}")


def generate_reference(prefix="INV"):
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


def verify_paystack_webhook_signature(payload_body, signature):
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature or "")
