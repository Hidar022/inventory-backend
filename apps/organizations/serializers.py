import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers

from apps.organizations.models import Membership, Organization, StaffInvitation

User = get_user_model()


class OrganizationSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "business_type", "currency", "timezone"]


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "slug",
            "business_type",
            "currency",
            "timezone",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "is_active", "created_at", "updated_at"]


class TeamMemberSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="user.id", read_only=True)
    name = serializers.SerializerMethodField()
    email = serializers.EmailField(source="user.email", read_only=True)
    role = serializers.CharField(read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    account_active = serializers.BooleanField(source="user.is_active", read_only=True)
    invitation_status = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Membership
        fields = [
            "id",
            "name",
            "email",
            "role",
            "is_active",
            "account_active",
            "invitation_status",
            "created_at",
        ]

    def get_name(self, obj):
        return obj.user.get_full_name() or obj.user.email

    def get_invitation_status(self, obj):
        invitation = (
            StaffInvitation.objects.filter(
                invited_user=obj.user,
                organization=obj.organization,
            )
            .order_by("-created_at")
            .first()
        )
        if invitation:
            return invitation.status
        if obj.user.is_active:
            return "ACTIVE"
        return "INACTIVE"


class OrganizationCreateSerializer(serializers.ModelSerializer):
    business_type = serializers.CharField(required=False, allow_blank=True, max_length=100)
    currency = serializers.CharField(required=True, min_length=3, max_length=3)

    class Meta:
        model = Organization
        fields = ["name", "business_type", "currency"]

    def validate_name(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Organization name is required.")
        return cleaned

    def validate_currency(self, value):
        currency = value.strip().upper()
        if len(currency) != 3:
            raise serializers.ValidationError("Currency must be a 3-letter ISO code.")
        return currency

    def create(self, validated_data):
        user = self.context["request"].user
        base_name = validated_data["name"]
        slug_base = slugify(base_name) or "organization"
        slug = slug_base
        counter = 1

        while Organization.objects.filter(slug=slug).exists():
            slug = f"{slug_base}-{counter}"
            counter += 1

        with transaction.atomic():
            organization = Organization.objects.create(
                name=base_name,
                slug=slug,
                business_type=validated_data.get("business_type", "").strip(),
                currency=validated_data.get("currency", "NGN").upper(),
                timezone="Africa/Lagos",
            )
            Membership.objects.create(
                user=user,
                organization=organization,
                role=Membership.Role.OWNER,
                is_active=True,
            )

        return organization


class TeamCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=[Membership.Role.MANAGER, Membership.Role.CASHIER])

    def validate_email(self, value):
        normalized = value.strip().lower()
        if User.objects.filter(email=normalized).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return normalized

    def validate(self, attrs):
        organization = self.context["request"].organization
        if not organization:
            raise serializers.ValidationError("You must belong to an active organization.")

        email = attrs["email"]
        if StaffInvitation.objects.filter(
            organization=organization,
            email=email,
            status=StaffInvitation.Status.PENDING,
        ).exists():
            raise serializers.ValidationError({"email": "There is already a pending invitation for this email."})

        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        organization = request.organization
        first_name, _, last_name = validated_data["name"].strip().partition(" ")
        if not first_name:
            raise serializers.ValidationError({"name": "Name cannot be blank."})

        user = User.objects.create_user(
            email=validated_data["email"],
            password=secrets.token_urlsafe(24),
            first_name=first_name,
            last_name=last_name,
            is_active=False,
        )

        Membership.objects.create(
            user=user,
            organization=organization,
            role=validated_data["role"],
            is_active=False,
        )

        token = secrets.token_urlsafe(32)
        expires_at = timezone.now() + timedelta(hours=settings.STAFF_INVITATION_EXPIRY_HOURS)
        invitation = StaffInvitation.objects.create(
            organization=organization,
            invited_user=user,
            email=user.email,
            role=validated_data["role"],
            token_hash=StaffInvitation.hash_token(token),
            status=StaffInvitation.Status.PENDING,
            expires_at=expires_at,
            created_by=request.user,
        )

        self.send_invitation_email(request, invitation, token)
        return {"user": user, "invitation": invitation}

    def send_invitation_email(self, request, invitation, raw_token):
        from django.core.mail import EmailMultiAlternatives

        base_url = settings.FRONTEND_BASE_URL.rstrip("/")
        invite_url = f"{base_url}/invite/{raw_token}"
        subject = f"You are invited to join {invitation.organization.name}"
        body = (
            f"Hello {invitation.invited_user.get_full_name() or invitation.email},\n\n"
            f"{request.user.get_full_name() or request.user.email} invited you to join "
            f"{invitation.organization.name} as a {invitation.role.title()}.\n\n"
            f"Please set up your account here: {invite_url}\n\n"
            f"This invitation expires on {invitation.expires_at.strftime('%Y-%m-%d %H:%M %Z')}."
        )
        html_body = (
            f"<p>Hello {invitation.invited_user.get_full_name() or invitation.email},</p>"
            f"<p>{request.user.get_full_name() or request.user.email} invited you to join "
            f"{invitation.organization.name} as a {invitation.role.title()}.</p>"
            f"<p><a href='{invite_url}'>Set up your account</a></p>"
            f"<p>This invitation expires on {invitation.expires_at.strftime('%Y-%m-%d %H:%M %Z')}.</p>"
        )
        email = EmailMultiAlternatives(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [invitation.email],
        )
        email.attach_alternative(html_body, "text/html")
        email.send()


class InvitationValidationSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True)

    def validate(self, attrs):
        token = attrs["token"]
        invitation = StaffInvitation.objects.select_related("organization", "invited_user").filter(
            token_hash=StaffInvitation.hash_token(token),
        ).first()
        if not invitation:
            raise serializers.ValidationError({"detail": "Invitation not found or invalid."})
        if invitation.status == StaffInvitation.Status.ACCEPTED:
            raise serializers.ValidationError({"detail": "This invitation has already been accepted."})
        if invitation.status == StaffInvitation.Status.REVOKED:
            raise serializers.ValidationError({"detail": "This invitation has been revoked."})
        if invitation.status == StaffInvitation.Status.EXPIRED or invitation.is_expired:
            if invitation.status != StaffInvitation.Status.EXPIRED:
                invitation.status = StaffInvitation.Status.EXPIRED
                invitation.save(update_fields=["status", "updated_at"])
            raise serializers.ValidationError({"detail": "This invitation has expired."})
        if invitation.status != StaffInvitation.Status.PENDING:
            raise serializers.ValidationError({"detail": "This invitation is no longer valid."})

        attrs["invitation"] = invitation
        return attrs


class InvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        token = attrs["token"]
        invitation = StaffInvitation.objects.select_related("organization", "invited_user").filter(
            token_hash=StaffInvitation.hash_token(token),
        ).first()
        if not invitation:
            raise serializers.ValidationError({"token": "Invitation is invalid or has already been used."})
        if invitation.status != StaffInvitation.Status.PENDING:
            raise serializers.ValidationError({"token": "This invitation is no longer pending."})
        if invitation.is_expired:
            invitation.status = StaffInvitation.Status.EXPIRED
            invitation.save(update_fields=["status", "updated_at"])
            raise serializers.ValidationError({"token": "This invitation has expired."})

        user = invitation.invited_user
        if not user:
            raise serializers.ValidationError({"token": "Invitation is missing the invited user."})

        try:
            validate_password(attrs["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc

        attrs["invitation"] = invitation
        attrs["user"] = user
        return attrs

    def create(self, validated_data):
        invitation = validated_data["invitation"]
        user = validated_data["user"]
        password = validated_data["password"]

        with transaction.atomic():
            user.set_password(password)
            user.is_active = True
            user.save(update_fields=["password", "is_active"])

            membership = Membership.objects.select_related("organization").filter(
                user=user,
                organization=invitation.organization,
            ).first()
            if not membership:
                raise serializers.ValidationError({"detail": "No active membership was found for this invitation."})

            membership.is_active = True
            membership.save(update_fields=["is_active"])

            invitation.status = StaffInvitation.Status.ACCEPTED
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["status", "accepted_at", "updated_at"])

        return {
            "detail": "Account setup completed successfully.",
            "login_url": "/login",
        }
