from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.expenses.views import DailyCashSummaryView, ExpenseCategoryViewSet, ExpenseViewSet

app_name = "expenses"

router = DefaultRouter()
router.register("expense-categories", ExpenseCategoryViewSet, basename="expense-category")
router.register("expenses", ExpenseViewSet, basename="expense")

urlpatterns = [
    path("cash/summary/", DailyCashSummaryView.as_view(), name="cash-summary"),
    path("", include(router.urls)),
]