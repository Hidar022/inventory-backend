import uuid

from django.conf import settings
from django.db import models

from apps.organizations.models import Organization


class ActivityEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="activity_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_events",
    )
    action = models.CharField(max_length=64)
    entity_type = models.CharField(max_length=64)
    entity_id = models.CharField(max_length=255, blank=True, default="")
    description = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["organization", "-created_at"], name="activity_org_created_idx"),
            models.Index(fields=["actor", "-created_at"], name="activity_actor_created_idx"),
            models.Index(fields=["action", "-created_at"], name="activity_action_created_idx"),
        ]

    def __str__(self):
        return f"{self.action} by {self.actor.email}"
