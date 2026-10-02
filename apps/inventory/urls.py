from django.urls import path

from apps.inventory.views import (
	InventoryListView,
	StockAdjustmentView,
	StockInView,
	StockMovementListView,
)

app_name = "inventory"

urlpatterns = [
	path("", InventoryListView.as_view(), name="inventory-list"),
	path("stock-in/", StockInView.as_view(), name="stock-in"),
	path("adjust/", StockAdjustmentView.as_view(), name="stock-adjustment"),
	path("movements/", StockMovementListView.as_view(), name="movement-list"),
]