from django.urls import path

from apps.organizations.views import CurrentOrganizationView, OrganizationCreateView

app_name = "organizations"

urlpatterns = [
    path("organizations/", OrganizationCreateView.as_view(), name="organization-create"),
    path("organizations/current/", CurrentOrganizationView.as_view(), name="organization-current"),
]
