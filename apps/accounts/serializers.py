import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import PasswordResetToken
from apps.organizations.models import Membership
from apps.organizations.serializers import OrganizationSummarySerializer
from common.email import send_product_email

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["id", "email", "password", "first_name", "last_name"]
        read_only_fields = ["id"]

    def validate_email(self, value):
        return value.strip().lower()

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        with transaction.atomic():
            user = User.objects.create_user(password=password, **validated_data)
        self.send_welcome_email(user)
        return user

    def send_welcome_email(self, user):
        if not user.email:
            return
        name = user.get_full_name() or user.email
        login_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/login"
        subject = "Your Inventory account is ready"
        body = (
            f"Hello {name},\n\n"
            "Your account has been created successfully. You are ready to set up your inventory business.\n\n"
            "Next, sign in to create your organization and add your business details.\n\n"
            f"Set up your business: {login_url}\n\n"
            "Regards,\nInventory team"
        )
        send_product_email(
            subject=subject,
            recipient=user.email,
            text_body=body,
            greeting=f"Hello {name},",
            heading="Your account is ready",
            paragraphs=(
                "Your account has been created successfully. You are ready to set up your inventory business.",
                "Sign in to create your organization and add your business details.",
            ),
            cta_label="Set up your business",
            cta_url=login_url,
        )


class UserSerializer(serializers.ModelSerializer):
    organization = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "organization", "role"]

    def get_organization(self, obj):
        membership = Membership.objects.select_related("organization").filter(
            user=obj,
            is_active=True,
        ).first()
        if not membership:
            return None
        return OrganizationSummarySerializer(membership.organization).data

    def get_role(self, obj):
        membership = Membership.objects.filter(user=obj, is_active=True).first()
        if not membership:
            return None
        return membership.role.lower()


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = "email"

    def validate(self, attrs):
        data = super().validate(attrs)
        return data


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.strip().lower()

    def save(self, **kwargs):
        email = self.validated_data["email"]
        user = User.objects.filter(email=email).first()

        if not user:
            return None

        PasswordResetToken.objects.filter(
            user=user,
            used_at__isnull=True,
        ).update(used_at=timezone.now())

        token = secrets.token_urlsafe(32)
        expires_at = timezone.now() + timedelta(
            hours=settings.PASSWORD_RESET_EXPIRY_HOURS,
        )
        reset_token = PasswordResetToken.objects.create(
            user=user,
            token_hash=PasswordResetToken.hash_token(token),
            expires_at=expires_at,
        )
        self.send_reset_email(user, reset_token, token)
        return reset_token

    def send_reset_email(self, user, reset_token, raw_token):
        base_url = settings.FRONTEND_BASE_URL.rstrip("/")
        reset_url = f"{base_url}/reset-password/{raw_token}"
        expiration = reset_token.expires_at.strftime("%Y-%m-%d %H:%M %Z")
        name = user.get_full_name() or user.email

        subject = "Reset your password"
        body = (
            f"Hello {name},\n\n"
            "We received a request to reset your password. Use the secure link below to choose a new password.\n\n"
            f"This link expires on {expiration}.\n\n"
            f"Reset password: {reset_url}\n\n"
            "If you did not request this reset, you can ignore this email. For your security, check your Spam or Junk folder if the email is not in your Inbox.\n\n"
            "Regards,\nInventory team"
        )

        send_product_email(
            subject=subject,
            recipient=user.email,
            text_body=body,
            greeting=f"Hello {name},",
            heading="Reset your password",
            paragraphs=(
                "We received a request to reset your password.",
                "Use the secure link below to choose a new password.",
                f"This reset link expires on {expiration}.",
                "If you did not request this reset, you can ignore this email. For your security, check your Spam or Junk folder if the email is not in your Inbox.",
            ),
            cta_label="Reset my password",
            cta_url=reset_url,
        )


class PasswordResetTokenValidationSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True)

    def validate(self, attrs):
        token = attrs["token"]
        reset_token = PasswordResetToken.objects.select_related("user").filter(
            token_hash=PasswordResetToken.hash_token(token),
        ).first()

        if not reset_token:
            raise serializers.ValidationError(
                {"token": "This password reset link is invalid or has already been used."},
            )

        if reset_token.used_at is not None:
            raise serializers.ValidationError(
                {"token": "This password reset link has already been used."},
            )

        if reset_token.is_expired:
            raise serializers.ValidationError(
                {"token": "This password reset link has expired."},
            )

        attrs["reset_token"] = reset_token
        return attrs


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        token = attrs["token"]
        reset_token = PasswordResetToken.objects.select_related("user").filter(
            token_hash=PasswordResetToken.hash_token(token),
        ).first()

        if not reset_token:
            raise serializers.ValidationError(
                {"token": "This password reset link is invalid or has already been used."},
            )

        if reset_token.used_at is not None:
            raise serializers.ValidationError(
                {"token": "This password reset link has already been used."},
            )

        if reset_token.is_expired:
            raise serializers.ValidationError(
                {"token": "This password reset link has expired."},
            )

        try:
            validate_password(attrs["password"], user=reset_token.user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc

        attrs["reset_token"] = reset_token
        return attrs

    def create(self, validated_data):
        reset_token = validated_data["reset_token"]
        password = validated_data["password"]
        user = reset_token.user

        with transaction.atomic():
            PasswordResetToken.objects.filter(
                user=user,
                used_at__isnull=True,
            ).exclude(pk=reset_token.pk).update(used_at=timezone.now())

            user.set_password(password)
            user.save(update_fields=["password"])

            reset_token.used_at = timezone.now()
            reset_token.save(update_fields=["used_at"])

        return {
            "detail": "Your password has been updated successfully.",
            "login_url": "/login",
        }
