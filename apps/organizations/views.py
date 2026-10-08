import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from common.email import send_product_email
from apps.dashboard.services import log_activity_event
from apps.organizations.models import Membership, Organization, StaffInvitation
from apps.organizations.permissions import (
    IsOrganizationMember,
    IsOwner,
    OrganizationContextMixin,
)
from apps.organizations.serializers import (
    InvitationAcceptSerializer,
    InvitationValidationSerializer,
    OrganizationCreateSerializer,
    OrganizationSerializer,
    TeamCreateSerializer,
    TeamMemberSerializer,
)

User = get_user_model()


class OrganizationCreateView(OrganizationContextMixin, generics.CreateAPIView):
    queryset = Organization.objects.all()
    serializer_class = OrganizationCreateSerializer
    permission_classes = [permissions.IsAuthenticated]


class TeamMemberListCreateView(OrganizationContextMixin, generics.ListCreateAPIView):
    serializer_class = TeamMemberSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]

    def get_queryset(self):
        org = self.request.organization
        if not org:
            return Membership.objects.none()
        return (
            Membership.objects.select_related("user", "organization")
            .filter(organization=org)
            .order_by("user__first_name", "user__last_name", "user__email")
        )

    def create(self, request, *args, **kwargs):
        serializer = TeamCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            created = serializer.save()
            log_activity_event(
                organization=request.organization,
                actor=request.user,
                action="team.invitation_created",
                entity_type="StaffInvitation",
                entity_id=str(created["invitation"].pk),
                description=f"Invited {created['invitation'].email} as {created['invitation'].role.lower()}",
                metadata={"role": created["invitation"].role},
            )
        data = {
            "id": created["user"].id,
            "name": created["user"].get_full_name() or created["user"].email,
            "email": created["user"].email,
            "role": created["invitation"].role,
            "status": "PENDING",
            "invitation_status": "PENDING",
        }
        return Response(data, status=status.HTTP_201_CREATED)


class InvitationValidationView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = InvitationValidationSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "token",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Invitation token to validate.",
            )
        ],
        responses={
            200: {
                "type": "object",
                "properties": {
                    "valid": {"type": "boolean"},
                    "email": {"type": "string"},
                    "role": {"type": "string"},
                    "expires_at": {"type": "string", "format": "date-time"},
                },
            }
        },
    )
    def get(self, request, *args, **kwargs):
        token = request.query_params.get("token")
        if not token:
            return Response({"valid": False, "detail": "A token is required."}, status=400)

        serializer = InvitationValidationSerializer(data={"token": token})
        serializer.is_valid(raise_exception=True)
        invitation = serializer.validated_data["invitation"]
        return Response(
            {
                "valid": True,
                "email": invitation.email,
                "role": invitation.role,
                "expires_at": invitation.expires_at.isoformat(),
                "organization": invitation.organization.name,
            },
            status=200,
        )


class InvitationAcceptView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = InvitationAcceptSerializer

    def post(self, request, *args, **kwargs):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = serializer.save()
        return Response(result, status=status.HTTP_200_OK)


class TeamInvitationResendView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    serializer_class = TeamMemberSerializer

    def post(self, request, *args, **kwargs):
        user = get_object_or_404(User, pk=kwargs["pk"])
        membership = Membership.objects.filter(user=user, organization=request.organization).first()
        if not membership:
            raise PermissionDenied("This staff member does not belong to your organization.")
        if membership.role == Membership.Role.OWNER:
            raise PermissionDenied("The organization owner cannot be invited or resent.")

        invitation = StaffInvitation.objects.filter(
            organization=request.organization,
            invited_user=user,
            status=StaffInvitation.Status.PENDING,
        ).order_by("-created_at").first()
        if not invitation:
            return Response({"detail": "No pending invitation is available to resend."}, status=400)

        with transaction.atomic():
            invitation.status = StaffInvitation.Status.REVOKED
            invitation.save(update_fields=["status", "updated_at"])

            new_token = secrets.token_urlsafe(32)
            new_invitation = StaffInvitation.objects.create(
                organization=request.organization,
                invited_user=user,
                email=user.email,
                role=membership.role,
                token_hash=StaffInvitation.hash_token(new_token),
                status=StaffInvitation.Status.PENDING,
                expires_at=timezone.now() + timedelta(hours=settings.STAFF_INVITATION_EXPIRY_HOURS),
                created_by=request.user,
            )
            log_activity_event(
                organization=request.organization,
                actor=request.user,
                action="team.invitation_resent",
                entity_type="StaffInvitation",
                entity_id=str(new_invitation.pk),
                description=f"Resent invitation to {new_invitation.email}",
                metadata={"role": new_invitation.role},
            )

        invite_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/invite/{new_token}"
        name = user.get_full_name() or user.email
        role = membership.role.title()
        expiration = new_invitation.expires_at.strftime("%Y-%m-%d %H:%M %Z")
        subject = f"Your invitation to join {request.organization.name}"
        body = (
            f"Hello {name},\n\n"
            f"Your invitation to join {request.organization.name} as a {role} has been resent.\n\n"
            f"This invitation expires on {expiration}.\n\n"
            f"Accept invitation: {invite_url}\n\n"
            "Regards,\nInventory team"
        )
        send_product_email(
            subject=subject,
            recipient=user.email,
            text_body=body,
            greeting=f"Hello {name},",
            heading=f"Your invitation to {request.organization.name}",
            paragraphs=(
                f"Your invitation to join {request.organization.name} as a {role} has been resent.",
                f"Use the secure invitation link below to set up your account. This invitation expires on {expiration}.",
            ),
            cta_label="Accept invitation",
            cta_url=invite_url,
        )
        return Response({"detail": "Invitation resent successfully."}, status=200)


