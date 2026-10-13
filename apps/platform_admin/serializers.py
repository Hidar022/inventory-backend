from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from apps.billing.models import BillingTransaction, OrganizationSubscription, SubscriptionPlan
from apps.organizations.models import Membership, Organization
from apps.platform_admin.models import PlatformAuditEvent, PlatformTeamInvitation
from apps.platform_admin.roles import PLATFORM_ROLE_CHOICES

User = get_user_model()


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=8, max_length=1000, trim_whitespace=True)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A meaningful reason is required.")
        return value.strip()


class SubscriptionAdjustmentSerializer(ReasonSerializer):
    days = serializers.IntegerField(min_value=-3650, max_value=3650)

    def validate_days(self, value):
        if value == 0:
            raise serializers.ValidationError("Choose a non-zero number of days.")
        return value


class MembershipRoleUpdateSerializer(ReasonSerializer):
    role = serializers.ChoiceField(choices=Membership.Role.choices)


class PlatformMembershipSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    name = serializers.SerializerMethodField()
    organization_id = serializers.UUIDField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = Membership
        fields = (
            "id",
            "user_id",
            "email",
            "name",
            "organization_id",
            "organization_name",
            "role",
            "is_active",
            "created_at",
        )

    @extend_schema_field(serializers.CharField())
    def get_name(self, obj):
        return obj.user.get_full_name() or obj.user.email


class PlatformOrganizationListSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(read_only=True)
    active_member_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "business_type",
            "currency",
            "is_active",
            "member_count",
            "active_member_count",
            "created_at",
        )


