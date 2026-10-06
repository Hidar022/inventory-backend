from rest_framework.routers import DefaultRouter

from apps.purchases.views import PurchaseViewSet, SupplierViewSet

app_name = "purchases"

router = DefaultRouter()
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("purchases", PurchaseViewSet, basename="purchase")

urlpatterns = router.urls