class TeamInvitationRevokeView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    serializer_class = TeamMemberSerializer

    def post(self, request, *args, **kwargs):
        user = get_object_or_404(User, pk=kwargs["pk"])
        membership = Membership.objects.filter(user=user, organization=request.organization).first()
        if not membership:
            raise PermissionDenied("This staff member does not belong to your organization.")
        if membership.role == Membership.Role.OWNER:
            raise PermissionDenied("The organization owner cannot be revoked.")

        invitation = StaffInvitation.objects.filter(
            organization=request.organization,
            invited_user=user,
            status=StaffInvitation.Status.PENDING,
        ).order_by("-created_at").first()
        if not invitation:
            return Response({"detail": "No pending invitation exists to revoke."}, status=400)

        invitation.status = StaffInvitation.Status.REVOKED
        with transaction.atomic():
            invitation.save(update_fields=["status", "updated_at"])
            log_activity_event(
                organization=request.organization,
                actor=request.user,
                action="team.invitation_revoked",
                entity_type="StaffInvitation",
                entity_id=str(invitation.pk),
                description=f"Revoked invitation for {invitation.email}",
            )
        return Response({"detail": "Invitation revoked successfully."}, status=200)


class TeamActivationView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    serializer_class = TeamMemberSerializer

    def post(self, request, *args, **kwargs):
        user = get_object_or_404(User, pk=kwargs["pk"])
        membership = Membership.objects.select_related("organization").filter(
            user=user,
            organization=request.organization,
        ).first()
        if not membership:
            raise PermissionDenied("This user is not part of your organization.")
        if membership.role == Membership.Role.OWNER:
            raise PermissionDenied("The organization owner cannot be deactivated.")

        if not membership.is_active or not user.is_active:
            with transaction.atomic():
                user.is_active = True
                user.save(update_fields=["is_active"])
                membership.is_active = True
                membership.save(update_fields=["is_active"])
                log_activity_event(
                    organization=request.organization,
                    actor=request.user,
                    action="team.member_activated",
                    entity_type="Membership",
                    entity_id=str(membership.pk),
                    description=f"Activated team member {user.email}",
                    metadata={"role": membership.role},
                )
        return Response({"detail": "Staff member activated."}, status=200)


class TeamDeactivationView(OrganizationContextMixin, generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    serializer_class = TeamMemberSerializer

    def post(self, request, *args, **kwargs):
        user = get_object_or_404(User, pk=kwargs["pk"])
        membership = Membership.objects.select_related("organization").filter(
            user=user,
            organization=request.organization,
        ).first()
        if not membership:
            raise PermissionDenied("This user is not part of your organization.")
        if membership.role == Membership.Role.OWNER:
            raise PermissionDenied("The organization owner cannot be deactivated.")

        if membership.is_active or user.is_active:
            with transaction.atomic():
                user.is_active = False
                user.save(update_fields=["is_active"])
                membership.is_active = False
                membership.save(update_fields=["is_active"])
                log_activity_event(
                    organization=request.organization,
                    actor=request.user,
                    action="team.member_deactivated",
                    entity_type="Membership",
                    entity_id=str(membership.pk),
                    description=f"Deactivated team member {user.email}",
                    metadata={"role": membership.role},
                )
        return Response({"detail": "Staff member deactivated."}, status=200)


class CurrentOrganizationView(OrganizationContextMixin, generics.RetrieveUpdateAPIView):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

    def get_permissions(self):
        permissions_list = [permissions.IsAuthenticated, IsOrganizationMember]
        if self.request.method in {"PATCH", "PUT", "DELETE"}:
            permissions_list.append(IsOwner)
        return [permission() for permission in permissions_list]

    def get_object(self):
        membership = getattr(self.request, "membership", None)
        if not membership or not membership.is_active:
            raise PermissionDenied("You do not have access to an active organization.")
        return membership.organization

    def perform_update(self, serializer):
        changed_fields = sorted(serializer.validated_data)
        with transaction.atomic():
            organization = serializer.save()
            log_activity_event(
                organization=organization,
                actor=self.request.user,
                action="organization.updated",
                entity_type="Organization",
                entity_id=str(organization.pk),
                description=f"Updated organization settings for {organization.name}",
                metadata={"changed_fields": changed_fields},
            )
