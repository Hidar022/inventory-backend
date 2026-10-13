from datetime import timedelta
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import BillingTransaction, OrganizationSubscription, SubscriptionPlan
from apps.billing.services import ensure_default_plans
from apps.organizations.models import Membership, Organization
from apps.platform_admin.models import PlatformAuditEvent
from apps.platform_admin.models import PlatformTeamInvitation
from apps.platform_admin.permissions import (
    PLATFORM_ADMIN_GROUP,
    IsPlatformFinanceReader,
    IsPlatformSupportOrFinanceReader,
    IsPlatformSupportReader,
    IsPlatformSuperAdmin,
    ROLE_REQUIRED_PERMISSION,
)
from apps.platform_admin.roles import PLATFORM_ROLE_GROUPS, PLATFORM_ROLE_CHOICES
from apps.platform_admin.serializers import (
    MembershipRoleUpdateSerializer,
    PlatformAuditEventSerializer,
    PlatformFinanceOverviewSerializer,
    PlatformOrganizationDetailSerializer,
    PlatformOrganizationListSerializer,
    PlatformOverviewQuerySerializer,
    PlatformOverviewSerializer,
    PlatformPaymentSerializer,
    PlatformPlanSerializer,
    PlatformPlanUpdateSerializer,
    PlatformSubscriptionSerializer,
    PlatformTeamInvitationAcceptSerializer,
    PlatformTeamInvitationCreateSerializer,
    PlatformTeamInvitationSerializer,
    PlatformTeamInvitationValidationSerializer,
    PlatformTeamMemberSerializer,
    PlatformTeamRoleUpdateSerializer,
    PlatformUserDetailSerializer,
    PlatformUserListSerializer,
    ReasonSerializer,
    SubscriptionAdjustmentSerializer,
)
from apps.platform_admin.services import (
    assert_organization_retains_active_owner,
    assert_other_active_platform_admin_remains,
    get_platform_overview,
    get_platform_finance_summary,
    record_platform_event,
)
from apps.platform_admin.team_services import (
    accept_platform_invitation,
    change_platform_role,
    create_platform_invitation,
    resend_platform_invitation,
    revoke_platform_invitation,
    revoke_platform_role,
)

User = get_user_model()


class PlatformReadOnlyListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]


