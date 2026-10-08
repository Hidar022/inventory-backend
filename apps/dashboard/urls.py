from django.urls import path

from apps.dashboard.views import ActivityEventListView, DashboardReportsView, DashboardView

app_name = "dashboard"

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("dashboard/reports/", DashboardReportsView.as_view(), name="dashboard-reports"),
    path("dashboard/activity/", ActivityEventListView.as_view(), name="dashboard-activity"),
]