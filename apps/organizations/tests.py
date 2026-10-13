from io import BytesIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from PIL import Image
from rest_framework import status
from rest_framework.test import APIClient

from apps.dashboard.models import ActivityEvent
from apps.organizations.models import Membership, Organization

User = get_user_model()
STRONG_PASSWORD = "StrongPass123!"


def make_logo(name="business-logo.png", image_format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (32, 32), color=(37, 99, 235)).save(buffer, format=image_format)
    content_type = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
    }[image_format]
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)


@override_settings(LOGO_STORAGE_IS_PERSISTENT=True)
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
        self.assertIsNone(response.data["logo"])
        org = Organization.objects.get(name="My Business")
        self.assertEqual(org.business_type, "Retail")
        self.assertEqual(org.currency, "NGN")
        self.assertTrue(org.memberships.filter(user=self.owner_user, role=Membership.Role.OWNER, is_active=True).exists())
        duplicate_response = self.client.post(
            "/api/v1/organizations/",
            {
                "name": "My Business",
                "business_type": "Retail",
                "currency": "NGN",
                "logo": make_logo(),
            },
            format="multipart",
        )
        self.assertEqual(duplicate_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Organization.objects.count(), 1)
        self.assertTrue(ActivityEvent.objects.filter(
            organization=org,
            actor=self.owner_user,
            action="organization.created",
            entity_id=str(org.pk),
        ).exists())

    def test_authenticated_user_can_create_organization_with_logo(self):
        self.client.force_authenticate(user=self.owner_user)

        response = self.client.post(
            "/api/v1/organizations/",
            {
                "name": "Logo Business",
                "business_type": "Retail",
                "currency": "NGN",
                "logo": make_logo(),
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Organization.objects.count(), 1)
        organization = Organization.objects.get()
        self.assertTrue(organization.logo.name.startswith(f"organization-logos/{organization.pk}/"))
        self.assertTrue(response.data["logo"].startswith("http://testserver/media/"))
        self.assertTrue(organization.memberships.filter(user=self.owner_user).exists())

    def test_manager_and_cashier_cannot_create_an_additional_business(self):
        manager_organization = Organization.objects.create(
            name="Manager Workspace",
            slug="manager-workspace",
        )
        cashier_organization = Organization.objects.create(
            name="Cashier Workspace",
            slug="cashier-workspace",
        )
        Membership.objects.create(
            user=self.manager_user,
            organization=manager_organization,
            role=Membership.Role.MANAGER,
            is_active=True,
        )
        Membership.objects.create(
            user=self.cashier_user,
            organization=cashier_organization,
            role=Membership.Role.CASHIER,
            is_active=True,
        )

        for user, name in (
            (self.manager_user, "Manager New Business"),
            (self.cashier_user, "Cashier New Business"),
        ):
            self.client.force_authenticate(user=user)
            response = self.client.post(
                "/api/v1/organizations/",
                {"name": name, "business_type": "Retail", "currency": "NGN"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(Organization.objects.count(), 2)

    def test_inactive_staff_membership_cannot_be_used_to_create_a_business(self):
        organization = Organization.objects.create(
            name="Historical Workspace",
            slug="historical-workspace",
        )
        Membership.objects.create(
            user=self.manager_user,
            organization=organization,
            role=Membership.Role.MANAGER,
            is_active=False,
        )

        self.client.force_authenticate(user=self.manager_user)
        response = self.client.post(
            "/api/v1/organizations/",
            {
                "name": "Unauthorized New Business",
                "business_type": "Retail",
                "currency": "NGN",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Organization.objects.count(), 1)

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

    def test_owner_can_replace_and_remove_current_organization_logo(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.owner_user, organization=org, role=Membership.Role.OWNER, is_active=True)
        org.logo.save("initial.png", make_logo(), save=True)
        original_name = org.logo.name

        self.client.force_authenticate(user=self.owner_user)
        with self.captureOnCommitCallbacks(execute=True):
            replacement_response = self.client.patch(
                "/api/v1/organizations/current/",
                {"logo": make_logo("replacement.png")},
                format="multipart",
            )

        self.assertEqual(replacement_response.status_code, status.HTTP_200_OK)
        org.refresh_from_db()
        replacement_name = org.logo.name
        self.assertNotEqual(replacement_name, original_name)
        self.assertFalse(default_storage.exists(original_name))
        self.assertTrue(replacement_response.data["logo"].startswith("http://testserver/"))

        with self.captureOnCommitCallbacks(execute=True):
            removal_response = self.client.patch(
                "/api/v1/organizations/current/",
                {"remove_logo": "true"},
                format="multipart",
            )

        self.assertEqual(removal_response.status_code, status.HTTP_200_OK)
        self.assertIsNone(removal_response.data["logo"])
        org.refresh_from_db()
        self.assertFalse(org.logo)
        self.assertFalse(default_storage.exists(replacement_name))

    def test_logo_update_uses_membership_not_client_organization_id(self):
        own_org = Organization.objects.create(name="My Business", slug="my-business")
        other_org = Organization.objects.create(name="Other Business", slug="other-business")
        Membership.objects.create(user=self.owner_user, organization=own_org, role=Membership.Role.OWNER, is_active=True)
        other_logo = make_logo("other.png")
        other_org.logo.save(other_logo.name, other_logo, save=True)
        other_logo_name = other_org.logo.name

        self.client.force_authenticate(user=self.owner_user)
        response = self.client.patch(
            "/api/v1/organizations/current/",
            {"organization_id": str(other_org.pk), "logo": make_logo()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        own_org.refresh_from_db()
        other_org.refresh_from_db()
        self.assertTrue(own_org.logo)
        self.assertEqual(other_org.logo.name, other_logo_name)

    def test_manager_cannot_change_current_organization_logo(self):
        org = Organization.objects.create(name="My Business", slug="my-business")
        Membership.objects.create(user=self.manager_user, organization=org, role=Membership.Role.MANAGER, is_active=True)

        self.client.force_authenticate(user=self.manager_user)
        response = self.client.patch(
            "/api/v1/organizations/current/",
            {"logo": make_logo()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        org.refresh_from_db()
        self.assertFalse(org.logo)

    def test_invalid_and_oversized_logo_uploads_are_rejected(self):
        self.client.force_authenticate(user=self.owner_user)
        invalid_response = self.client.post(
            "/api/v1/organizations/",
            {
                "name": "Invalid Logo Business",
                "business_type": "Retail",
                "currency": "NGN",
                "logo": SimpleUploadedFile(
                    "not-an-image.png", b"not an image", content_type="image/png"
                ),
            },
            format="multipart",
        )
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Organization.objects.count(), 0)
        self.assertIn("logo", invalid_response.data)

        logo = make_logo()
        oversized = SimpleUploadedFile(
            "large.png",
            logo.read() + b"x" * (2 * 1024 * 1024),
            content_type="image/png",
        )
        oversized_response = self.client.post(
            "/api/v1/organizations/",
            {
                "name": "Large Logo Business",
                "business_type": "Retail",
                "currency": "NGN",
                "logo": oversized,
            },
            format="multipart",
        )
        self.assertEqual(oversized_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Organization.objects.count(), 0)
        self.assertIn("2 MB or smaller", str(oversized_response.data["logo"]))

    @override_settings(DEBUG=False, LOGO_STORAGE_IS_PERSISTENT=False)
    def test_logo_upload_is_disabled_without_production_persistent_storage(self):
        self.client.force_authenticate(user=self.owner_user)

        response = self.client.post(
            "/api/v1/organizations/",
            {
                "name": "No Persistent Storage",
                "business_type": "Retail",
                "currency": "NGN",
                "logo": make_logo(),
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Organization.objects.count(), 0)
        self.assertIn("persistent media storage", str(response.data["logo"]))

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
