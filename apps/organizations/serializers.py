from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from apps.organizations.models import Membership, Organization


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
