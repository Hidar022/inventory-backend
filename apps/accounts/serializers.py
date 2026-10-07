from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

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
