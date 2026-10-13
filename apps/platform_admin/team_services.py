import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.platform_admin.models import PlatformTeamInvitation
from apps.platform_admin.permissions import (
    get_or_configure_platform_role_group,
    assigned_platform_roles,
    user_platform_role,
    user_has_platform_access,
)
from apps.platform_admin.roles import PLATFORM_ROLE_GROUPS, PLATFORM_SUPER_ADMIN
from apps.platform_admin.services import record_platform_event
from common.email import send_product_email

logger = logging.getLogger(__name__)
User = get_user_model()
INVITATION_TTL = timedelta(hours=24)


def _deliver_invitation(invitation_id, raw_token):
    invitation = PlatformTeamInvitation.objects.select_related("invited_user", "created_by").get(
        pk=invitation_id,
    )
    base_url = settings.FRONTEND_BASE_URL.rstrip("/")
    invite_url = f"{base_url}/platform/invite/{raw_token}"
    name = invitation.invited_user.get_full_name() or invitation.email
    inviter = invitation.created_by.get_full_name() or invitation.created_by.email
    role_label = invitation.get_role_display()
    expires = invitation.expires_at.strftime("%Y-%m-%d %H:%M %Z")
    try:
        send_product_email(
            subject="Your Inventory SaaS platform invitation",
            recipient=invitation.email,
            text_body=(
                f"Hello {name},\n\n{inviter} invited you to join the Inventory SaaS "
                f"platform team as {role_label}. Set your password using the secure link. "
                f"The invitation expires on {expires}.\n\n{invite_url}\n"
            ),
            greeting=f"Hello {name},",
            heading="Join the Inventory SaaS platform team",
            paragraphs=(
                f"{inviter} invited you as {role_label}.",
                f"Set up your account using this one-time link. It expires on {expires}.",
            ),
            cta_label="Set up platform access",
            cta_url=invite_url,
        )
    except Exception:
        logger.error(
            "Platform invitation delivery failed for invitation_id=%s",
            invitation_id,
        )


def create_platform_invitation(*, actor, email, first_name, last_name, role, reason):
    normalized_email = email.strip().lower()
    raw_token = secrets.token_urlsafe(32)
    with transaction.atomic():
        expired_invitations = PlatformTeamInvitation.objects.select_for_update().filter(
            email=normalized_email,
            status=PlatformTeamInvitation.Status.PENDING,
            expires_at__lte=timezone.now(),
        )
        for expired_invitation in expired_invitations:
            expired_invitation.status = PlatformTeamInvitation.Status.EXPIRED
            expired_invitation.save(update_fields=["status", "updated_at"])
            record_platform_event(
                actor=actor,
                action="platform_team.invitation_expired",
                target_type="PlatformTeamInvitation",
                target_id=expired_invitation.pk,
                reason=reason,
                before={"status": PlatformTeamInvitation.Status.PENDING},
                after={"status": PlatformTeamInvitation.Status.EXPIRED},
            )
        user = User.objects.select_for_update().filter(email=normalized_email).first()
        if user is None:
            user = User.objects.create_user(
                email=normalized_email,
                first_name=first_name,
                last_name=last_name,
                is_active=False,
            )
        if assigned_platform_roles(user):
            raise ValidationError({"email": "This account already has platform access."})
        invitation = PlatformTeamInvitation.objects.create(
            email=normalized_email,
            first_name=first_name,
            last_name=last_name,
            role=role,
            invited_user=user,
            token_hash=PlatformTeamInvitation.hash_token(raw_token),
            expires_at=timezone.now() + INVITATION_TTL,
            reason=reason,
            created_by=actor,
        )
        record_platform_event(
            actor=actor,
            action="platform_team.invitation_created",
            target_type="PlatformTeamInvitation",
            target_id=invitation.pk,
            reason=reason,
            after={"email": normalized_email, "role": role},
        )
        transaction.on_commit(
            lambda invitation_id=invitation.pk, token=raw_token: _deliver_invitation(
                invitation_id,
                token,
            )
        )
    return invitation


def resend_platform_invitation(*, actor, invitation_id, reason):
    raw_token = secrets.token_urlsafe(32)
    expired = False
    with transaction.atomic():
        invitation = PlatformTeamInvitation.objects.select_for_update().get(pk=invitation_id)
        if invitation.status != PlatformTeamInvitation.Status.PENDING:
            raise ValidationError({"detail": "Only pending invitations can be resent."})
        if invitation.is_expired:
            invitation.status = PlatformTeamInvitation.Status.EXPIRED
            invitation.save(update_fields=["status", "updated_at"])
            expired = True
        else:
            before = {"status": invitation.status, "expires_at": invitation.expires_at.isoformat()}
            invitation.token_hash = PlatformTeamInvitation.hash_token(raw_token)
            invitation.expires_at = timezone.now() + INVITATION_TTL
            invitation.reason = reason
            invitation.save(update_fields=["token_hash", "expires_at", "reason", "updated_at"])
            record_platform_event(
                actor=actor,
                action="platform_team.invitation_resent",
                target_type="PlatformTeamInvitation",
                target_id=invitation.pk,
                reason=reason,
                before=before,
                after={"status": invitation.status, "expires_at": invitation.expires_at.isoformat()},
            )
            transaction.on_commit(
                lambda invitation_id=invitation.pk, token=raw_token: _deliver_invitation(
                    invitation_id,
                    token,
                )
            )
    if expired:
        raise ValidationError({"detail": "This invitation has expired. Create a new invitation."})
    return invitation


