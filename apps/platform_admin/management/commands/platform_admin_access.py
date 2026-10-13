from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.platform_admin.models import PlatformAuditEvent
from apps.platform_admin.permissions import (
    PLATFORM_ADMIN_GROUP,
    PLATFORM_ADMIN_PERMISSION,
    configure_platform_role_group,
)
from apps.platform_admin.roles import PLATFORM_SUPER_ADMIN
from apps.platform_admin.services import record_platform_event

User = get_user_model()


class Command(BaseCommand):
    help = "Grant or revoke explicit Platform Control Center access."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("grant", "revoke"))
        parser.add_argument("email")
        parser.add_argument("--actor-email", required=True)
        parser.add_argument("--reason", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        actor_email = options["actor_email"].strip().lower()
        reason = options["reason"].strip()
        if not reason:
            raise CommandError("A non-empty reason is required.")

        content_type = ContentType.objects.get_for_model(PlatformAuditEvent)
        permission = Permission.objects.get(
            content_type=content_type,
            codename=PLATFORM_ADMIN_PERMISSION,
        )
        group, _ = Group.objects.get_or_create(name=PLATFORM_ADMIN_GROUP)
        group = Group.objects.select_for_update().get(pk=group.pk)
        user = User.objects.select_for_update().filter(email=email).first()
        if user is None:
            raise CommandError("No user exists with that email address.")
        actor = User.objects.filter(email=actor_email, is_active=True).first()
        if actor is None or not actor.is_staff:
            raise CommandError("An active Django staff operator is required for audit attribution.")
        if options["action"] == "grant" and not user.is_active:
            raise CommandError("Activate the user account before granting access.")

        had_permission = group.permissions.filter(pk=permission.pk).exists()
        if (
            not had_permission
            and group.user_set.exclude(pk=user.pk).exists()
        ):
            raise CommandError(
                "The platform group has other members but lost its permission. "
                "Review those memberships before restoring group access."
            )
        if not configure_platform_role_group(group, PLATFORM_SUPER_ADMIN):
            raise CommandError("Platform role permissions are not installed. Run migrations first.")

        if options["action"] == "grant":
            if had_permission and user.groups.filter(pk=group.pk).exists():
                self.stdout.write(self.style.WARNING("User already has platform access."))
                return
            user.groups.add(group)
            record_platform_event(
                actor=actor,
                action="platform_admin.granted",
                target_type="User",
                target_id=user.pk,
                reason=reason,
                after={"email": user.email, "group": group.name},
            )
            self.stdout.write(self.style.SUCCESS("Platform access granted."))
            return

        if not user.groups.filter(pk=group.pk).exists():
            self.stdout.write(self.style.WARNING("User has no platform access."))
            return

        locked_admins = list(
            User.objects.select_for_update()
            .filter(is_active=True, groups=group)
            .order_by("pk")
        )
        if user.is_active and len(locked_admins) <= 1:
            raise CommandError(
                "Cannot revoke the final active platform administrator."
            )

        user.groups.remove(group)
        record_platform_event(
            actor=actor,
            action="platform_admin.revoked",
            target_type="User",
            target_id=user.pk,
            reason=reason,
            before={"email": user.email, "group": group.name},
        )
        self.stdout.write(self.style.SUCCESS("Platform access revoked."))