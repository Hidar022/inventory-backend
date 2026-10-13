import uuid
import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.organizations.models import Organization
from apps.platform_admin.roles import PLATFORM_ROLE_CHOICES


class PlatformAuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_audit_events",
    )
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=255)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_audit_events",
    )
    reason = models.TextField(blank=True, default="")
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at"], name="platform_audit_created_idx"),
            models.Index(fields=["target_type", "target_id"], name="platform_audit_target_idx"),
            models.Index(fields=["organization", "-created_at"], name="platform_audit_org_idx"),
        ]
        permissions = [
            (
                "access_platform_control_center",
                "Can access the platform control center",
            ),
            ("view_platform_support_data", "Can view platform support data"),
            ("view_platform_finance_data", "Can view platform finance data"),
            ("manage_platform_organizations", "Can manage platform organizations"),
            ("manage_platform_users", "Can manage platform users"),
            ("manage_platform_billing", "Can manage platform billing"),
            ("manage_platform_team", "Can manage the platform team"),
        ]

    def __str__(self):
        return f"{self.action} {self.target_type}:{self.target_id}"


class PlatformTeamInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        EXPIRED = "EXPIRED", "Expired"
        REVOKED = "REVOKED", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    role = models.CharField(max_length=32, choices=PLATFORM_ROLE_CHOICES)
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="platform_team_invitations",
    )
    token_hash = models.CharField(max_length=128, unique=True, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    reason = models.CharField(max_length=1000)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_platform_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(status="PENDING"),
                name="unique_pending_platform_invite_email",
            ),
        ]

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @classmethod
    def expiry_time(cls):
        return timezone.now() + timedelta(hours=24)

    def __str__(self):
        return f"{self.email} ({self.role}) - {self.status}"