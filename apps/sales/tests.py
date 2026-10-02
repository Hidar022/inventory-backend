from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Category, Product
from apps.inventory.models import StockMovement
from apps.organizations.models import Membership, Organization
from apps.sales.models import Payment, Sale, SaleItem

User = get_user_model()
PASSWORD = "StrongPass123!"


class PosCheckoutAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org_a = Organization.objects.create(name="Org A", slug="org-a")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b")
        self.owner = self.make_member("owner@example.com", self.org_a, Membership.Role.OWNER)
        self.manager = self.make_member("manager@example.com", self.org_a, Membership.Role.MANAGER)
        self.cashier = self.make_member("cashier@example.com", self.org_a, Membership.Role.CASHIER)
        self.other_owner = self.make_member("other@example.com", self.org_b, Membership.Role.OWNER)
        self.category = Category.objects.create(organization=self.org_a, name="Beverages")
        self.other_category = Category.objects.create(organization=self.org_b, name="Other")
        self.product = Product.objects.create(
            organization=self.org_a,
            category=self.category,
            name="Milk",
            sku="MILK-1",
            selling_price=Decimal("45.00"),
            cost_price=Decimal("30.00"),
            stock_quantity=10,
        )
        self.foreign_product = Product.objects.create(
            organization=self.org_b,
            category=self.other_category,
            name="Foreign Milk",
            sku="FOREIGN-1",
            selling_price=Decimal("50.00"),
            cost_price=Decimal("35.00"),
            stock_quantity=20,
        )

    def make_member(self, email, organization, role, is_active=True):
        user = User.objects.create_user(email=email, password=PASSWORD)
        Membership.objects.create(
            user=user,
            organization=organization,
            role=role,
            is_active=is_active,
        )
        return user

    def authenticate(self, user):
        self.client.force_authenticate(user=user)

    def checkout_payload(self, **overrides):
        payload = {
            "idempotency_key": "checkout-abc-123",
            "items": [{"product_id": self.product.pk, "quantity": 2}],
            "discount": "0.00",
            "payments": [{"method": "cash", "amount": "90.00"}],
        }
        payload.update(overrides)
        return payload

    def test_owner_checkout_succeeds_and_deducts_stock(self):
        self.authenticate(self.owner)
        response = self.client.post("/api/v1/pos/checkout/", self.checkout_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(response.data["subtotal"], "90.00")
        self.assertEqual(response.data["total"], "90.00")
        self.assertEqual(response.data["payment_status"], "PAID")
        self.assertTrue(response.data["receipt_number"].startswith("REC-"))

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 1)
        self.assertEqual(
            StockMovement.objects.get(product=self.product).movement_type,
            StockMovement.MovementType.SALE_OUT,
        )

    def test_cashier_discount_is_rejected(self):
        self.authenticate(self.cashier)
        response = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(discount="10.00"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Sale.objects.count(), 0)

    def test_exact_payment_and_validation_rules(self):
        self.authenticate(self.owner)

        exact = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(payments=[{"method": "cash", "amount": "90.00"}]),
            format="json",
        )
        self.assertEqual(exact.status_code, status.HTTP_201_CREATED)

        under = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="checkout-underpay",
                payments=[{"method": "cash", "amount": "80.00"}],
            ),
            format="json",
        )
        self.assertEqual(under.status_code, status.HTTP_400_BAD_REQUEST)

        over = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="checkout-overpay",
                payments=[{"method": "cash", "amount": "100.00"}],
            ),
            format="json",
        )
        self.assertEqual(over.status_code, status.HTTP_400_BAD_REQUEST)

        invalid_method = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="checkout-unsupported-method",
                payments=[{"method": "card", "amount": "90.00"}],
            ),
            format="json",
        )
        self.assertEqual(invalid_method.status_code, status.HTTP_400_BAD_REQUEST)

        multiple = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="checkout-multiple-payments",
                payments=[
                    {"method": "cash", "amount": "60.00"},
                    {"method": "transfer", "amount": "30.00"},
                ],
            ),
            format="json",
        )
        self.assertEqual(multiple.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Payment.objects.filter(sale__id=multiple.data["id"]).count(), 2)

    def test_zero_total_sale_is_supported_and_deterministic(self):
        self.authenticate(self.owner)
        response = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="zero-total-sale",
                items=[{"product_id": self.product.pk, "quantity": 2}],
                discount="90.00",
                payments=[],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["total"], "0.00")
        self.assertEqual(response.data["payment_status"], "PAID")
        self.assertEqual(Payment.objects.filter(sale_id=response.data["id"]).count(), 0)

    def test_same_idempotency_key_reuses_existing_sale_and_records(self):
        self.authenticate(self.owner)
        first = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(idempotency_key="same-key-1"),
            format="json",
        )
        second = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(idempotency_key="same-key-1"),
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(Sale.objects.filter(idempotency_key="same-key-1").count(), 1)
        self.assertEqual(SaleItem.objects.filter(sale_id=first.data["id"]).count(), 1)
        self.assertEqual(Payment.objects.filter(sale_id=first.data["id"]).count(), 1)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 1)
        self.assertEqual(first.data["id"], second.data["id"])

    def test_same_key_different_payload_conflicts(self):
        self.authenticate(self.owner)
        first = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="same-key-2",
                items=[{"product_id": self.product.pk, "quantity": 1}],
                payments=[{"method": "cash", "amount": "45.00"}],
            ),
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="same-key-2",
                items=[{"product_id": self.product.pk, "quantity": 2}],
                payments=[{"method": "cash", "amount": "90.00"}],
            ),
            format="json",
        )

        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.filter(idempotency_key="same-key-2").count(), 1)

    def test_receipt_number_collision_retries_without_aborting_checkout(self):
        self.authenticate(self.owner)
        first = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(idempotency_key="receipt-first"),
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        with patch(
            "apps.sales.services.generate_receipt_number",
            side_effect=[first.data["receipt_number"], "REC-20990101-000002"],
        ):
            second = self.client.post(
                "/api/v1/pos/checkout/",
                self.checkout_payload(idempotency_key="receipt-second"),
                format="json",
            )

        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(first.data["receipt_number"], second.data["receipt_number"])
        self.assertEqual(Sale.objects.count(), 2)

    def test_same_idempotency_key_is_independent_across_organizations(self):
        self.authenticate(self.owner)
        first = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(idempotency_key="shared-key"),
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        other_user = self.make_member("owner-b@example.com", self.org_b, Membership.Role.OWNER)
        other_client = APIClient()
        other_client.force_authenticate(user=other_user)
        other_payload = {
            "idempotency_key": "shared-key",
            "items": [{"product_id": self.foreign_product.pk, "quantity": 1}],
            "discount": "0.00",
            "payments": [{"method": "cash", "amount": "50.00"}],
        }
        second = other_client.post("/api/v1/pos/checkout/", other_payload, format="json")

        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Sale.objects.filter(idempotency_key="shared-key").count(), 2)

    def test_duplicate_product_items_are_normalized_before_stock_deduction(self):
        self.authenticate(self.owner)
        response = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="duplicate-item-key",
                items=[
                    {"product_id": self.product.pk, "quantity": 2},
                    {"product_id": self.product.pk, "quantity": 3},
                ],
                payments=[{"method": "cash", "amount": "225.00"}],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SaleItem.objects.filter(sale_id=response.data["id"]).count(), 1)
        self.assertEqual(response.data["subtotal"], "225.00")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 5)

    def test_inactive_product_is_rejected_and_does_not_create_sale_or_movement(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        self.authenticate(self.owner)
        response = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_foreign_product_cannot_be_checked_out(self):
        self.authenticate(self.owner)
        response = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(
                idempotency_key="foreign-key",
                items=[{"product_id": self.foreign_product.pk, "quantity": 1}],
                payments=[{"method": "cash", "amount": "50.00"}],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 0)

    def test_full_rollback_after_late_failure(self):
        self.authenticate(self.owner)
        self.client.raise_request_exception = False
        with patch("apps.sales.services.StockMovement.objects.bulk_create", side_effect=Exception("boom")):
            response = self.client.post(
                "/api/v1/pos/checkout/",
                self.checkout_payload(idempotency_key="rollback-key"),
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

    def test_sales_list_and_detail_are_role_scoped(self):
        self.authenticate(self.owner)
        first = self.client.post(
            "/api/v1/pos/checkout/",
            self.checkout_payload(idempotency_key="owner-sale-1"),
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        owner_detail = self.client.get(f"/api/v1/sales/{first.data['id']}/")
        self.assertEqual(owner_detail.status_code, status.HTTP_200_OK)

        self.authenticate(self.cashier)
        list_response = self.client.get("/api/v1/sales/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 0)
        cashier_detail = self.client.get(f"/api/v1/sales/{first.data['id']}/")
        self.assertEqual(cashier_detail.status_code, status.HTTP_404_NOT_FOUND)

        self.authenticate(self.manager)
        manager_list = self.client.get("/api/v1/sales/")
        self.assertEqual(manager_list.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(manager_list.data["count"], 1)
        manager_detail = self.client.get(f"/api/v1/sales/{first.data['id']}/")
        self.assertEqual(manager_detail.status_code, status.HTTP_200_OK)


@skipUnlessDBFeature("has_select_for_update")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CheckoutConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Concurrent Sales", slug="concurrent-sales")
        self.user = User.objects.create_user(email="concurrent@example.com", password=PASSWORD)
        Membership.objects.create(
            user=self.user,
            organization=self.organization,
            role=Membership.Role.OWNER,
        )
        category = Category.objects.create(organization=self.organization, name="Concurrency")
        self.product = Product.objects.create(
            organization=self.organization,
            category=category,
            name="Limited stock",
            sku="LIMITED-1",
            selling_price=Decimal("45.00"),
            cost_price=Decimal("30.00"),
            stock_quantity=1,
        )

    def test_competing_checkouts_do_not_oversell_stock(self):
        user_id = self.user.pk
        product_id = self.product.pk

        def checkout_once(idempotency_key):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=user_id))
                response = client.post(
                    "/api/v1/pos/checkout/",
                    {
                        "idempotency_key": idempotency_key,
                        "items": [{"product_id": product_id, "quantity": 1}],
                        "discount": "0.00",
                        "payments": [{"method": "cash", "amount": "45.00"}],
                    },
                    format="json",
                )
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(checkout_once, ["concurrent-a", "concurrent-b"]))

        self.assertCountEqual(responses, [status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 0)
        self.assertEqual(Sale.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(SaleItem.objects.filter(sale__organization=self.organization).count(), 1)
        self.assertEqual(Payment.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(StockMovement.objects.filter(organization=self.organization).count(), 1)
