from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

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
