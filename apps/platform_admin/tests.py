from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command, CommandError
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import BillingTransaction, OrganizationSubscription, SubscriptionPlan
from apps.billing.services import ensure_default_plans
from apps.organizations.models import Membership, Organization
from apps.accounts.admin import UserChangeForm
from apps.platform_admin.models import PlatformAuditEvent, PlatformTeamInvitation
from apps.platform_admin.permissions import (
    PLATFORM_ADMIN_GROUP,
    IsPlatformSuperAdmin,
    configure_platform_role_group,
    get_or_configure_platform_role_group,
    user_platform_role,
)
from apps.platform_admin.roles import (
    PLATFORM_FINANCE,
    PLATFORM_ROLE_PERMISSIONS,
    PLATFORM_ROLE_GROUPS,
    PLATFORM_SUPPORT,
    PLATFORM_SUPER_ADMIN,
)
from apps.platform_admin.services import record_platform_event

User = get_user_model()


class PlatformAuthorizationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.permission = IsPlatformSuperAdmin()
        self.group = Group.objects.create(name=PLATFORM_ADMIN_GROUP)
        configure_platform_role_group(self.group, PLATFORM_SUPER_ADMIN)
        self.organization = Organization.objects.create(
            name="Authorization Test",
            slug="authorization-test",
        )

    def allowed(self, user):
        request = self.factory.get("/api/v1/platform/overview/")
        request.user = user
        return self.permission.has_permission(request, None)

    def test_org_roles_staff_and_superuser_do_not_grant_platform_access(self):
        for role in Membership.Role.values:
            user = User.objects.create_user(
                email=f"{role.lower()}@example.com",
                password="StrongPass123!",
            )
            Membership.objects.create(
                user=user,
                organization=self.organization,
                role=role,
            )
            self.assertFalse(self.allowed(user))

        staff_user = User.objects.create_user(
            email="staff@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        superuser = User.objects.create_superuser(
            email="superuser@example.com",
            password="StrongPass123!",
        )
        self.assertFalse(self.allowed(staff_user))
        self.assertFalse(self.allowed(superuser))

    def test_explicit_group_permission_grants_only_active_user_access(self):
        user = User.objects.create_user(
            email="platform-admin@example.com",
            password="StrongPass123!",
        )
        user.groups.add(self.group)
        self.assertTrue(self.allowed(user))
        self.assertEqual(user_platform_role(user), PLATFORM_SUPER_ADMIN)

        user.is_active = False
        user.save(update_fields=["is_active"])
        self.assertFalse(self.allowed(user))

    def test_migration_backfills_only_the_explicit_legacy_super_admin_group(self):
        content_type = self.group.permissions.first().content_type
        legacy_access = Permission.objects.get(
            content_type=content_type,
            codename="access_platform_control_center",
        )
        self.group.permissions.clear()
        self.group.permissions.add(legacy_access)
        user = User.objects.create_user(
            email="legacy-platform-admin@example.com",
            password="StrongPass123!",
        )
        user.groups.add(self.group)

        unrelated_group = Group.objects.create(name="Other Group")
        unrelated_group.permissions.add(legacy_access)

        migration = import_module(
            "apps.platform_admin.migrations.0003_backfill_super_admin_role_permissions"
        )
        schema_editor = SimpleNamespace(connection=connection)
        migration.upgrade_existing_super_admin_group(apps, schema_editor)

        self.assertEqual(user_platform_role(user), PLATFORM_SUPER_ADMIN)
        self.assertEqual(
            set(self.group.permissions.values_list("codename", flat=True)),
            PLATFORM_ROLE_PERMISSIONS[PLATFORM_SUPER_ADMIN],
        )
        self.assertEqual(
            set(unrelated_group.permissions.values_list("codename", flat=True)),
            {"access_platform_control_center"},
        )

        migration.upgrade_existing_super_admin_group(apps, schema_editor)
        self.assertEqual(
            Permission.objects.filter(
                content_type=content_type,
                codename__in=PLATFORM_ROLE_PERMISSIONS[PLATFORM_SUPER_ADMIN],
            ).count(),
            len(PLATFORM_ROLE_PERMISSIONS[PLATFORM_SUPER_ADMIN]),
        )

        migration.noop_reverse(apps, schema_editor)
        self.assertEqual(user_platform_role(user), PLATFORM_SUPER_ADMIN)

    def test_cli_bootstrap_records_grant_and_protects_final_admin(self):
        user = User.objects.create_user(
            email="bootstrap@example.com",
            password="StrongPass123!",
        )
        operator = User.objects.create_user(
            email="operator@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        output = StringIO()
        call_command(
            "platform_admin_access",
            "grant",
            user.email,
            actor_email=operator.email,
            reason="Initial secure bootstrap",
            stdout=output,
        )
        user.refresh_from_db()
        self.assertTrue(self.allowed(user))
        event = PlatformAuditEvent.objects.get(action="platform_admin.granted")
        self.assertEqual(event.actor, operator)
        self.assertEqual(event.target_id, str(user.pk))
        self.assertEqual(event.reason, "Initial secure bootstrap")

        with self.assertRaises(CommandError):
            call_command(
                "platform_admin_access",
                "revoke",
                user.email,
                actor_email=operator.email,
                reason="Attempt to revoke last administrator",
                stdout=StringIO(),
            )
        user.refresh_from_db()
        self.assertTrue(self.allowed(user))

    def test_audit_service_sanitizes_sensitive_metadata(self):
        event = record_platform_event(
            actor=None,
            action="test.sanitization",
            target_type="User",
            target_id="123",
            reason="Support note token=do-not-store",
            before={"email": "person@example.com", "password_hash": "never-store"},
            after={"access_token": "never-store", "is_active": True},
        )
        self.assertEqual(event.before, {"email": "person@example.com"})
        self.assertEqual(event.after, {"is_active": True})
        self.assertEqual(event.reason, "Support note token=[REDACTED]")

    def test_django_admin_cannot_edit_platform_group_or_delete_users(self):
        request = self.factory.get("/admin/")
        request.user = User.objects.create_superuser(
            email="django-admin@example.com",
            password="StrongPass123!",
        )
        group_admin = admin.site._registry[Group]
        user_admin = admin.site._registry[User]
        self.assertFalse(group_admin.has_change_permission(request, self.group))
        self.assertFalse(group_admin.has_delete_permission(request, self.group))
        with self.assertRaises(PermissionDenied):
            group_admin.delete_queryset(request, Group.objects.filter(pk=self.group.pk))
        self.assertFalse(user_admin.has_delete_permission(request, request.user))

    def test_django_user_form_cannot_change_platform_group_membership(self):
        platform_admin = User.objects.create_user(
            email="managed-platform-admin@example.com",
            password="StrongPass123!",
        )
        platform_admin.groups.add(self.group)
        form = UserChangeForm(
            data={
                "email": platform_admin.email,
                "first_name": "",
                "last_name": "",
                "is_active": "on",
                "is_staff": "on",
                "is_superuser": "",
                "groups": [],
                "user_permissions": [],
            },
            instance=platform_admin,
        )
        self.assertFalse(form.is_valid())
        self.assertIn(
            "Use the audited platform team operation",
            str(form.non_field_errors()),
        )


class PlatformAdminAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(
            name="North Shop",
            slug="north-shop",
        )
        self.other_organization = Organization.objects.create(
            name="South Shop",
            slug="south-shop",
        )
        self.owner = self.make_user("owner@example.com", Membership.Role.OWNER, self.organization)
        self.manager = self.make_user("manager@example.com", Membership.Role.MANAGER, self.organization)
        self.cashier = self.make_user("cashier@example.com", Membership.Role.CASHIER, self.organization)
        self.other_owner = self.make_user("other-owner@example.com", Membership.Role.OWNER, self.other_organization)
        self.staff_user = User.objects.create_user(
            email="staff@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        self.platform_admin = User.objects.create_user(
            email="platform@example.com",
            password="StrongPass123!",
        )
        self.platform_group = Group.objects.create(name=PLATFORM_ADMIN_GROUP)
        configure_platform_role_group(self.platform_group, PLATFORM_SUPER_ADMIN)
        self.platform_admin.groups.add(self.platform_group)
        self.support = User.objects.create_user(
            email="platform-support@example.com",
            password="StrongPass123!",
        )
        self.support.groups.add(
            get_or_configure_platform_role_group(PLATFORM_SUPPORT),
        )
        self.finance = User.objects.create_user(
            email="platform-finance@example.com",
            password="StrongPass123!",
        )
        self.finance.groups.add(
            get_or_configure_platform_role_group(PLATFORM_FINANCE),
        )
        self.monthly_plan = SubscriptionPlan.objects.create(
            code=SubscriptionPlan.Code.MONTHLY,
            name="Monthly",
            description="One month of access",
            months=1,
            amount=Decimal("10000.00"),
            currency="NGN",
            sort_order=1,
        )
        self.subscription = OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=self.monthly_plan,
            status=OrganizationSubscription.Status.ACTIVE,
            start_date=timezone.now(),
            expiry_date=timezone.now() + timedelta(days=30),
        )

    def make_user(self, email, role, organization):
        user = User.objects.create_user(email=email, password="StrongPass123!")
        Membership.objects.create(user=user, organization=organization, role=role)
        return user

    def authenticate(self, user):
        self.client.force_authenticate(user=user)

    def test_every_platform_read_endpoint_denies_non_platform_principals(self):
        paths = [
            "/api/v1/platform/overview/",
            "/api/v1/platform/organizations/",
            f"/api/v1/platform/organizations/{self.organization.pk}/",
            "/api/v1/platform/users/",
            f"/api/v1/platform/users/{self.owner.pk}/",
            "/api/v1/platform/team/",
            "/api/v1/platform/team/invitations/",
            "/api/v1/platform/subscriptions/",
            f"/api/v1/platform/subscriptions/{self.subscription.pk}/",
            "/api/v1/platform/plans/",
            f"/api/v1/platform/plans/{self.monthly_plan.code}/",
            "/api/v1/platform/payments/",
            "/api/v1/platform/audit/",
        ]
        for user in (self.owner, self.manager, self.cashier, self.staff_user):
            self.authenticate(user)
            for path in paths:
                with self.subTest(user=user.email, path=path):
                    self.assertEqual(self.client.get(path).status_code, status.HTTP_403_FORBIDDEN)

        self.authenticate(User.objects.create_superuser(
            email="unassigned-superuser@example.com",
            password="StrongPass123!",
        ))
        self.assertEqual(
            self.client.get("/api/v1/platform/overview/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.client.force_authenticate(user=None)
        for path in paths:
            with self.subTest(anonymous_path=path):
                self.assertEqual(self.client.get(path).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_platform_admin_can_read_cross_organization_data_safely(self):
        self.authenticate(self.platform_admin)
        organizations = self.client.get("/api/v1/platform/organizations/")
        self.assertEqual(organizations.status_code, status.HTTP_200_OK)
        self.assertEqual(organizations.data["count"], 2)

        filtered = self.client.get(
            "/api/v1/platform/organizations/",
            {"search": "South", "status": "ACTIVE"},
        )
        self.assertEqual(filtered.data["count"], 1)
        self.assertEqual(filtered.data["results"][0]["id"], str(self.other_organization.pk))

        detail = self.client.get(f"/api/v1/platform/organizations/{self.organization.pk}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(len(detail.data["memberships"]), 3)

        overview = self.client.get("/api/v1/platform/overview/?currency=ngn")
        self.assertEqual(overview.status_code, status.HTTP_200_OK)
        self.assertEqual(overview.data["organizations_total"], 2)
        self.assertEqual(overview.data["revenue_received"], "0.00")
        self.assertEqual(overview.data["currency"], "NGN")

        users = self.client.get("/api/v1/platform/users/?role=OWNER")
        self.assertEqual(users.status_code, status.HTTP_200_OK)
        self.assertEqual(users.data["count"], 2)
        user_detail = self.client.get(f"/api/v1/platform/users/{self.owner.pk}/")
        self.assertEqual(user_detail.status_code, status.HTTP_200_OK)
        self.assertNotIn("password", user_detail.data)
        self.assertFalse(user_detail.data["is_platform_admin"])
        owner_membership = next(
            membership
            for membership in user_detail.data["memberships"]
            if membership["organization_id"] == str(self.organization.pk)
        )
        self.assertEqual(owner_membership["organization_name"], self.organization.name)

        payment_values = [
            ("INV-REVENUE-QUALIFIED", "NGN", BillingTransaction.Status.SUCCESS, True, Decimal("25000.00")),
            ("INV-REVENUE-PENDING", "NGN", BillingTransaction.Status.PENDING, False, Decimal("40000.00")),
            ("INV-REVENUE-OTHER-CURRENCY", "USD", BillingTransaction.Status.SUCCESS, True, Decimal("100.00")),
        ]
        for reference, currency, payment_status, fulfilled, amount in payment_values:
            BillingTransaction.objects.create(
                organization=self.organization,
                plan=self.monthly_plan,
                reference=reference,
                kind=BillingTransaction.Kind.RENEWAL,
                amount=amount,
                currency=currency,
                status=payment_status,
                is_fulfilled=fulfilled,
                paid_at=timezone.now() if fulfilled else None,
                email=self.owner.email,
            )
        overview = self.client.get("/api/v1/platform/overview/?currency=NGN")
        self.assertEqual(overview.data["revenue_received"], "25000.00")
        self.assertEqual(overview.data["payments_pending"], 1)
        self.assertEqual(overview.data["payments_successful"], 2)

    def test_platform_subscription_and_plan_lists_filter_effective_status(self):
        past_subscription = OrganizationSubscription.objects.create(
            organization=self.other_organization,
            plan=self.monthly_plan,
            status=OrganizationSubscription.Status.ACTIVE,
            start_date=timezone.now() - timedelta(days=45),
            expiry_date=timezone.now() - timedelta(days=15),
        )
        self.authenticate(self.platform_admin)
        expired = self.client.get("/api/v1/platform/subscriptions/?status=EXPIRED")
        active = self.client.get("/api/v1/platform/subscriptions/?status=ACTIVE")
        expired_ids = {item["id"] for item in expired.data["results"]}
        active_ids = {item["id"] for item in active.data["results"]}
        self.assertIn(str(past_subscription.pk), expired_ids)
        self.assertNotIn(str(past_subscription.pk), active_ids)

        plans = self.client.get("/api/v1/platform/plans/")
        self.assertEqual(plans.status_code, status.HTTP_200_OK)
        self.assertTrue(any(item["code"] == self.monthly_plan.code for item in plans.data["results"]))

    def test_platform_roles_have_distinct_read_boundaries(self):
        support_allowed = (
            "/api/v1/platform/organizations/",
            "/api/v1/platform/users/",
            "/api/v1/platform/subscriptions/",
        )
        support_denied = (
            "/api/v1/platform/overview/",
            "/api/v1/platform/payments/",
            "/api/v1/platform/audit/",
            "/api/v1/platform/team/",
            "/api/v1/platform/finance/overview/",
        )
        self.authenticate(self.support)
        for path in support_allowed:
            self.assertEqual(self.client.get(path).status_code, status.HTTP_200_OK)
        for path in support_denied:
            self.assertEqual(self.client.get(path).status_code, status.HTTP_403_FORBIDDEN)

        self.authenticate(self.finance)
        for path in (
            "/api/v1/platform/subscriptions/",
            "/api/v1/platform/plans/",
            "/api/v1/platform/payments/",
            "/api/v1/platform/finance/overview/",
        ):
            self.assertEqual(self.client.get(path).status_code, status.HTTP_200_OK)
        for path in (
            "/api/v1/platform/organizations/",
            "/api/v1/platform/users/",
            "/api/v1/platform/audit/",
            "/api/v1/platform/team/",
        ):
            self.assertEqual(self.client.get(path).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.post(
                "/api/v1/platform/team/invitations/",
                {
                    "email": "escalation@example.com",
                    "role": PLATFORM_SUPER_ADMIN,
                    "reason": "Attempted role escalation",
                },
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/platform/organizations/{self.organization.pk}/"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_support_org_inspection_does_not_expose_financial_history(self):
        BillingTransaction.objects.create(
            organization=self.organization,
            plan=self.monthly_plan,
            subscription=self.subscription,
            reference="INV-SUPPORT-REDACT-PAYMENT",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=self.monthly_plan.amount,
            currency="NGN",
            email=self.owner.email,
        )
        self.authenticate(self.support)
        response = self.client.get(
            f"/api/v1/platform/organizations/{self.organization.pk}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["recent_payments"], [])
        self.assertEqual(
            response.data["latest_subscription"]["recent_payments"],
            [],
        )

    def test_platform_invitation_grants_access_only_after_one_time_acceptance(self):
        self.authenticate(self.platform_admin)
        with patch("apps.platform_admin.team_services.send_product_email") as send_email:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/platform/team/invitations/",
                    {
                        "email": "new-platform-support@example.com",
                        "first_name": "New",
                        "last_name": "Support",
                        "role": PLATFORM_SUPPORT,
                        "reason": "Support team capacity",
                    },
                    format="json",
                )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn("token", response.data)
        self.assertNotIn("token_hash", response.data)
        invited_user = User.objects.get(email="new-platform-support@example.com")
        self.assertFalse(invited_user.is_active)
        self.assertFalse(invited_user.groups.exists())
        raw_token = send_email.call_args.kwargs["cta_url"].rsplit("/", 1)[-1]

        self.authenticate(self.platform_admin)
        validation = self.client.get(
            "/api/v1/platform/team/invitations/validate/",
            {"token": raw_token},
        )
        self.assertEqual(validation.status_code, status.HTTP_200_OK)
        self.assertEqual(validation.data["role"], PLATFORM_SUPPORT)
        accepted = self.client.post(
            "/api/v1/platform/team/invitations/accept/",
            {"token": raw_token, "password": "StrongerPlatformPass!2026"},
            format="json",
        )
        self.assertEqual(accepted.status_code, status.HTTP_200_OK, accepted.data)
        invited_user.refresh_from_db()
        self.assertTrue(invited_user.is_active)
        self.assertEqual(user_platform_role(invited_user), PLATFORM_SUPPORT)
        self.assertEqual(invited_user.organization_memberships.count(), 0)
        self.assertTrue(
            PlatformAuditEvent.objects.filter(
                action="platform_team.invitation_accepted",
                target_id=str(invited_user.pk),
            ).exists()
        )
        replay = self.client.post(
            "/api/v1/platform/team/invitations/accept/",
            {"token": raw_token, "password": "AnotherPlatformPass!2026"},
            format="json",
        )
        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)

    def test_existing_account_is_unchanged_until_invitation_acceptance(self):
        self.authenticate(self.platform_admin)
        response = self.client.post(
            "/api/v1/platform/team/invitations/",
            {
                "email": self.owner.email,
                "role": PLATFORM_FINANCE,
                "reason": "Finance support assignment",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertIsNone(user_platform_role(self.owner))
        self.assertTrue(self.owner.organization_memberships.filter(is_active=True).exists())

    def test_deactivated_former_platform_role_cannot_be_reinvited_with_lower_privilege(self):
        former_admin = User.objects.create_user(
            email="former-platform-admin@example.com",
            password="StrongPass123!",
            is_active=False,
        )
        former_admin.groups.add(self.platform_group)
        self.authenticate(self.platform_admin)
        response = self.client.post(
            "/api/v1/platform/team/invitations/",
            {
                "email": former_admin.email,
                "role": PLATFORM_SUPPORT,
                "reason": "Attempt to downgrade former admin",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        former_admin.refresh_from_db()
        self.assertFalse(former_admin.is_active)
        self.assertTrue(former_admin.groups.filter(name=PLATFORM_ADMIN_GROUP).exists())
        self.assertFalse(PlatformTeamInvitation.objects.filter(email=former_admin.email).exists())

    def test_invitation_resend_rotates_hash_and_revoke_invalidates_link(self):
        self.authenticate(self.platform_admin)
        with patch("apps.platform_admin.team_services.send_product_email") as send_email:
            with self.captureOnCommitCallbacks(execute=True):
                created = self.client.post(
                    "/api/v1/platform/team/invitations/",
                    {
                        "email": "resend-platform@example.com",
                        "role": PLATFORM_FINANCE,
                        "reason": "Finance onboarding",
                    },
                    format="json",
                )
        invitation_id = created.data["id"]
        old_token = send_email.call_args.kwargs["cta_url"].rsplit("/", 1)[-1]
        with patch("apps.platform_admin.team_services.send_product_email") as resent_email:
            with self.captureOnCommitCallbacks(execute=True):
                resent = self.client.post(
                    f"/api/v1/platform/team/invitations/{invitation_id}/resend/",
                    {"reason": "Recipient requested a fresh link"},
                    format="json",
                )
        self.assertEqual(resent.status_code, status.HTTP_200_OK)
        new_token = resent_email.call_args.kwargs["cta_url"].rsplit("/", 1)[-1]
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(
            self.client.get("/api/v1/platform/team/invitations/validate/", {"token": old_token}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        revoked = self.client.post(
            f"/api/v1/platform/team/invitations/{invitation_id}/revoke/",
            {"reason": "Hiring plan changed"},
            format="json",
        )
        self.assertEqual(revoked.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.get("/api/v1/platform/team/invitations/validate/", {"token": new_token}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_platform_role_changes_are_audited_and_last_super_admin_is_protected(self):
        self.authenticate(self.platform_admin)
        last_admin = self.client.patch(
            f"/api/v1/platform/team/{self.platform_admin.pk}/role/",
            {"role": PLATFORM_SUPPORT, "reason": "Attempt to remove last Super Admin"},
            format="json",
        )
        self.assertEqual(last_admin.status_code, status.HTTP_400_BAD_REQUEST)

        second_admin = User.objects.create_user(
            email="second-super-admin@example.com",
            password="StrongPass123!",
        )
        second_admin.groups.add(self.platform_group)
        changed = self.client.patch(
            f"/api/v1/platform/team/{second_admin.pk}/role/",
            {"role": PLATFORM_FINANCE, "reason": "Least privilege review"},
            format="json",
        )
        self.assertEqual(changed.status_code, status.HTTP_200_OK, changed.data)
        second_admin.refresh_from_db()
        self.assertEqual(user_platform_role(second_admin), PLATFORM_FINANCE)
        self.assertTrue(PlatformAuditEvent.objects.filter(action="platform_team.role_changed").exists())

        revoked = self.client.post(
            f"/api/v1/platform/team/{second_admin.pk}/revoke/",
            {"reason": "Access no longer required"},
            format="json",
        )
        self.assertEqual(revoked.status_code, status.HTTP_200_OK)
        second_admin.refresh_from_db()
        self.assertIsNone(user_platform_role(second_admin))

    def test_all_mutation_endpoints_deny_tenant_roles_and_staff(self):
        self.authenticate(self.platform_admin)
        payment = BillingTransaction.objects.create(
            organization=self.organization,
            plan=self.monthly_plan,
            subscription=self.subscription,
            reference="INV-PLATFORM-AUTH-1",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=self.monthly_plan.amount,
            currency="NGN",
            email=self.owner.email,
        )
        Membership.objects.create(
            user=self.owner,
            organization=self.other_organization,
            role=Membership.Role.MANAGER,
        )
        routes = [
            ("post", f"/api/v1/platform/organizations/{self.organization.pk}/suspend/", {"reason": "Administrative review"}),
            ("post", f"/api/v1/platform/organizations/{self.organization.pk}/reactivate/", {"reason": "Review complete"}),
            ("post", f"/api/v1/platform/users/{self.manager.pk}/deactivate/", {"reason": "Policy violation"}),
            ("post", f"/api/v1/platform/users/{self.manager.pk}/reactivate/", {"reason": "Review complete"}),
            ("patch", f"/api/v1/platform/memberships/{self.manager.organization_memberships.first().pk}/role/", {"role": "CASHIER", "reason": "Role correction"}),
            ("post", f"/api/v1/platform/subscriptions/{self.subscription.pk}/adjust/", {"days": 7, "reason": "Support correction"}),
            ("patch", f"/api/v1/platform/plans/{self.monthly_plan.code}/", {"name": "Renamed", "reason": "Copy correction"}),
            ("post", f"/api/v1/platform/plans/{self.monthly_plan.code}/deactivate/", {"reason": "Plan retired"}),
            ("post", f"/api/v1/platform/plans/{self.monthly_plan.code}/reactivate/", {"reason": "Plan restored"}),
            ("post", "/api/v1/platform/team/invitations/", {"email": "blocked@example.com", "role": PLATFORM_SUPPORT, "reason": "Blocked unauthorized invite"}),
            ("patch", f"/api/v1/platform/team/{self.manager.pk}/role/", {"role": PLATFORM_FINANCE, "reason": "Blocked unauthorized role change"}),
            ("post", f"/api/v1/platform/team/{self.manager.pk}/revoke/", {"reason": "Blocked unauthorized revocation"}),
            ("delete", f"/api/v1/platform/users/{self.manager.pk}/", None),
        ]
        for user in (self.owner, self.manager, self.cashier, self.staff_user):
            self.authenticate(user)
            for method, path, data in routes:
                with self.subTest(user=user.email, method=method, path=path):
                    response = getattr(self.client, method)(path, data, format="json") if data is not None else getattr(self.client, method)(path)
                    self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(user=None)
        for method, path, data in routes:
            with self.subTest(anonymous_method=method, anonymous_path=path):
                response = getattr(self.client, method)(path, data, format="json") if data is not None else getattr(self.client, method)(path)
                self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        payment.refresh_from_db()
        self.assertEqual(payment.status, BillingTransaction.Status.PENDING)

    def test_organization_suspension_preserves_data_and_leaves_billing_available(self):
        self.authenticate(self.platform_admin)
        response = self.client.post(
            f"/api/v1/platform/organizations/{self.organization.pk}/suspend/",
            {"reason": "Account review required"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.organization.refresh_from_db()
        self.assertFalse(self.organization.is_active)
        self.assertEqual(self.organization.memberships.count(), 3)
        self.assertTrue(
            PlatformAuditEvent.objects.filter(
                action="organization.suspended",
                target_id=str(self.organization.pk),
                reason="Account review required",
            ).exists()
        )

        self.authenticate(self.owner)
        self.assertEqual(self.client.get("/api/v1/products/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get("/api/v1/billing/summary/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get("/api/v1/billing/plans/").status_code, status.HTTP_200_OK)

        self.authenticate(self.platform_admin)
        response = self.client.post(
            f"/api/v1/platform/organizations/{self.organization.pk}/reactivate/",
            {"reason": "Review completed"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.organization.refresh_from_db()
        self.assertTrue(self.organization.is_active)
        self.authenticate(self.owner)
        self.assertEqual(self.client.get("/api/v1/products/").status_code, status.HTTP_200_OK)

    def test_user_deactivation_invalidates_access_and_refresh_and_protects_owners(self):
        refresh = RefreshToken.for_user(self.manager)
        self.authenticate(self.platform_admin)
        response = self.client.post(
            f"/api/v1/platform/users/{self.manager.pk}/deactivate/",
            {"reason": "Employment ended"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.manager.refresh_from_db()
        self.assertFalse(self.manager.is_active)
        self.assertTrue(
            PlatformAuditEvent.objects.filter(
                action="user.deactivated",
                target_id=str(self.manager.pk),
            ).exists()
        )

        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.credentials()
        self.assertEqual(
            self.client.post("/api/v1/auth/token/refresh/", {"refresh": str(refresh)}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        self.authenticate(self.platform_admin)
        owner_response = self.client.post(
            f"/api/v1/platform/users/{self.owner.pk}/deactivate/",
            {"reason": "Attempt last owner deactivation"},
            format="json",
        )
        self.assertEqual(owner_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

        reactivate = self.client.post(
            f"/api/v1/platform/users/{self.manager.pk}/reactivate/",
            {"reason": "Reinstated"},
            format="json",
        )
        self.assertEqual(reactivate.status_code, status.HTTP_200_OK)

    def test_last_platform_admin_cannot_be_deactivated_and_user_delete_is_disabled(self):
        self.authenticate(self.platform_admin)
        last_admin = self.client.post(
            f"/api/v1/platform/users/{self.platform_admin.pk}/deactivate/",
            {"reason": "Attempt last platform admin"},
            format="json",
        )
        self.assertEqual(last_admin.status_code, status.HTTP_400_BAD_REQUEST)
        deletion = self.client.delete(f"/api/v1/platform/users/{self.owner.pk}/")
        self.assertEqual(deletion.status_code, status.HTTP_409_CONFLICT)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.organization_memberships.filter(is_active=True).exists())

    def test_membership_role_change_and_subscription_adjustment_are_audited(self):
        self.authenticate(self.platform_admin)
        membership = self.manager.organization_memberships.get(organization=self.organization)
        role_response = self.client.patch(
            f"/api/v1/platform/memberships/{membership.pk}/role/",
            {"role": Membership.Role.CASHIER, "reason": "Corrected staff assignment"},
            format="json",
        )
        self.assertEqual(role_response.status_code, status.HTTP_200_OK, role_response.data)
        membership.refresh_from_db()
        self.assertEqual(membership.role, Membership.Role.CASHIER)
        self.assertTrue(PlatformAuditEvent.objects.filter(action="membership.role_changed").exists())

        old_expiry = self.subscription.expiry_date
        adjustment = self.client.post(
            f"/api/v1/platform/subscriptions/{self.subscription.pk}/adjust/",
            {"days": 14, "reason": "Approved support adjustment"},
            format="json",
        )
        self.assertEqual(adjustment.status_code, status.HTTP_200_OK, adjustment.data)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.expiry_date, old_expiry + timedelta(days=14))
        self.assertEqual(BillingTransaction.objects.count(), 0)
        self.assertTrue(PlatformAuditEvent.objects.filter(action="subscription.adjusted").exists())

    def test_plan_admin_preserves_terms_pending_transactions_and_seeded_values(self):
        ensure_default_plans()
        plan = SubscriptionPlan.objects.get(code=SubscriptionPlan.Code.INITIAL_SETUP_6_MONTHS)
        plan.description = "Platform-managed description"
        plan.is_active = False
        plan.save(update_fields=["description", "is_active"])
        ensure_default_plans()
        plan.refresh_from_db()
        self.assertEqual(plan.description, "Platform-managed description")
        self.assertFalse(plan.is_active)

        pending = BillingTransaction.objects.create(
            organization=self.organization,
            plan=self.monthly_plan,
            reference="INV-PLATFORM-PENDING-TERM",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=Decimal("10000.00"),
            currency="NGN",
            email=self.owner.email,
        )
        self.authenticate(self.platform_admin)
        invalid_price_edit = self.client.patch(
            f"/api/v1/platform/plans/{self.monthly_plan.code}/",
            {"amount": "1.00", "months": 99, "reason": "Must not change terms"},
            format="json",
        )
        self.assertEqual(invalid_price_edit.status_code, status.HTTP_400_BAD_REQUEST)
        self.client.post(
            f"/api/v1/platform/plans/{self.monthly_plan.code}/deactivate/",
            {"reason": "Temporary offer pause"},
            format="json",
        )
        pending.refresh_from_db()
        self.monthly_plan.refresh_from_db()
        self.assertEqual(pending.amount, Decimal("10000.00"))
        self.assertEqual(self.monthly_plan.amount, Decimal("10000.00"))
        self.assertFalse(self.monthly_plan.is_active)

    def test_payment_list_is_read_only_sanitized_and_audit_is_not_tenant_activity(self):
        payment_subscription = OrganizationSubscription.objects.create(
            organization=self.other_organization,
            plan=self.monthly_plan,
            status=OrganizationSubscription.Status.ACTIVE,
            start_date=timezone.now(),
            expiry_date=timezone.now() + timedelta(days=30),
        )
        payment = BillingTransaction.objects.create(
            organization=self.other_organization,
            plan=self.monthly_plan,
            subscription=payment_subscription,
            reference="INV-CROSS-TENANT-PAYMENT",
            kind=BillingTransaction.Kind.RENEWAL,
            amount=Decimal("10000.00"),
            currency="NGN",
            email=self.other_owner.email,
            metadata={"api_key": "do-not-return"},
            paystack_access_code="do-not-return",
        )
        payment.status = BillingTransaction.Status.SUCCESS
        payment.is_fulfilled = True
        payment.paid_at = timezone.now()
        payment.save(update_fields=["status", "is_fulfilled", "paid_at"])
        record_platform_event(
            actor=self.platform_admin,
            action="test.admin_action",
            target_type="Organization",
            target_id=self.other_organization.pk,
            organization=self.other_organization,
            reason="Test event",
            after={"is_active": True},
        )

        self.authenticate(self.platform_admin)
        response = self.client.get("/api/v1/platform/payments/?search=CROSS-TENANT")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        result = response.data["results"][0]
        self.assertEqual(result["subscription_plan"], self.monthly_plan.name)
        for sensitive_field in ("metadata", "paystack_access_code", "paystack_transaction_id", "email"):
            self.assertNotIn(sensitive_field, result)

        filtered = self.client.get(
            "/api/v1/platform/payments/",
            {"organization": str(self.other_organization.pk), "status": "SUCCESS"},
        )
        self.assertEqual(filtered.data["count"], 1)
        self.assertEqual(
            self.client.get("/api/v1/platform/audit/").data["count"],
            1,
        )

        self.authenticate(self.owner)
        tenant_activity = self.client.get("/api/v1/dashboard/activity/")
        self.assertEqual(tenant_activity.status_code, status.HTTP_200_OK)
        self.assertEqual(tenant_activity.data["count"], 0)
        self.authenticate(self.platform_admin)
        self.assertEqual(self.client.delete("/api/v1/platform/payments/").status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_mutation_and_audit_event_roll_back_together(self):
        self.authenticate(self.platform_admin)
        self.client.raise_request_exception = False
        with patch(
            "apps.platform_admin.views.record_platform_event",
            side_effect=RuntimeError("audit write failed"),
        ):
            response = self.client.post(
                f"/api/v1/platform/organizations/{self.organization.pk}/suspend/",
                {"reason": "Audit rollback check"},
                format="json",
            )
        self.organization.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertTrue(self.organization.is_active)
        self.assertFalse(PlatformAuditEvent.objects.filter(reason="Audit rollback check").exists())