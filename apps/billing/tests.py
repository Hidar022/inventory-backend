from decimal import Decimal
from datetime import timedelta
import hashlib
import hmac
import io
import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.billing.models import BillingTransaction, OrganizationSubscription, SubscriptionPlan
from apps.organizations.permissions import HasActiveSubscription
from apps.organizations.models import Membership, Organization
from apps.billing.services import PaystackClient, PaystackError

User = get_user_model()


class BillingAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(email="billing-owner@example.com", password="StrongPass123!")
        self.manager = User.objects.create_user(email="billing-manager@example.com", password="StrongPass123!")
        self.cashier = User.objects.create_user(email="billing-cashier@example.com", password="StrongPass123!")
        self.organization = Organization.objects.create(name="Billing Business", slug="billing-business")
        Membership.objects.create(user=self.owner, organization=self.organization, role=Membership.Role.OWNER, is_active=True)
        Membership.objects.create(user=self.manager, organization=self.organization, role=Membership.Role.MANAGER, is_active=True)
        Membership.objects.create(user=self.cashier, organization=self.organization, role=Membership.Role.CASHIER, is_active=True)

    def test_summary_reports_authoritative_initial_activation_lifecycle(self):
        initial_plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.INITIAL_SETUP_6_MONTHS,
            name="Initial setup + 6 months",
            description="Initial activation",
            months=6,
            amount=Decimal("50000.00"),
            currency="NGN",
        )
        renewal_plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="Monthly access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
        )
        self.client.force_authenticate(user=self.owner)

        response = self.client.get("/api/v1/billing/summary/")
        self.assertEqual(response.data["initial_activation"]["status"], "NOT_STARTED")

        initial_payment = BillingTransaction.objects.create(
            organization=self.organization,
            plan=initial_plan,
            reference="INV-LIFECYCLE-PENDING",
            kind=BillingTransaction.Kind.INITIAL_SETUP,
            amount=initial_plan.amount,
            currency="NGN",
            email=self.owner.email,
        )
        response = self.client.get("/api/v1/billing/summary/")
        self.assertEqual(response.data["initial_activation"]["status"], "PENDING")
        self.assertEqual(
            response.data["initial_activation"]["reference"],
            initial_payment.reference,
        )

        initial_payment.status = BillingTransaction.Status.FAILED
        initial_payment.save(update_fields=["status", "updated_at"])
        response = self.client.get("/api/v1/billing/summary/")
        self.assertEqual(response.data["initial_activation"]["status"], "FAILED")

        initial_payment.status = BillingTransaction.Status.SUCCESS
        initial_payment.is_fulfilled = True
        initial_payment.fulfilled_at = timezone.now()
        initial_payment.save(
            update_fields=["status", "is_fulfilled", "fulfilled_at", "updated_at"]
        )
        for index in range(10):
            BillingTransaction.objects.create(
                organization=self.organization,
                plan=renewal_plan,
                reference=f"INV-LIFECYCLE-RENEWAL-{index}",
                kind=BillingTransaction.Kind.RENEWAL,
                amount=renewal_plan.amount,
                currency="NGN",
                email=self.owner.email,
            )

        response = self.client.get("/api/v1/billing/summary/")
        self.assertEqual(response.data["initial_activation"]["status"], "COMPLETED")
        self.assertEqual(len(response.data["transactions"]), 10)

    def test_initial_setup_and_renewal_plans_are_available(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.get("/api/v1/billing/plans/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data["results"]), 5)
        prices = {plan["code"]: Decimal(str(plan["price"])) for plan in response.data["results"]}
        self.assertEqual(prices["INITIAL_SETUP_6_MONTHS"], Decimal("50000"))
        self.assertEqual(prices["MONTHLY"], Decimal("10000"))
        self.assertEqual(prices["SEMI_ANNUAL"], Decimal("60000"))

    def test_owner_can_view_billing_summary_and_manager_cannot_purchase(self):
        self.client.force_authenticate(user=self.owner)
        summary = self.client.get("/api/v1/billing/summary/")
        self.assertEqual(summary.status_code, status.HTTP_200_OK)
        self.assertIn("status", summary.data)

        self.client.force_authenticate(user=self.manager)
        response = self.client.post(
            "/api/v1/billing/initialize/",
            {"plan_code": "MONTHLY"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_subscription_expiry_blocks_business_mutations(self):
        plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="Monthly access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
            sort_order=1,
        )
        OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=plan,
            status=OrganizationSubscription.Status.EXPIRED,
            start_date=timezone.now() - timedelta(days=40),
            expiry_date=timezone.now() - timedelta(days=10),
            is_setup=False,
        )
        self.client.force_authenticate(user=self.owner)
        response = self.client.post("/api/v1/products/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_initialize_paystack_checkout(self):
        self.client.force_authenticate(user=self.owner)

        with patch(
            "apps.billing.views.PaystackClient.initialize_transaction",
            return_value={
                "data": {
                    "authorization_url": "https://paystack.com/checkout/test",
                    "access_code": "test-access-code",
                    "id": 123,
                }
            },
        ):
            response = self.client.post(
                "/api/v1/billing/initialize/",
                {"plan_code": "MONTHLY"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], "pending")
        self.assertTrue(response.data["authorization_url"].startswith("https://"))

    @override_settings(
        DEBUG=False,
        PAYSTACK_SECRET_KEY="sk_test_request_mock",
        PAYSTACK_USER_AGENT="InventorySaaS-test/1.0",
    )
    def test_paystack_http_request_has_user_agent_and_secret_authorization(self):
        with patch("apps.billing.services.request.urlopen") as mocked_urlopen:
            mocked_urlopen.return_value.__enter__.return_value.read.return_value = (
                b'{"status": true}'
            )
            result = PaystackClient.request(
                "POST",
                "transaction/initialize",
                {"amount": 500000},
            )

        outgoing_request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            outgoing_request.full_url,
            "https://api.paystack.co/transaction/initialize",
        )
        self.assertEqual(
            outgoing_request.get_header("User-agent"),
            "InventorySaaS-test/1.0",
        )
        self.assertEqual(
            outgoing_request.get_header("Authorization"),
            "Bearer sk_test_request_mock",
        )
        self.assertTrue(result["status"])

    @override_settings(
        DEBUG=False,
        PAYSTACK_SECRET_KEY="sk_test_do_not_log",
        PAYSTACK_USER_AGENT="InventorySaaS-test/1.0",
    )
    def test_cloudflare_403_is_sanitized_logged_and_not_retried(self):
        cloudflare_response = json.dumps(
            {
                "error_code": 1010,
                "error_name": "browser_signature_banned",
                "zone": "api.paystack.co",
                "owner_action_required": True,
                "detail": "RAW CLOUDFARE DIAGNOSTIC MUST NOT BE DISCLOSED",
            }
        ).encode()
        http_error = HTTPError(
            "https://api.paystack.co/transaction/initialize",
            403,
            "Forbidden",
            {},
            io.BytesIO(cloudflare_response),
        )

        with patch(
            "apps.billing.services.request.urlopen",
            side_effect=http_error,
        ) as mocked_urlopen:
            with self.assertLogs("apps.billing.services", level="WARNING") as captured_logs:
                with self.assertRaises(PaystackError) as raised:
                    PaystackClient.request("POST", "transaction/initialize", {})

        self.assertIn("Cloudflare Error 1010", str(raised.exception))
        self.assertNotIn("RAW CLOUDFARE DIAGNOSTIC", str(raised.exception))
        self.assertEqual(mocked_urlopen.call_count, 1)
        joined_logs = "\n".join(captured_logs.output)
        self.assertIn("http_status=403", joined_logs)
        self.assertIn("endpoint=transaction/initialize", joined_logs)
        self.assertIn("browser_signature_banned", joined_logs)
        self.assertNotIn("sk_test_do_not_log", joined_logs)
        self.assertNotIn("Authorization", joined_logs)
        self.assertNotIn("RAW CLOUDFARE DIAGNOSTIC", joined_logs)

    def test_has_active_subscription_rejects_expired_subscription(self):
        plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="Monthly access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
            sort_order=1,
        )
        OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=plan,
            status=OrganizationSubscription.Status.EXPIRED,
            start_date=timezone.now() - timedelta(days=10),
            expiry_date=timezone.now() - timedelta(days=1),
            is_setup=False,
        )

        request = SimpleNamespace(
            user=self.owner,
            organization=self.organization,
            membership=Membership.objects.get(user=self.owner, organization=self.organization),
        )

        self.assertFalse(HasActiveSubscription().has_permission(request, None))

    @override_settings(
        PAYSTACK_SECRET_KEY="webhook-test-secret",
        PAYSTACK_WEBHOOK_SECRET="wrong-legacy-secret",
    )
    def test_webhook_cannot_downgrade_a_fulfilled_payment(self):
        plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="Monthly access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
            sort_order=1,
        )
        subscription = OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=plan,
            status=OrganizationSubscription.Status.ACTIVE,
            start_date=timezone.now(),
            expiry_date=timezone.now() + timedelta(days=30),
            is_setup=False,
        )
        payment = BillingTransaction.objects.create(
            organization=self.organization,
            subscription=subscription,
            plan=plan,
            reference="INV-WEBHOOK-FULFILLED",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=plan.amount,
            currency="NGN",
            status=BillingTransaction.Status.SUCCESS,
            email=self.owner.email,
            is_fulfilled=True,
            paid_at=timezone.now(),
            fulfilled_at=timezone.now(),
        )
        payload = json.dumps(
            {"event": "charge.failed", "data": {"reference": payment.reference}}
        ).encode()
        signature = hmac.new(
            b"webhook-test-secret",
            payload,
            hashlib.sha512,
        ).hexdigest()

        response = self.client.post(
            "/api/v1/billing/webhook/",
            data=payload,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(payment.status, BillingTransaction.Status.SUCCESS)

    def test_failed_paystack_payment_returns_a_normal_status_result(self):
        plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="Monthly access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
            sort_order=1,
        )
        payment = BillingTransaction.objects.create(
            organization=self.organization,
            plan=plan,
            reference="INV-FAILED-VERIFY",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=plan.amount,
            currency="NGN",
            email=self.owner.email,
        )
        self.client.force_authenticate(user=self.owner)

        with patch(
            "apps.billing.views.PaystackClient.verify_transaction",
            return_value={"data": {"status": "failed"}},
        ):
            response = self.client.post(
                "/api/v1/billing/verify/",
                {"reference": payment.reference},
                format="json",
            )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(payment.status, BillingTransaction.Status.FAILED)

    def test_pending_paystack_verification_does_not_mark_payment_failed(self):
        plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="Monthly access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
            sort_order=1,
        )
        payment = BillingTransaction.objects.create(
            organization=self.organization,
            plan=plan,
            reference="INV-PENDING-VERIFY",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=plan.amount,
            currency="NGN",
            email=self.owner.email,
        )
        self.client.force_authenticate(user=self.owner)

        with patch(
            "apps.billing.views.PaystackClient.verify_transaction",
            return_value={"data": {"status": "pending"}},
        ):
            response = self.client.post(
                "/api/v1/billing/verify/",
                {"reference": payment.reference},
                format="json",
            )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(payment.status, BillingTransaction.Status.PENDING)
        self.assertFalse(payment.is_fulfilled)
        self.assertEqual(
            OrganizationSubscription.objects.filter(organization=self.organization).count(),
            0,
        )

    def test_repeated_successful_verification_fulfills_payment_once(self):
        plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="Monthly access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
            sort_order=1,
        )
        payment = BillingTransaction.objects.create(
            organization=self.organization,
            plan=plan,
            reference="INV-VERIFY-IDEMPOTENT",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=plan.amount,
            currency="NGN",
            email=self.owner.email,
        )
        self.client.force_authenticate(user=self.owner)
        verification = {
            "data": {
                "status": "success",
                "reference": payment.reference,
                "amount": plan.amount_kobo,
                "currency": "NGN",
                "metadata": {"organization_id": str(self.organization.pk)},
            }
        }

        with patch(
            "apps.billing.views.PaystackClient.verify_transaction",
            return_value=verification,
        ):
            first_response = self.client.post(
                "/api/v1/billing/verify/",
                {"reference": payment.reference},
                format="json",
            )
            second_response = self.client.post(
                "/api/v1/billing/verify/",
                {"reference": payment.reference},
                format="json",
            )

        payment.refresh_from_db()
        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            OrganizationSubscription.objects.filter(organization=self.organization).count(),
            1,
        )
        self.assertTrue(payment.is_fulfilled)
