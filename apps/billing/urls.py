from django.urls import path

from apps.billing.views import (
    BillingCallbackView,
    BillingInitializeView,
    BillingSummaryView,
    BillingVerifyView,
    PaystackWebhookView,
    SubscriptionPlanListView,
)

app_name = "billing"

urlpatterns = [
    path("billing/plans/", SubscriptionPlanListView.as_view(), name="plans"),
    path("billing/summary/", BillingSummaryView.as_view(), name="summary"),
    path("billing/initialize/", BillingInitializeView.as_view(), name="initialize"),
    path("billing/verify/", BillingVerifyView.as_view(), name="verify"),
    path("billing/webhook/", PaystackWebhookView.as_view(), name="webhook"),
    path("billing/callback/", BillingCallbackView.as_view(), name="callback"),
]
