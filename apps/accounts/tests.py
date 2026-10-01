from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

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


class AuthenticationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="owner@example.com", password=STRONG_PASSWORD)

    def test_valid_login_succeeds(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            {"email": "owner@example.com", "password": STRONG_PASSWORD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

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
