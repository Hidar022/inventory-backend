from django.urls import path

from apps.sales.views import (
    CheckoutView,
    SaleDetailView,
    SaleReturnListView,
    SalesListView,
)

app_name = "sales"

urlpatterns = [
    path("pos/checkout/", CheckoutView.as_view(), name="pos-checkout"),
    path("sales/", SalesListView.as_view(), name="sales-list"),
    path("sales/<uuid:sale_id>/returns/", SaleReturnListView.as_view(), name="sale-returns"),
    path("sales/<uuid:id>/", SaleDetailView.as_view(), name="sales-detail"),
]