class PlatformSubscriptionSerializer(serializers.ModelSerializer):
    organization_id = serializers.UUIDField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    plan_code = serializers.CharField(source="plan.code", read_only=True)
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    effective_status = serializers.SerializerMethodField()
    days_remaining = serializers.SerializerMethodField()
    recent_payments = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationSubscription
        fields = (
            "id",
            "organization_id",
            "organization_name",
            "plan_code",
            "plan_name",
            "status",
            "effective_status",
            "is_setup",
            "start_date",
            "expiry_date",
            "last_paid_at",
            "days_remaining",
            "created_at",
            "recent_payments",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_effective_status(self, obj):
        from django.utils import timezone

        if obj.status == OrganizationSubscription.Status.ACTIVE and obj.expiry_date <= timezone.now():
            return OrganizationSubscription.Status.EXPIRED
        return obj.status

    @extend_schema_field(serializers.IntegerField())
    def get_days_remaining(self, obj):
        return obj.days_remaining

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_recent_payments(self, obj):
        from apps.platform_admin.permissions import user_platform_role
        from apps.platform_admin.roles import PLATFORM_FINANCE, PLATFORM_SUPER_ADMIN

        request = self.context.get("request")
        role = user_platform_role(request.user) if request else None
        if role not in {PLATFORM_FINANCE, PLATFORM_SUPER_ADMIN}:
            return []
        transactions = obj.transactions.select_related(
            "organization",
            "subscription",
            "subscription__plan",
        ).order_by("-created_at")[:10]
        return PlatformPaymentSerializer(transactions, many=True).data


class PlatformPaymentSerializer(serializers.ModelSerializer):
    organization_id = serializers.UUIDField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    subscription_id = serializers.UUIDField(read_only=True, allow_null=True)
    subscription_status = serializers.CharField(
        source="subscription.status",
        read_only=True,
        allow_null=True,
    )
    subscription_plan = serializers.CharField(
        source="subscription.plan.name",
        read_only=True,
        allow_null=True,
    )
    subscription_expiry_date = serializers.DateTimeField(
        source="subscription.expiry_date",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = BillingTransaction
        fields = (
            "id",
            "organization_id",
            "organization_name",
            "reference",
            "amount",
            "currency",
            "kind",
            "status",
            "created_at",
            "paid_at",
            "fulfilled_at",
            "subscription_id",
            "subscription_status",
            "subscription_plan",
            "subscription_expiry_date",
        )
        read_only_fields = fields


class PlatformOrganizationDetailSerializer(PlatformOrganizationListSerializer):
    memberships = PlatformMembershipSerializer(many=True, read_only=True)
    latest_subscription = serializers.SerializerMethodField()
    recent_payments = serializers.SerializerMethodField()

    class Meta(PlatformOrganizationListSerializer.Meta):
        fields = PlatformOrganizationListSerializer.Meta.fields + (
            "timezone",
            "updated_at",
            "memberships",
            "latest_subscription",
            "recent_payments",
        )

    @extend_schema_field(PlatformSubscriptionSerializer(allow_null=True))
    def get_latest_subscription(self, obj):
        subscription = (
            obj.subscriptions.select_related("plan")
            .order_by("-start_date", "-created_at")
            .first()
        )
        return (
            PlatformSubscriptionSerializer(subscription, context=self.context).data
            if subscription
            else None
        )

    @extend_schema_field(PlatformPaymentSerializer(many=True))
    def get_recent_payments(self, obj):
        from apps.platform_admin.permissions import user_platform_role
        from apps.platform_admin.roles import PLATFORM_FINANCE, PLATFORM_SUPER_ADMIN

        role = user_platform_role(self.context["request"].user)
        if role not in {PLATFORM_FINANCE, PLATFORM_SUPER_ADMIN}:
            return []
        payments = obj.billing_transactions.select_related("organization", "subscription").order_by("-created_at")[:5]
        return PlatformPaymentSerializer(payments, many=True).data


class PlatformUserListSerializer(serializers.ModelSerializer):
    memberships = PlatformMembershipSerializer(
        source="organization_memberships",
        many=True,
        read_only=True,
    )
    is_platform_admin = serializers.SerializerMethodField()
    platform_role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_platform_admin",
            "platform_role",
            "date_joined",
            "last_login",
            "memberships",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_is_platform_admin(self, obj):
        from apps.platform_admin.permissions import user_has_platform_access

        return user_has_platform_access(obj)

    def get_platform_role(self, obj):
        from apps.platform_admin.permissions import assigned_platform_roles

        roles = assigned_platform_roles(obj)
        return roles[0] if roles else None


class PlatformUserDetailSerializer(PlatformUserListSerializer):
    pass


class PlatformPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPlan
        fields = ("code", "name", "description", "months", "amount", "currency", "is_active", "sort_order", "created_at", "updated_at")
        read_only_fields = ("code", "months", "amount", "currency", "created_at", "updated_at")


class PlatformPlanUpdateSerializer(ReasonSerializer):
    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(max_length=255, allow_blank=True, required=False)
    sort_order = serializers.IntegerField(min_value=0, required=False)

    def validate(self, attrs):
        unexpected = set(self.initial_data) - {"reason", "name", "description", "sort_order"}
        if unexpected:
            raise serializers.ValidationError(
                {field: "This plan field is locked by platform pricing policy." for field in unexpected}
            )
        if not any(key in attrs for key in ("name", "description", "sort_order")):
            raise serializers.ValidationError("Provide at least one permitted plan field.")
        return attrs


class PlatformAuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True, allow_null=True)

    class Meta:
        model = PlatformAuditEvent
        fields = (
            "id",
            "actor_id",
            "actor_email",
            "action",
            "target_type",
            "target_id",
            "organization_id",
            "organization_name",
            "reason",
            "before",
            "after",
            "created_at",
        )
        read_only_fields = fields


class PlatformOverviewSerializer(serializers.Serializer):
    organizations_total = serializers.IntegerField()
    organizations_active = serializers.IntegerField()
    organizations_suspended = serializers.IntegerField()
    users_total = serializers.IntegerField()
    users_active = serializers.IntegerField()
    users_deactivated = serializers.IntegerField()
    subscriptions_active = serializers.IntegerField()
    subscriptions_expired = serializers.IntegerField()
    subscriptions_expiring_30_days = serializers.IntegerField()
    payments_pending = serializers.IntegerField()
    payments_successful = serializers.IntegerField()
    revenue_received = serializers.DecimalField(max_digits=18, decimal_places=2)
    currency = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    recent_organizations = PlatformOrganizationListSerializer(many=True)
    recent_payments = PlatformPaymentSerializer(many=True)
    subscriptions_requiring_attention = PlatformSubscriptionSerializer(many=True)
    recent_administrative_activity = PlatformAuditEventSerializer(many=True)


class PlatformOverviewQuerySerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    currency = serializers.RegexField(r"^[A-Za-z]{3}$", default="NGN")

    def validate(self, attrs):
        from datetime import timedelta

        from django.utils import timezone

        today = timezone.localdate()
        attrs.setdefault("date_to", today)
        attrs.setdefault("date_from", today - timedelta(days=29))
        attrs["currency"] = attrs["currency"].upper()
        if attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError("date_from must not be after date_to.")
        if attrs["date_to"] > today:
            raise serializers.ValidationError("date_to cannot be in the future.")
        return attrs


class PlatformFinanceOverviewSerializer(serializers.Serializer):
    payments_pending = serializers.IntegerField()
    payments_successful = serializers.IntegerField()
    revenue_received = serializers.DecimalField(max_digits=18, decimal_places=2)
    currency = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()


class PlatformTeamInvitationCreateSerializer(ReasonSerializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    last_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    role = serializers.ChoiceField(choices=PLATFORM_ROLE_CHOICES)

    def validate_email(self, value):
        return value.strip().lower()

    def validate(self, attrs):
        from django.contrib.auth import get_user_model

        from apps.platform_admin.permissions import user_has_platform_access

        user = get_user_model().objects.filter(email=attrs["email"]).first()
        if user and user_has_platform_access(user):
            raise serializers.ValidationError(
                {"email": "This user already has platform access. Use the role review workflow instead."}
            )
        from django.utils import timezone

        if PlatformTeamInvitation.objects.filter(
            email=attrs["email"],
            status=PlatformTeamInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).exists():
            raise serializers.ValidationError(
                {"email": "A pending platform invitation already exists for this address."}
            )
        return attrs


class PlatformTeamRoleUpdateSerializer(ReasonSerializer):
    role = serializers.ChoiceField(choices=PLATFORM_ROLE_CHOICES)


class PlatformTeamInvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        token = attrs["token"]
        invitation = PlatformTeamInvitation.objects.select_related("invited_user").filter(
            token_hash=PlatformTeamInvitation.hash_token(token),
        ).first()
        if not invitation:
            raise serializers.ValidationError(
                {"token": "This platform invitation is invalid or has already been used."}
            )
        if invitation.status != PlatformTeamInvitation.Status.PENDING:
            raise serializers.ValidationError({"token": "This invitation is no longer pending."})
        if invitation.is_expired:
            invitation.status = PlatformTeamInvitation.Status.EXPIRED
            invitation.save(update_fields=["status", "updated_at"])
            raise serializers.ValidationError({"token": "This platform invitation has expired."})
        user = invitation.invited_user
        if user.email.strip().lower() != invitation.email.strip().lower():
            raise serializers.ValidationError(
                {"token": "The account email no longer matches this invitation."}
            )
        try:
            validate_password(attrs["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        attrs["invitation"] = invitation
        attrs["user"] = user
        return attrs


class PlatformTeamInvitationValidationSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True)

    def validate(self, attrs):
        invitation = PlatformTeamInvitation.objects.select_related("invited_user").filter(
            token_hash=PlatformTeamInvitation.hash_token(attrs["token"]),
        ).first()
        if not invitation:
            raise serializers.ValidationError({"detail": "Invitation not found or invalid."})
        if invitation.status != PlatformTeamInvitation.Status.PENDING:
            raise serializers.ValidationError({"detail": "This invitation is no longer pending."})
        if invitation.is_expired:
            raise serializers.ValidationError({"detail": "This platform invitation has expired."})
        attrs["invitation"] = invitation
        return attrs


class PlatformTeamMemberSerializer(serializers.ModelSerializer):
    platform_role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "platform_role",
            "date_joined",
            "last_login",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_platform_role(self, obj):
        from apps.platform_admin.permissions import assigned_platform_roles

        roles = assigned_platform_roles(obj)
        return roles[0] if roles else None


class PlatformTeamInvitationSerializer(serializers.ModelSerializer):
    platform_role = serializers.CharField(source="role", read_only=True)
    status = serializers.SerializerMethodField()
    invited_user_active = serializers.BooleanField(source="invited_user.is_active", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)

    class Meta:
        model = PlatformTeamInvitation
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "platform_role",
            "status",
            "invited_user_active",
            "created_by_email",
            "expires_at",
            "accepted_at",
            "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_status(self, obj):
        if obj.status == PlatformTeamInvitation.Status.PENDING and obj.is_expired:
            return PlatformTeamInvitation.Status.EXPIRED
        return obj.status