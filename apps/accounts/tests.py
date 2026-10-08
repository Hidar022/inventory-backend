from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.dashboard.models import ActivityEvent
from apps.organizations.models import Membership, Organization

User = get_user_model()
STRONG_PASSWORD = "StrongPass123!"


class RegistrationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_registration_succeeds(self):
        payload = {
            "email": "owner@example.com",
            "password": STRONG_PASSWORD,
            "first_name": "Admin",
            "last_name": "User",
        }

        response = self.client.post("/api/v1/auth/register/", payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get(email="owner@example.com")
        self.assertEqual(user.first_name, "Admin")
        self.assertEqual(user.last_name, "User")
        self.assertTrue(user.check_password(STRONG_PASSWORD))
        self.assertEqual(len(mail.outbox), 1)
        welcome_email = mail.outbox[0]
        self.assertIn("/login", welcome_email.body)
        self.assertIn("Set up your business", welcome_email.body)
        self.assertNotIn(STRONG_PASSWORD, welcome_email.body)
        self.assertEqual(len(welcome_email.alternatives), 1)
        self.assertIn("Set up your business", welcome_email.alternatives[0][0])

    def test_duplicate_email_rejected(self):
        User.objects.create_user(email="owner@example.com", password=STRONG_PASSWORD)

        response = self.client.post(
            "/api/v1/auth/register/",
            {"email": "owner@example.com", "password": STRONG_PASSWORD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_invalid_email_rejected(self):
        response = self.client.post(
            "/api/v1/auth/register/",
            {"email": "not-an-email", "password": STRONG_PASSWORD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_weak_password_rejected(self):
        response = self.client.post(
            "/api/v1/auth/register/",
            {"email": "owner@example.com", "password": "123"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)

    def test_password_is_never_returned(self):
        response = self.client.post(
            "/api/v1/auth/register/",
            {"email": "owner@example.com", "password": STRONG_PASSWORD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("password", response.data)


class PasswordResetAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="owner@example.com", password=STRONG_PASSWORD)

    def test_reset_request_returns_generic_response_for_unknown_email(self):
        response = self.client.post(
            "/api/v1/auth/password/reset/request/",
            {"email": "missing@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["detail"],
            "If an account exists for this email, we've sent a password reset link.",
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_request_sends_email_for_existing_user(self):
        response = self.client.post(
            "/api/v1/auth/password/reset/request/",
            {"email": "owner@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["detail"],
            "If an account exists for this email, we've sent a password reset link.",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Reset your password", mail.outbox[0].subject)
        self.assertIn("/reset-password/", mail.outbox[0].body)
        self.assertNotIn(STRONG_PASSWORD, mail.outbox[0].body)

    def test_reset_token_allows_one_time_password_change(self):
        self.client.post(
            "/api/v1/auth/password/reset/request/",
            {"email": "owner@example.com"},
            format="json",
        )
        token = self.extract_reset_token(mail.outbox[0].body)

        response = self.client.post(
            "/api/v1/auth/password/reset/confirm/",
            {"token": token, "password": "NewStrongPass456!"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewStrongPass456!"))

        replay = self.client.post(
            "/api/v1/auth/password/reset/confirm/",
            {"token": token, "password": "AnotherStrongPass789!"},
            format="json",
        )

        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("token", replay.data)

    def test_invalid_token_is_rejected(self):
        response = self.client.post(
            "/api/v1/auth/password/reset/confirm/",
            {"token": "bad-token", "password": "NewStrongPass456!"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("token", response.data)

    @staticmethod
    def extract_reset_token(email_body):
        marker = "/reset-password/"
        start = email_body.index(marker)
        token = email_body[start + len(marker) :].split()[0].strip()
        return token


class AuthenticationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="owner@example.com", password=STRONG_PASSWORD)

    def test_valid_login_succeeds(self):
        organization = Organization.objects.create(name="Login org", slug="login-org")
        Membership.objects.create(
            user=self.user,
            organization=organization,
            role=Membership.Role.OWNER,
        )
        response = self.client.post(
            "/api/v1/auth/token/",
            {"email": "owner@example.com", "password": STRONG_PASSWORD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        event = ActivityEvent.objects.get(
            organization=organization,
            action="authentication.login",
        )
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.entity_id, str(self.user.pk))
        self.assertEqual(event.metadata, {})

    def test_invalid_password_rejected(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            {"email": "owner@example.com", "password": "wrong-password"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unknown_user_rejected(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            {"email": "missing@example.com", "password": STRONG_PASSWORD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_succeeds(self):
        token_response = self.client.post(
            "/api/v1/auth/token/",
            {"email": "owner@example.com", "password": STRONG_PASSWORD},
            format="json",
        )
        refresh = token_response.data["refresh"]

        response = self.client.post(
            "/api/v1/auth/token/refresh/",
            {"refresh": refresh},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_me_requires_authentication(self):
        response = self.client.get("/api/v1/auth/me/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_authenticated_user_context(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/auth/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "owner@example.com")
        self.assertIn("organization", response.data)
        self.assertIn("role", response.data)

    def test_me_returns_no_organization_when_user_has_no_membership(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/auth/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["organization"])
        self.assertIsNone(response.data["role"])