def revoke_platform_invitation(*, actor, invitation_id, reason):
    with transaction.atomic():
        invitation = PlatformTeamInvitation.objects.select_for_update().get(pk=invitation_id)
        if invitation.status != PlatformTeamInvitation.Status.PENDING:
            raise ValidationError({"detail": "Only pending invitations can be revoked."})
        invitation.status = PlatformTeamInvitation.Status.REVOKED
        invitation.revoked_at = timezone.now()
        invitation.reason = reason
        invitation.save(update_fields=["status", "revoked_at", "reason", "updated_at"])
        record_platform_event(
            actor=actor,
            action="platform_team.invitation_revoked",
            target_type="PlatformTeamInvitation",
            target_id=invitation.pk,
            reason=reason,
            before={"status": PlatformTeamInvitation.Status.PENDING},
            after={"status": invitation.status},
        )
    return invitation


def accept_platform_invitation(*, invitation, user, password):
    with transaction.atomic():
        invitation = PlatformTeamInvitation.objects.select_for_update().select_related(
            "invited_user",
        ).get(pk=invitation.pk)
        if invitation.status != PlatformTeamInvitation.Status.PENDING or invitation.is_expired:
            raise ValidationError({"token": "This platform invitation is no longer valid."})
        user = User.objects.select_for_update().get(pk=invitation.invited_user_id)
        if user.email.strip().lower() != invitation.email.strip().lower():
            raise ValidationError({"token": "The invitation no longer matches this account."})
        if assigned_platform_roles(user):
            raise ValidationError({"token": "This account already has platform access."})
        group = get_or_configure_platform_role_group(invitation.role, lock=True)
        user.set_password(password)
        user.is_active = True
        if not user.first_name and invitation.first_name:
            user.first_name = invitation.first_name
        if not user.last_name and invitation.last_name:
            user.last_name = invitation.last_name
        user.save(update_fields=["password", "is_active", "first_name", "last_name"])
        user.groups.add(group)
        invitation.status = PlatformTeamInvitation.Status.ACCEPTED
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["status", "accepted_at", "updated_at"])
        record_platform_event(
            actor=user,
            action="platform_team.invitation_accepted",
            target_type="User",
            target_id=user.pk,
            reason=invitation.reason,
            after={"email": user.email, "role": invitation.role},
        )
    return user


def assert_last_super_admin_remains(user):
    if not user.groups.filter(name=PLATFORM_ROLE_GROUPS[PLATFORM_SUPER_ADMIN]).exists():
        return
    super_group = Group.objects.select_for_update().get(
        name=PLATFORM_ROLE_GROUPS[PLATFORM_SUPER_ADMIN],
    )
    active_admins = list(
        User.objects.select_for_update()
        .filter(is_active=True, groups=super_group)
        .order_by("pk")
    )
    if user.is_active and len(active_admins) <= 1:
        raise ValidationError(
            {"detail": "The final active Platform Super Admin cannot be removed."}
        )


def revoke_platform_role(*, actor, user, reason):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        roles = assigned_platform_roles(user)
        if not roles:
            raise ValidationError({"detail": "This user has no platform role."})
        if PLATFORM_SUPER_ADMIN in roles:
            assert_last_super_admin_remains(user)
        groups = list(
            Group.objects.select_for_update().filter(
                name__in=[PLATFORM_ROLE_GROUPS[role] for role in roles],
            )
        )
        user.groups.remove(*groups)
        record_platform_event(
            actor=actor,
            action="platform_team.access_revoked",
            target_type="User",
            target_id=user.pk,
            reason=reason,
            before={"email": user.email, "platform_roles": roles},
            after={"email": user.email, "platform_role": None},
        )
    return user


def change_platform_role(*, actor, user, new_role, reason):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        old_roles = assigned_platform_roles(user)
        if not old_roles:
            raise ValidationError({"detail": "This user has no platform role."})
        if old_roles == [new_role]:
            return user
        if PLATFORM_SUPER_ADMIN in old_roles and new_role != PLATFORM_SUPER_ADMIN:
            assert_last_super_admin_remains(user)
        old_groups = list(
            Group.objects.select_for_update().filter(
                name__in=[PLATFORM_ROLE_GROUPS[role] for role in old_roles],
            )
        )
        new_group = get_or_configure_platform_role_group(new_role, lock=True)
        user.groups.remove(*old_groups)
        user.groups.add(new_group)
        record_platform_event(
            actor=actor,
            action="platform_team.role_changed",
            target_type="User",
            target_id=user.pk,
            reason=reason,
            before={"email": user.email, "platform_roles": old_roles},
            after={"email": user.email, "platform_role": new_role},
        )
    return user
    
