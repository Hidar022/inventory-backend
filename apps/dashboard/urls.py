from django.urls import path

from apps.dashboard.views import DashboardView

app_name = "dashboard"

urlpatterns = [path("dashboard/", DashboardView.as_view(), name="dashboard")]