import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import BillingTransaction, OrganizationSubscription, SubscriptionPlan
from apps.billing.serializers import (
    BillingTransactionSerializer,
    PaymentInitializationSerializer,
    SubscriptionPlanSerializer,
    build_subscription_summary,
)
from apps.billing.services import (
    PaystackClient,
    PaystackError,
    ensure_default_plans,
    fulfill_payment,
    generate_reference,
    get_latest_subscription,
    get_subscription_status,
    verify_paystack_webhook_signature,
)
from apps.organizations.permissions import IsOrganizationMember, IsOwner, OrganizationContextMixin


class SubscriptionPlanListView(OrganizationContextMixin, generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]
    serializer_class = SubscriptionPlanSerializer

    def get_queryset(self):
        ensure_default_plans()
        return SubscriptionPlan.objects.filter(is_active=True).order_by("sort_order", "code")


class BillingSummaryView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

    def get(self, request, *args, **kwargs):
        summary = build_subscription_summary(request.organization)
        transactions = BillingTransaction.objects.filter(organization=request.organization).select_related("plan").order_by("-created_at")[:10]
        payload = {
            **summary,
            "transactions": BillingTransactionSerializer(transactions, many=True).data,
        }
        return Response(payload, status=status.HTTP_200_OK)


class BillingInitializeView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember, IsOwner]
    serializer_class = PaymentInitializationSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.validated_data["plan_code"]
        reference = generate_reference("INV")
        transaction = BillingTransaction.objects.create(
            organization=request.organization,
            plan=plan,
            reference=reference,
            amount=plan.amount,
            currency="NGN",
            email=request.user.email,
            status=BillingTransaction.Status.PENDING,
            metadata={
                "organization_id": str(request.organization.pk),
                "plan_code": plan.code,
                "user_id": request.user.pk,
            },
            kind=(
                BillingTransaction.Kind.INITIAL_SETUP
                if plan.code == SubscriptionPlan.Code.INITIAL_SETUP_6_MONTHS
                else BillingTransaction.Kind.RENEWAL
            ),
        )

        callback_url = getattr(settings, "PAYSTACK_CALLBACK_URL", "") or f"{settings.FRONTEND_BASE_URL.rstrip('/')}/billing/callback"
        try:
            response = PaystackClient.initialize_transaction(
                email=request.user.email,
                amount_kobo=plan.amount_kobo,
                reference=reference,
                callback_url=callback_url,
                plan_code=plan.code,
                organization_id=request.organization.pk,
            )
        except PaystackError as exc:
            transaction.status = BillingTransaction.Status.INVALID
            transaction.save(update_fields=["status", "updated_at"])
            raise ValidationError({"detail": str(exc)}) from exc

        data = response.get("data", {}) if isinstance(response, dict) else {}
        transaction.paystack_access_code = data.get("access_code", "")
        transaction.paystack_transaction_id = data.get("id", "")
        transaction.paystack_channel = data.get("channel", "")
        transaction.save(update_fields=[
            "paystack_access_code",
            "paystack_transaction_id",
            "paystack_channel",
            "updated_at",
        ])

        return Response(
            {
                "reference": reference,
                "status": "pending",
                "authorization_url": data.get("authorization_url"),
                "amount": float(plan.amount),
                "currency": "NGN",
                "plan_code": plan.code,
            },
            status=status.HTTP_200_OK,
        )


