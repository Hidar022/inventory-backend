from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.organizations.models import Membership
from apps.organizations.serializers import OrganizationSummarySerializer

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
        subject = "Welcome to Inventory SaaS"
        body = (
            f"Hello {name},\n\n"
            "Welcome to Inventory SaaS. Your account has been created successfully.\n\n"
            f"Login here: {settings.FRONTEND_BASE_URL.rstrip('/')}/login\n\n"
            "Next steps: create or join an organization, then invite your team and start managing inventory."
        )
        email = EmailMultiAlternatives(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
        email.send()


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