class PlatformTeamMemberListView(PlatformReadOnlyListView):
    serializer_class = PlatformTeamMemberSerializer

    @extend_schema(
        operation_id="platform_team_members_list",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("role", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
        description="Super Admin only. Lists accounts with an explicitly assigned platform role.",
    )
    def get_queryset(self):
        role_query = Q()
        for role, group_name in PLATFORM_ROLE_GROUPS.items():
            role_query |= Q(
                groups__name=group_name,
                groups__permissions__content_type__app_label="platform_admin",
                groups__permissions__codename=ROLE_REQUIRED_PERMISSION[role],
            )
        queryset = User.objects.filter(role_query).distinct().order_by("email")
        search = (self.request.query_params.get("search") or "").strip()
        role = (self.request.query_params.get("role") or "").upper()
        status_filter = (self.request.query_params.get("status") or "").lower()
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        if role:
            valid_roles = {value for value, _label in PLATFORM_ROLE_CHOICES}
            if role not in valid_roles:
                raise ValidationError({"role": "Choose a valid platform role."})
            queryset = queryset.filter(
                groups__name=PLATFORM_ROLE_GROUPS[role],
                groups__permissions__codename=ROLE_REQUIRED_PERMISSION[role],
            )
        if status_filter == "active":
            queryset = queryset.filter(is_active=True)
        elif status_filter in {"inactive", "deactivated"}:
            queryset = queryset.filter(is_active=False)
        elif status_filter:
            raise ValidationError({"status": "Use active or deactivated."})
        return queryset.distinct()


class PlatformTeamInvitationListCreateView(PlatformReadOnlyListView):
    serializer_class = PlatformTeamInvitationSerializer

    @extend_schema(
        operation_id="platform_team_invitations_list",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
        description="Super Admin only. Lists platform team invitations without exposing token hashes or raw tokens.",
    )
    def get_queryset(self):
        queryset = PlatformTeamInvitation.objects.select_related("invited_user", "created_by")
        search = (self.request.query_params.get("search") or "").strip()
        status_filter = (self.request.query_params.get("status") or "").upper()
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        if status_filter:
            valid_statuses = {value for value, _label in PlatformTeamInvitation.Status.choices}
            if status_filter not in valid_statuses:
                raise ValidationError({"status": "Choose a valid invitation status."})
            queryset = queryset.filter(status=status_filter)
        return queryset

    @extend_schema(
        operation_id="platform_team_invitation_create",
        request=PlatformTeamInvitationCreateSerializer,
        responses={201: PlatformTeamInvitationSerializer},
        description="Super Admin only. Creates a one-time email invitation; role access is granted only after successful acceptance.",
    )
    def post(self, request):
        serializer = PlatformTeamInvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = create_platform_invitation(
            actor=request.user,
            email=serializer.validated_data["email"],
            first_name=serializer.validated_data.get("first_name", ""),
            last_name=serializer.validated_data.get("last_name", ""),
            role=serializer.validated_data["role"],
            reason=serializer.validated_data["reason"],
        )
        return Response(
            PlatformTeamInvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED,
        )


class PlatformTeamInvitationResendView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    serializer_class = ReasonSerializer

    @extend_schema(request=ReasonSerializer, responses=PlatformTeamInvitationSerializer)
    def post(self, request, invitation_id):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            invitation = resend_platform_invitation(
                actor=request.user,
                invitation_id=invitation_id,
                reason=serializer.validated_data["reason"],
            )
        except PlatformTeamInvitation.DoesNotExist as exc:
            raise ValidationError({"detail": "Invitation not found."}) from exc
        return Response(PlatformTeamInvitationSerializer(invitation).data)


class PlatformTeamInvitationRevokeView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    serializer_class = ReasonSerializer

    @extend_schema(request=ReasonSerializer, responses=PlatformTeamInvitationSerializer)
    def post(self, request, invitation_id):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            invitation = revoke_platform_invitation(
                actor=request.user,
                invitation_id=invitation_id,
                reason=serializer.validated_data["reason"],
            )
        except PlatformTeamInvitation.DoesNotExist as exc:
            raise ValidationError({"detail": "Invitation not found."}) from exc
        return Response(PlatformTeamInvitationSerializer(invitation).data)


class PlatformTeamRoleRevokeView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    serializer_class = ReasonSerializer

    @extend_schema(request=ReasonSerializer, responses=PlatformTeamMemberSerializer)
    def post(self, request, user_id):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_object_or_404(User, pk=user_id)
        user = revoke_platform_role(
            actor=request.user,
            user=user,
            reason=serializer.validated_data["reason"],
        )
        return Response(PlatformTeamMemberSerializer(user).data)


class PlatformTeamRoleUpdateView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    serializer_class = PlatformTeamRoleUpdateSerializer

    @extend_schema(
        request=PlatformTeamRoleUpdateSerializer,
        responses=PlatformTeamMemberSerializer,
        description="Super Admin only. Assigns an approved platform role and prevents removal of the final Super Admin.",
    )
    def patch(self, request, user_id):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_object_or_404(User, pk=user_id)
        user = change_platform_role(
            actor=request.user,
            user=user,
            new_role=serializer.validated_data["role"],
            reason=serializer.validated_data["reason"],
        )
        return Response(PlatformTeamMemberSerializer(user).data)


class PlatformTeamInvitationValidationView(APIView):
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        operation_id="platform_team_invitation_validate",
        parameters=[OpenApiParameter("token", OpenApiTypes.STR, OpenApiParameter.QUERY)],
        responses={200: OpenApiTypes.OBJECT},
        description="Validates a one-time invitation token without granting platform access.",
    )
    def get(self, request):
        serializer = PlatformTeamInvitationValidationSerializer(
            data={"token": request.query_params.get("token", "")},
        )
        serializer.is_valid(raise_exception=True)
        invitation = serializer.validated_data["invitation"]
        return Response(
            {
                "valid": True,
                "email": invitation.email,
                "first_name": invitation.first_name,
                "role": invitation.role,
                "expires_at": invitation.expires_at,
            }
        )