class BillingVerifyView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember, IsOwner]

    def post(self, request, *args, **kwargs):
        reference = (request.data or {}).get("reference") or request.query_params.get("reference")
        if not reference:
            raise ValidationError({"reference": "A payment reference is required."})

        transaction = BillingTransaction.objects.filter(
            reference=reference,
            organization=request.organization,
        ).select_related("organization", "plan").first()
        if transaction is None:
            raise ValidationError({"reference": "No payment was found for this reference."})

        if transaction.is_fulfilled:
            return Response({"reference": reference, "status": transaction.status, "subscription": build_subscription_summary(request.organization)}, status=status.HTTP_200_OK)

        try:
            verification = PaystackClient.verify_transaction(reference)
        except PaystackError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        payload = verification.get("data", {}) if isinstance(verification, dict) else {}
        status_value = payload.get("status")
        amount_value = payload.get("amount", 0)
        currency_value = (payload.get("currency") or "").upper()
        reference_value = payload.get("reference")
        metadata = payload.get("metadata") or {}

        if status_value != "success":
            transaction.status = BillingTransaction.Status.FAILED
            transaction.save(update_fields=["status", "updated_at"])
            return Response({"reference": reference, "status": "failed"}, status=status.HTTP_200_OK)

        if reference_value != reference:
            raise ValidationError({"reference": "The payment reference does not match the pending transaction."})
        if amount_value != transaction.amount_kobo:
            raise ValidationError({"amount": "The payment amount does not match the expected plan price."})
        if currency_value != "NGN":
            raise ValidationError({"currency": "Only NGN payments are accepted."})
        if metadata.get("organization_id") and str(metadata.get("organization_id")) != str(request.organization.pk):
            raise ValidationError({"organization": "This payment does not belong to this organization."})
        if transaction.is_fulfilled:
            return Response({"reference": reference, "status": transaction.status, "subscription": build_subscription_summary(request.organization)}, status=status.HTTP_200_OK)

        transaction.status = BillingTransaction.Status.SUCCESS
        transaction.paystack_transaction_id = payload.get("id", transaction.paystack_transaction_id)
        transaction.last_verified_at = timezone.now()
        transaction.save(update_fields=["status", "paystack_transaction_id", "last_verified_at", "updated_at"])

        subscription = fulfill_payment(transaction)
        return Response(
            {
                "reference": reference,
                "status": "success",
                "subscription": {
                    "status": subscription.status,
                    "start_date": subscription.start_date.isoformat(),
                    "expiry_date": subscription.expiry_date.isoformat(),
                    "plan_code": subscription.plan.code,
                },
            },
            status=status.HTTP_200_OK,
        )


class PaystackWebhookView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        raw_body = request.body
        signature = request.META.get("HTTP_X_PAYSTACK_SIGNATURE", "")

        if not verify_paystack_webhook_signature(raw_body, signature):
            return Response({"detail": "Invalid webhook signature."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (TypeError, ValueError):
            return Response({"detail": "Malformed webhook payload."}, status=status.HTTP_400_BAD_REQUEST)

        event = payload.get("event")
        data = payload.get("data") or {}
        reference = data.get("reference")
        if not reference:
            return Response({"detail": "Webhook reference missing."}, status=status.HTTP_400_BAD_REQUEST)

        payment = BillingTransaction.objects.filter(reference=reference).select_related("organization", "plan").first()
        if payment is None:
            return Response({"status": "ignored"}, status=status.HTTP_200_OK)

        if payment.is_fulfilled:
            return Response({"status": "already-processed"}, status=status.HTTP_200_OK)

        if event == "charge.success":
            if payment.amount_kobo != int(data.get("amount", 0)):
                payment.status = BillingTransaction.Status.INVALID
                payment.save(update_fields=["status", "updated_at"])
                return Response({"status": "invalid-amount"}, status=status.HTTP_400_BAD_REQUEST)
            if (data.get("currency") or "").upper() != "NGN":
                payment.status = BillingTransaction.Status.INVALID
                payment.save(update_fields=["status", "updated_at"])
                return Response({"status": "invalid-currency"}, status=status.HTTP_400_BAD_REQUEST)
            if payment.status != BillingTransaction.Status.SUCCESS:
                payment.status = BillingTransaction.Status.SUCCESS
                payment.paystack_transaction_id = str(data.get("id", payment.paystack_transaction_id))
                payment.last_verified_at = timezone.now()
                payment.save(update_fields=["status", "paystack_transaction_id", "last_verified_at", "updated_at"])
            fulfill_payment(payment)

        elif event in {"charge.failed", "transfer.failed"}:
            payment.status = BillingTransaction.Status.FAILED
            payment.save(update_fields=["status", "updated_at"])

        elif event in {"charge.pending", "pending"}:
            payment.status = BillingTransaction.Status.PENDING
            payment.save(update_fields=["status", "updated_at"])

        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class BillingCallbackView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        reference = request.query_params.get("reference")
        if not reference:
            return Response({"detail": "A payment reference is required."}, status=status.HTTP_400_BAD_REQUEST)
        transaction = BillingTransaction.objects.filter(reference=reference).select_related("organization", "plan").first()
        if transaction is None:
            return Response({"status": "unknown", "reference": reference}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "reference": reference,
                "status": transaction.status,
                "fulfilled": transaction.is_fulfilled,
                "organization": str(transaction.organization_id),
            },
            status=status.HTTP_200_OK,
        )
