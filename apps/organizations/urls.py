from django.urls import path

from apps.organizations.views import (
    CurrentOrganizationView,
    InvitationAcceptView,
    InvitationValidationView,
    OrganizationCreateView,
    TeamActivationView,
    TeamDeactivationView,
    TeamInvitationResendView,
    TeamInvitationRevokeView,
    TeamMemberListCreateView,
)

app_name = "organizations"

urlpatterns = [
    path("organizations/", OrganizationCreateView.as_view(), name="organization-create"),
    path("organizations/current/", CurrentOrganizationView.as_view(), name="organization-current"),
    path("team/", TeamMemberListCreateView.as_view(), name="team-list-create"),
    path("team/invitations/accept/", InvitationAcceptView.as_view(), name="team-invitation-accept"),
    path("team/invitations/validate/", InvitationValidationView.as_view(), name="team-invitation-validate"),
    path("team/<int:pk>/resend-invitation/", TeamInvitationResendView.as_view(), name="team-resend-invitation"),
    path("team/<int:pk>/revoke-invitation/", TeamInvitationRevokeView.as_view(), name="team-revoke-invitation"),
    path("team/<int:pk>/activate/", TeamActivationView.as_view(), name="team-activate"),
    path("team/<int:pk>/deactivate/", TeamDeactivationView.as_view(), name="team-deactivate"),
]