class PlatformTeamInvitationAcceptView(generics.GenericAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PlatformTeamInvitationAcceptSerializer

    @extend_schema(
        operation_id="platform_team_invitation_accept",
        request=PlatformTeamInvitationAcceptSerializer,
        responses={200: OpenApiTypes.OBJECT},
        description="Sets a password, activates the invited account, and grants its approved platform role only after token verification.",
    )
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invited_user = accept_platform_invitation(
            invitation=serializer.validated_data["invitation"],
            user=serializer.validated_data["user"],
            password=serializer.validated_data["password"],
        )
        return Response(
            {
                "detail": "Platform account setup completed. Sign in to continue.",
                "login_url": "/login",
                "email": invited_user.email,
                "platform_role": serializer.validated_data["invitation"].role,
            }
        )


def _organization_id_filter(queryset, value):
    if not value:
        return queryset
    try:
        organization_id = UUID(value)
    except (TypeError, ValueError):
        raise ValidationError({"organization": "Provide a valid organization ID."})
    return queryset.filter(organization_id=organization_id)


def _date_filter(queryset, request, *, field="created_at"):
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    parsed_from = parse_date(date_from) if date_from else None
    parsed_to = parse_date(date_to) if date_to else None
    if date_from and parsed_from is None:
        raise ValidationError({"date_from": "Use a valid YYYY-MM-DD date."})
    if date_to and parsed_to is None:
        raise ValidationError({"date_to": "Use a valid YYYY-MM-DD date."})
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise ValidationError({"date_range": "date_from must not be after date_to."})
    for parameter, lookup in (("date_from", "gte"), ("date_to", "lte")):
        value = request.query_params.get(parameter)
        if value:
            parsed = parsed_from if parameter == "date_from" else parsed_to
            queryset = queryset.filter(**{f"{field}__date__{lookup}": parsed})
    return queryset


class PlatformOverviewView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]

    @extend_schema(
        operation_id="platform_overview",
        parameters=[
            OpenApiParameter("date_from", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("date_to", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("currency", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
        responses=PlatformOverviewSerializer,
        description=(
            "Platform-wide counts and recent records. Revenue includes only "
            "fulfilled successful transactions paid within the selected dates "
            "and currency; the default period is the last 30 local calendar days."
        ),
    )
    def get(self, request):
        query = PlatformOverviewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        result = get_platform_overview(**query.validated_data)
        return Response(
            PlatformOverviewSerializer(result, context={"request": request}).data
        )


class PlatformFinanceOverviewView(PlatformOverviewView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformFinanceReader]

    @extend_schema(
        operation_id="platform_finance_overview",
        parameters=[
            OpenApiParameter("date_from", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("date_to", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("currency", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
        responses=PlatformFinanceOverviewSerializer,
        description="Finance-only aggregates. Revenue includes fulfilled successful transactions in the selected currency and date period; no organization or user-security metrics are returned.",
    )
    def get(self, request):
        query = PlatformOverviewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        summary = get_platform_finance_summary(**query.validated_data)
        return Response(PlatformFinanceOverviewSerializer(summary).data)


class PlatformOrganizationListView(PlatformReadOnlyListView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportReader]
    serializer_class = PlatformOrganizationListSerializer

    @extend_schema(
        operation_id="platform_organizations_list",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
        description="Paginated platform-wide organization search and status filtering.",
    )
    def get_queryset(self):
        queryset = Organization.objects.annotate(
            member_count=Count("memberships", distinct=True),
            active_member_count=Count(
                "memberships",
                filter=Q(memberships__is_active=True, memberships__user__is_active=True),
                distinct=True,
            ),
        )
        search = (self.request.query_params.get("search") or "").strip()
        status_filter = (self.request.query_params.get("status") or "").upper()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(slug__icontains=search)
                | Q(business_type__icontains=search)
            )
        if status_filter == "ACTIVE":
            queryset = queryset.filter(is_active=True)
        elif status_filter == "SUSPENDED":
            queryset = queryset.filter(is_active=False)
        elif status_filter:
            raise ValidationError({"status": "Use ACTIVE or SUSPENDED."})
        return queryset.order_by("-created_at", "name")


class PlatformOrganizationDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportReader]
    serializer_class = PlatformOrganizationDetailSerializer
    lookup_url_kwarg = "organization_id"

    @extend_schema(
        operation_id="platform_organizations_retrieve",
        description="Returns organization details, memberships, latest subscription, and safe payment summaries.",
    )
    def get_queryset(self):
        return Organization.objects.annotate(
            member_count=Count("memberships", distinct=True),
            active_member_count=Count(
                "memberships",
                filter=Q(memberships__is_active=True, memberships__user__is_active=True),
                distinct=True,
            ),
        ).prefetch_related("memberships__user")


class PlatformOrganizationStateView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    is_active = True
    action_name = "organization.reactivated"
    success_detail = "Organization reactivated."

    @extend_schema(
        request=ReasonSerializer,
        responses={200: OpenApiTypes.OBJECT},
        description="Suspend or reactivate an organization without deleting data. A reason is required.",
    )
    def post(self, request, organization_id):
        reason_input = ReasonSerializer(data=request.data)
        reason_input.is_valid(raise_exception=True)
        with transaction.atomic():
            organization = get_object_or_404(
                Organization.objects.select_for_update(),
                pk=organization_id,
            )
            before = {"is_active": organization.is_active}
            if organization.is_active != self.is_active:
                organization.is_active = self.is_active
                organization.save(update_fields=["is_active", "updated_at"])
                record_platform_event(
                    actor=request.user,
                    action=self.action_name,
                    target_type="Organization",
                    target_id=organization.pk,
                    organization=organization,
                    reason=reason_input.validated_data["reason"],
                    before=before,
                    after={"is_active": organization.is_active},
                )
        return Response(
            {
                "detail": self.success_detail,
                "organization": {
                    "id": str(organization.pk),
                    "name": organization.name,
                    "is_active": organization.is_active,
                },
            }
        )


class PlatformOrganizationSuspendView(PlatformOrganizationStateView):
    is_active = False
    action_name = "organization.suspended"
    success_detail = "Organization suspended. Business data was preserved."


class PlatformOrganizationReactivateView(PlatformOrganizationStateView):
    is_active = True
    action_name = "organization.reactivated"
    success_detail = "Organization reactivated."


class PlatformUserListView(PlatformReadOnlyListView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportReader]
    serializer_class = PlatformUserListSerializer

    @extend_schema(
        operation_id="platform_users_list",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("organization", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("role", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
        description="Paginated platform-wide user search; credentials and tokens are never serialized.",
    )
    def get_queryset(self):
        queryset = User.objects.prefetch_related(
            "organization_memberships__organization",
            "organization_memberships__user",
            "groups",
        ).order_by("-date_joined", "-pk")
        search = (self.request.query_params.get("search") or "").strip()
        status_filter = (self.request.query_params.get("status") or "").lower()
        role = (self.request.query_params.get("role") or "").upper()
        organization = self.request.query_params.get("organization")
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        if status_filter == "active":
            queryset = queryset.filter(is_active=True)
        elif status_filter in {"inactive", "deactivated"}:
            queryset = queryset.filter(is_active=False)
        elif status_filter:
            raise ValidationError({"status": "Use active or deactivated."})
        if role:
            if role not in Membership.Role.values:
                raise ValidationError({"role": "Choose a valid organization role."})
            queryset = queryset.filter(organization_memberships__role=role)
        if organization:
            try:
                organization_id = UUID(organization)
            except (TypeError, ValueError):
                raise ValidationError({"organization": "Provide a valid organization ID."})
            queryset = queryset.filter(organization_memberships__organization_id=organization_id)
        return queryset.distinct()


class PlatformUserDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportReader]
    serializer_class = PlatformUserDetailSerializer
    lookup_url_kwarg = "user_id"

    @extend_schema(operation_id="platform_users_retrieve")
    def get_queryset(self):
        return User.objects.prefetch_related(
            "organization_memberships__organization",
            "organization_memberships__user",
            "groups",
        )

    @extend_schema(
        request=None,
        responses={409: OpenApiTypes.OBJECT},
        description="Permanent deletion is disabled to preserve historical business and audit attribution. Deactivate instead.",
    )
    def delete(self, request, *args, **kwargs):
        self.get_object()
        return Response(
            {"detail": "Permanent deletion is unavailable because user history is retained. Deactivate the account instead."},
            status=status.HTTP_409_CONFLICT,
        )


class PlatformUserStateView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    serializer_class = ReasonSerializer
    is_active = True
    action_name = "user.reactivated"
    success_detail = "User account reactivated."

    @extend_schema(
        request=ReasonSerializer,
        responses={200: OpenApiTypes.OBJECT},
        description="Activate or deactivate an eligible user account.",
    )
    def post(self, request, user_id):
        reason_input = self.get_serializer(data=request.data)
        reason_input.is_valid(raise_exception=True)
        with transaction.atomic():
            if not self.is_active:
                from django.contrib.auth.models import Group

                Group.objects.select_for_update().filter(name=PLATFORM_ADMIN_GROUP).first()
            user = get_object_or_404(User.objects.select_for_update(), pk=user_id)
            before = {"is_active": user.is_active}
            if user.is_active != self.is_active:
                if not self.is_active:
                    assert_other_active_platform_admin_remains(user)
                    assert_organization_retains_active_owner(user)
                user.is_active = self.is_active
                user.save(update_fields=["is_active"])
                record_platform_event(
                    actor=request.user,
                    action=self.action_name,
                    target_type="User",
                    target_id=user.pk,
                    reason=reason_input.validated_data["reason"],
                    before=before,
                    after={"is_active": user.is_active},
                )
        return Response({"detail": self.success_detail, "is_active": user.is_active})


class PlatformUserDeactivateView(PlatformUserStateView):
    is_active = False
    action_name = "user.deactivated"
    success_detail = "User account deactivated. Existing and renewed JWT access will be denied."


class PlatformUserReactivateView(PlatformUserStateView):
    is_active = True
    action_name = "user.reactivated"
    success_detail = "User account reactivated. Membership and subscription rules still apply."


class PlatformMembershipRoleView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    serializer_class = MembershipRoleUpdateSerializer

    @extend_schema(
        request=MembershipRoleUpdateSerializer,
        responses={200: OpenApiTypes.OBJECT},
        description="Change an existing membership role. This endpoint does not reassign organizations or grant ownership.",
    )
    def patch(self, request, membership_id):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_role = serializer.validated_data["role"]
        if new_role == Membership.Role.OWNER:
            raise ValidationError({"role": "Ownership transfer is not supported by this endpoint."})

        with transaction.atomic():
            membership = get_object_or_404(
                Membership.objects.select_for_update().select_related("organization", "user"),
                pk=membership_id,
            )
            Organization.objects.select_for_update().get(pk=membership.organization_id)
            before = {"role": membership.role, "is_active": membership.is_active}
            if membership.role == Membership.Role.OWNER:
                other_owner_exists = Membership.objects.select_for_update().filter(
                    organization=membership.organization,
                    role=Membership.Role.OWNER,
                    is_active=True,
                    user__is_active=True,
                ).exclude(pk=membership.pk).exists()
                if membership.is_active and membership.user.is_active and not other_owner_exists:
                    raise ValidationError({"role": "Transfer ownership before changing the last active owner."})
            if membership.role != new_role:
                membership.role = new_role
                membership.save(update_fields=["role"])
                record_platform_event(
                    actor=request.user,
                    action="membership.role_changed",
                    target_type="Membership",
                    target_id=membership.pk,
                    organization=membership.organization,
                    reason=serializer.validated_data["reason"],
                    before=before,
                    after={"role": membership.role, "is_active": membership.is_active},
                )
        return Response({"id": str(membership.pk), "role": membership.role})


class PlatformSubscriptionListView(PlatformReadOnlyListView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportOrFinanceReader]
    serializer_class = PlatformSubscriptionSerializer

    @extend_schema(
        operation_id="platform_subscriptions_list",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("organization", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
    )
    def get_queryset(self):
        queryset = OrganizationSubscription.objects.select_related(
            "organization",
            "plan",
        ).prefetch_related("transactions")
        search = (self.request.query_params.get("search") or "").strip()
        status_filter = (self.request.query_params.get("status") or "").upper()
        if search:
            queryset = queryset.filter(organization__name__icontains=search)
        if status_filter:
            valid_statuses = {value for value, _label in OrganizationSubscription.Status.choices}
            if status_filter not in valid_statuses:
                raise ValidationError({"status": "Choose a valid subscription status."})
            if status_filter == OrganizationSubscription.Status.EXPIRED:
                queryset = queryset.filter(
                    Q(status=OrganizationSubscription.Status.EXPIRED)
                    | Q(status=OrganizationSubscription.Status.ACTIVE, expiry_date__lte=timezone.now())
                )
            elif status_filter == OrganizationSubscription.Status.ACTIVE:
                queryset = queryset.filter(
                    status=OrganizationSubscription.Status.ACTIVE,
                    expiry_date__gt=timezone.now(),
                )
            else:
                queryset = queryset.filter(status=status_filter)
        return _organization_id_filter(
            queryset,
            self.request.query_params.get("organization"),
        ).order_by("-start_date", "-created_at")


class PlatformSubscriptionDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportOrFinanceReader]
    serializer_class = PlatformSubscriptionSerializer
    lookup_url_kwarg = "subscription_id"
    queryset = OrganizationSubscription.objects.select_related(
        "organization",
        "plan",
    ).prefetch_related("transactions")


class PlatformSubscriptionAdjustView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]

    @extend_schema(
        request=SubscriptionAdjustmentSerializer,
        responses=PlatformSubscriptionSerializer,
        description="Adjust expiry by a bounded number of days. Requires a reason; does not create or fulfill a payment.",
    )
    def post(self, request, subscription_id):
        serializer = SubscriptionAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        days = serializer.validated_data["days"]
        reason = serializer.validated_data["reason"]
        with transaction.atomic():
            subscription = get_object_or_404(
                OrganizationSubscription.objects.select_for_update().select_related("organization", "plan"),
                pk=subscription_id,
            )
            if subscription.status not in {
                OrganizationSubscription.Status.ACTIVE,
                OrganizationSubscription.Status.EXPIRED,
            }:
                raise ValidationError({"status": "Only active or expired subscriptions can be adjusted."})
            before = {
                "status": subscription.status,
                "start_date": subscription.start_date.isoformat(),
                "expiry_date": subscription.expiry_date.isoformat(),
            }
            subscription.expiry_date += timedelta(days=days)
            subscription.status = (
                OrganizationSubscription.Status.ACTIVE
                if subscription.expiry_date > timezone.now()
                else OrganizationSubscription.Status.EXPIRED
            )
            subscription.save(update_fields=["expiry_date", "status", "updated_at"])
            record_platform_event(
                actor=request.user,
                action="subscription.adjusted",
                target_type="OrganizationSubscription",
                target_id=subscription.pk,
                organization=subscription.organization,
                reason=reason,
                before=before,
                after={
                    "status": subscription.status,
                    "start_date": subscription.start_date.isoformat(),
                    "expiry_date": subscription.expiry_date.isoformat(),
                    "days_adjusted": days,
                },
            )
        return Response(PlatformSubscriptionSerializer(subscription).data)


class PlatformPlanListView(PlatformReadOnlyListView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportOrFinanceReader]
    serializer_class = PlatformPlanSerializer

    @extend_schema(operation_id="platform_plans_list")
    def get_queryset(self):
        ensure_default_plans()
        queryset = SubscriptionPlan.objects.all()
        search = (self.request.query_params.get("search") or "").strip()
        status_filter = (self.request.query_params.get("status") or "").lower()
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(code__icontains=search))
        if status_filter == "active":
            queryset = queryset.filter(is_active=True)
        elif status_filter in {"inactive", "deactivated"}:
            queryset = queryset.filter(is_active=False)
        elif status_filter:
            raise ValidationError({"status": "Use active or deactivated."})
        return queryset.order_by("sort_order", "months", "code")


class PlatformPlanDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSupportOrFinanceReader]
    serializer_class = PlatformPlanSerializer
    lookup_url_kwarg = "plan_code"
    queryset = SubscriptionPlan.objects.all()

    @extend_schema(
        request=PlatformPlanUpdateSerializer,
        responses=PlatformPlanSerializer,
        description="Updates descriptive fields only. Price, currency, code, and duration are immutable in this phase.",
    )
    def patch(self, request, plan_code):
        serializer = PlatformPlanUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            plan = get_object_or_404(SubscriptionPlan.objects.select_for_update(), code=plan_code)
            values = serializer.validated_data.copy()
            reason = values.pop("reason")
            before = {key: getattr(plan, key) for key in values}
            for key, value in values.items():
                setattr(plan, key, value)
            if values:
                plan.save(update_fields=[*values.keys(), "updated_at"])
                record_platform_event(
                    actor=request.user,
                    action="plan.updated",
                    target_type="SubscriptionPlan",
                    target_id=plan.code,
                    reason=reason,
                    before=before,
                    after={key: getattr(plan, key) for key in values},
                )
        return Response(PlatformPlanSerializer(plan).data)


class PlatformPlanStateView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformSuperAdmin]
    is_active = True
    action_name = "plan.activated"
    detail = "Plan activated."

    @extend_schema(request=ReasonSerializer, responses=PlatformPlanSerializer)
    def post(self, request, plan_code):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            plan = get_object_or_404(SubscriptionPlan.objects.select_for_update(), code=plan_code)
            before = {"is_active": plan.is_active}
            if plan.is_active != self.is_active:
                plan.is_active = self.is_active
                plan.save(update_fields=["is_active", "updated_at"])
                record_platform_event(
                    actor=request.user,
                    action=self.action_name,
                    target_type="SubscriptionPlan",
                    target_id=plan.code,
                    reason=serializer.validated_data["reason"],
                    before=before,
                    after={"is_active": plan.is_active},
                )
        return Response({"detail": self.detail, "plan": PlatformPlanSerializer(plan).data})


class PlatformPlanDeactivateView(PlatformPlanStateView):
    is_active = False
    action_name = "plan.deactivated"
    detail = "Plan deactivated. Existing subscriptions and pending transactions are preserved."


class PlatformPlanReactivateView(PlatformPlanStateView):
    is_active = True
    action_name = "plan.activated"
    detail = "Plan activated."


class PlatformPaymentListView(PlatformReadOnlyListView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformFinanceReader]
    serializer_class = PlatformPaymentSerializer

    @extend_schema(
        operation_id="platform_payments_list",
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("organization", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("kind", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("date_from", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("date_to", OpenApiTypes.DATE, OpenApiParameter.QUERY),
        ],
        description="Read-only cross-organization transaction history. Paystack credentials and raw metadata are excluded.",
    )
    def get_queryset(self):
        queryset = BillingTransaction.objects.select_related(
            "organization",
            "subscription",
            "subscription__plan",
        )
        search = (self.request.query_params.get("search") or "").strip()
        transaction_status = (self.request.query_params.get("status") or "").upper()
        kind = (self.request.query_params.get("kind") or "").upper()
        if search:
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(organization__name__icontains=search)
            )
        if transaction_status:
            valid = {value for value, _label in BillingTransaction.Status.choices}
            if transaction_status not in valid:
                raise ValidationError({"status": "Choose a valid payment status."})
            queryset = queryset.filter(status=transaction_status)
        if kind:
            valid = {value for value, _label in BillingTransaction.Kind.choices}
            if kind not in valid:
                raise ValidationError({"kind": "Choose a valid transaction type."})
            queryset = queryset.filter(kind=kind)
        queryset = _organization_id_filter(
            queryset,
            self.request.query_params.get("organization"),
        )
        return _date_filter(queryset, self.request).order_by("-created_at", "-id")


class PlatformAuditLogListView(PlatformReadOnlyListView):
    serializer_class = PlatformAuditEventSerializer

    @extend_schema(
        operation_id="platform_audit_list",
        parameters=[
            OpenApiParameter("action", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("organization", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("date_from", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("date_to", OpenApiTypes.DATE, OpenApiParameter.QUERY),
        ],
        description="Read-only platform administrative audit events; tenant activity feeds do not include these records.",
    )
    def get_queryset(self):
        queryset = PlatformAuditEvent.objects.select_related("actor", "organization")
        action = (self.request.query_params.get("action") or "").strip()
        if action:
            queryset = queryset.filter(action=action)
        queryset = _organization_id_filter(
            queryset,
            self.request.query_params.get("organization"),
        )
        return _date_filter(queryset, self.request).order_by("-created_at", "-id")