from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.dashboard.models import ActivityEvent
from apps.organizations.models import Membership, Organization

User = get_user_model()
STRONG_PASSWORD = "StrongPass123!"


class OrganizationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner_user = User.objects.create_user(email="owner@example.com", password=STRONG_PASSWORD)
        self.manager_user = User.objects.create_user(email="manager@example.com", password=STRONG_PASSWORD)
        self.cashier_user = User.objects.create_user(email="cashier@example.com", password=STRONG_PASSWORD)

    def test_authenticated_user_can_create_organization(self):
        self.client.force_authenticate(user=self.owner_user)

        response = self.client.post(
            "/api/v1/organizations/",
            {"name": "My Business", "business_type": "Retail", "currency": "NGN"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Organization.objects.count(), 1)
        org = Organization.objects.get(name="My Business")
        self.assertEqual(org.business_type, "Retail")
        self.assertEqual(org.currency, "NGN")
        self.assertTrue(org.memberships.filter(user=self.owner_user, role=Membership.Role.OWNER, is_active=True).exists())
        self.assertTrue(ActivityEvent.objects.filter(
            organization=org,
            actor=self.owner_user,
            action="organization.created",
            entity_id=str(org.pk),
        ).exists())

    def test_current_organization_can_be_retrieved(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.owner_user, organization=org, role=Membership.Role.OWNER, is_active=True)

        self.client.force_authenticate(user=self.owner_user)
        response = self.client.get("/api/v1/organizations/current/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "My Business")

    def test_owner_can_update_current_organization(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.owner_user, organization=org, role=Membership.Role.OWNER, is_active=True)

        self.client.force_authenticate(user=self.owner_user)
        response = self.client.patch(
            "/api/v1/organizations/current/",
            {"name": "Updated Business", "business_type": "Wholesale"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        org.refresh_from_db()
        self.assertEqual(org.name, "Updated Business")
        self.assertEqual(org.business_type, "Wholesale")
        self.assertTrue(ActivityEvent.objects.filter(
            organization=org,
            actor=self.owner_user,
            action="organization.updated",
            entity_id=str(org.pk),
        ).exists())

    def test_manager_cannot_update_current_organization(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.manager_user, organization=org, role=Membership.Role.MANAGER, is_active=True)

        self.client.force_authenticate(user=self.manager_user)
        response = self.client.patch(
            "/api/v1/organizations/current/",
            {"name": "Nope"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cashier_cannot_update_current_organization(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.cashier_user, organization=org, role=Membership.Role.CASHIER, is_active=True)

        self.client.force_authenticate(user=self.cashier_user)
        response = self.client.patch(
            "/api/v1/organizations/current/",
            {"name": "Nope"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_active_member_can_access_organization(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.owner_user, organization=org, role=Membership.Role.OWNER, is_active=True)

        self.client.force_authenticate(user=self.owner_user)
        response = self.client.get("/api/v1/organizations/current/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_inactive_member_cannot_access_organization(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.owner_user, organization=org, role=Membership.Role.OWNER, is_active=False)

        self.client.force_authenticate(user=self.owner_user)
        response = self.client.get("/api/v1/organizations/current/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_member_cannot_access_organization(self):
        org = Organization.objects.create(name="My Business", slug="my-business")

        self.client.force_authenticate(user=self.owner_user)
        response = self.client.get("/api/v1/organizations/current/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_cannot_access_other_organizations(self):
        org_a = Organization.objects.create(name="Org A", slug="org-a")
        org_b = Organization.objects.create(name="Org B", slug="org-b")
        Membership.objects.create(user=self.owner_user, organization=org_a, role=Membership.Role.OWNER, is_active=True)
        user_b = User.objects.create_user(email="other@example.com", password=STRONG_PASSWORD)
        Membership.objects.create(user=user_b, organization=org_b, role=Membership.Role.OWNER, is_active=True)

        self.client.force_authenticate(user=self.owner_user)
        response = self.client.get(f"/api/v1/organizations/{org_b.pk}/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TeamManagementAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(email="owner@example.com", password=STRONG_PASSWORD)
        self.manager = User.objects.create_user(email="manager@example.com", password=STRONG_PASSWORD)
        self.cashier = User.objects.create_user(email="cashier@example.com", password=STRONG_PASSWORD)
        self.org = Organization.objects.create(name="Acme", slug="acme")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER, is_active=True)
        Membership.objects.create(user=self.manager, organization=self.org, role=Membership.Role.MANAGER, is_active=True)
        Membership.objects.create(user=self.cashier, organization=self.org, role=Membership.Role.CASHIER, is_active=True)

    def test_owner_lists_team(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.get("/api/v1/team/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data["results"]), 3)

    def test_owner_can_create_manager_invitation(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            "/api/v1/team/",
            {"name": "Jane Smith", "email": "jane@example.com", "role": "MANAGER"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ActivityEvent.objects.filter(
            organization=self.org,
            actor=self.owner,
            action="team.invitation_created",
        ).exists())
        self.assertTrue(User.objects.filter(email="jane@example.com").exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("invite", mail.outbox[0].body)
        self.assertEqual(len(mail.outbox[0].alternatives), 1)
        self.assertIn("Accept invitation", mail.outbox[0].alternatives[0][0])

    @staticmethod
    def extract_invitation_token(email_body):
        marker = "/invite/"
        start = email_body.find(marker)
        self_assert = start != -1
        if not self_assert:
            raise AssertionError("Invitation link not found in email body.")
        return email_body[start + len(marker):].split()[0].strip()

    def test_manager_cannot_create_staff(self):
        self.client.force_authenticate(user=self.manager)
        response = self.client.post(
            "/api/v1/team/",
            {"name": "No Access", "email": "noaccess@example.com", "role": "CASHIER"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cashier_cannot_create_staff(self):
        self.client.force_authenticate(user=self.cashier)
        response = self.client.post(
            "/api/v1/team/",
            {"name": "No Access", "email": "noaccess2@example.com", "role": "MANAGER"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pending_invitation_setup_sets_password_and_activates_membership(self):
        self.client.force_authenticate(user=self.owner)
        create_response = self.client.post(
            "/api/v1/team/",
            {"name": "Invitee User", "email": "invitee@example.com", "role": "CASHIER"},
            format="json",
        )
        token = self.extract_invitation_token(mail.outbox[-1].body)

        setup_response = self.client.post(
            "/api/v1/team/invitations/accept/",
            {"token": token, "password": "StrongPass123!"},
            format="json",
        )

        self.assertEqual(setup_response.status_code, status.HTTP_200_OK)
        user = User.objects.get(email="invitee@example.com")
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("StrongPass123!"))
        self.assertTrue(user.organization_memberships.filter(organization=self.org, is_active=True).exists())
        self.assertTrue(ActivityEvent.objects.filter(
            organization=self.org,
            actor=user,
            action="team.invitation_accepted",
        ).exists())

    def test_deactivated_staff_cannot_authenticate(self):
        self.client.force_authenticate(user=self.owner)
        self.client.post(
            "/api/v1/team/",
            {"name": "Deactivated User", "email": "deactivated@example.com", "role": "CASHIER"},
            format="json",
        )
        token = self.extract_invitation_token(mail.outbox[-1].body)
        self.client.post(
            "/api/v1/team/invitations/accept/",
            {"token": token, "password": "StrongPass123!"},
            format="json",
        )
        deactivated_user = User.objects.get(email="deactivated@example.com")

        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            f"/api/v1/team/{deactivated_user.pk}/deactivate/",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(ActivityEvent.objects.filter(
            organization=self.org,
            actor=self.owner,
            action="team.member_deactivated",
            entity_id=str(Membership.objects.get(user=deactivated_user, organization=self.org).pk),
        ).exists())

        login_response = self.client.post(
            "/api/v1/auth/token/",
            {"email": "deactivated@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login_response.status_code, status.HTTP_401_UNAUTHORIZED)

        activation = self.client.post(
            f"/api/v1/team/{deactivated_user.pk}/activate/",
            format="json",
        )
        self.assertEqual(activation.status_code, status.HTTP_200_OK)
        self.assertTrue(ActivityEvent.objects.filter(
            organization=self.org,
            actor=self.owner,
            action="team.member_activated",
        ).exists())
