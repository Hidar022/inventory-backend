from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Category, Product
from apps.inventory.models import StockMovement
from apps.organizations.models import Membership, Organization
from apps.sales.models import Payment, Sale, SaleItem, SaleReturn, SaleReturnItem

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


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SalesReadAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="Sales Read", slug="sales-read")
        self.other_organization = Organization.objects.create(name="Other Sales", slug="other-sales")
        self.owner = self.make_member("read-owner@example.com", self.organization, Membership.Role.OWNER)
        self.manager = self.make_member("read-manager@example.com", self.organization, Membership.Role.MANAGER)
        self.cashier_a = self.make_member("read-a@example.com", self.organization, Membership.Role.CASHIER)
        self.cashier_b = self.make_member("read-b@example.com", self.organization, Membership.Role.CASHIER)
        self.foreign_owner = self.make_member("read-foreign@example.com", self.other_organization, Membership.Role.OWNER)
        self.product = Product.objects.create(
            organization=self.organization,
            name="Green Tea",
            sku="TEA-1",
            selling_price=Decimal("12.50"),
            cost_price=Decimal("5.00"),
            stock_quantity=10,
        )
        self.other_product = Product.objects.create(
            organization=self.other_organization,
            name="Foreign Tea",
            sku="FOREIGN-TEA",
            selling_price=Decimal("99.00"),
            cost_price=Decimal("40.00"),
            stock_quantity=4,
        )

    def make_member(self, email, organization, role):
        user = User.objects.create_user(email=email, password=PASSWORD)
        Membership.objects.create(user=user, organization=organization, role=role)
        return user

    def create_sale(self, cashier, receipt, amount="12.50", *, organization=None, product=None,
                    payment_method=Payment.Method.CASH, payment_status=Sale.PaymentStatus.PAID,
                    status_value=Sale.Status.COMPLETED, created_at=None):
        organization = organization or self.organization
        product = product or self.product
        sale = Sale.objects.create(
            organization=organization,
            receipt_number=receipt,
            cashier=cashier,
            subtotal=Decimal(amount),
            discount=Decimal("0.00"),
            total=Decimal(amount),
            status=status_value,
            payment_status=payment_status,
            idempotency_key=receipt,
        )
        SaleItem.objects.create(
            sale=sale,
            product=product,
            product_name=product.name,
            product_sku=product.sku,
            quantity=1,
            unit_price=Decimal(amount),
            line_total=Decimal(amount),
        )
        if payment_status == Sale.PaymentStatus.PAID:
            Payment.objects.create(
                sale=sale,
                organization=organization,
                method=payment_method,
                amount=Decimal(amount),
                created_by=cashier,
            )
        if created_at:
            Sale.objects.filter(pk=sale.pk).update(created_at=created_at)
        return sale

    def test_roles_and_tenant_scope_sales_list_and_detail(self):
        sale_a = self.create_sale(self.cashier_a, "REC-A-1")
        sale_b = self.create_sale(self.cashier_b, "REC-B-1")
        foreign_sale = self.create_sale(
            self.foreign_owner,
            "REC-FOREIGN-1",
            organization=self.other_organization,
            product=self.other_product,
        )

        for user in (self.owner, self.manager):
            self.client.force_authenticate(user=user)
            response = self.client.get("/api/v1/sales/?organization_id=" + str(self.other_organization.pk))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual({row["id"] for row in response.data["results"]}, {str(sale_a.pk), str(sale_b.pk)})

        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.get(f"/api/v1/sales/?cashier={self.cashier_b.pk}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row["id"] for row in response.data["results"]], [])
        own_sales = self.client.get("/api/v1/sales/")
        self.assertEqual([row["id"] for row in own_sales.data["results"]], [str(sale_a.pk)])
        self.assertEqual(self.client.get(f"/api/v1/sales/{sale_b.pk}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(f"/api/v1/sales/{foreign_sale.pk}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(f"/api/v1/sales/{sale_a.pk}/").status_code, status.HTTP_200_OK)

    def test_filters_search_payment_and_lagos_local_date_boundaries(self):
        day = timezone.localdate()
        local_zone = ZoneInfo("Africa/Lagos")
        start = timezone.make_aware(datetime.combine(day, datetime.min.time()), local_zone)
        next_start = timezone.make_aware(datetime.combine(day + timedelta(days=1), datetime.min.time()), local_zone)
        previous_sale = self.create_sale(
            self.cashier_a,
            "REC-PREVIOUS",
            created_at=start - timedelta(seconds=1),
        )
        current_sale = self.create_sale(
            self.cashier_a,
            "REC-CURRENT",
            payment_method=Payment.Method.TRANSFER,
            created_at=start,
        )
        self.create_sale(self.cashier_b, "REC-NEXT", created_at=next_start)
        self.create_sale(
            self.cashier_b,
            "REC-UNPAID",
            payment_status=Sale.PaymentStatus.UNPAID,
            created_at=next_start + timedelta(minutes=1),
        )
        self.client.force_authenticate(user=self.owner)

        same_day = self.client.get(f"/api/v1/sales/?from={day.isoformat()}&to={day.isoformat()}")
        self.assertEqual(same_day.status_code, status.HTTP_200_OK)
        self.assertEqual([row["id"] for row in same_day.data["results"]], [str(current_sale.pk)])
        search = self.client.get("/api/v1/sales/?search=Green%20Tea")
        self.assertEqual(search.data["count"], 4)
        cashier_search = self.client.get("/api/v1/sales/?search=read-a%40example.com")
        self.assertEqual(cashier_search.data["count"], 2)
        transfer = self.client.get("/api/v1/sales/?payment_method=transfer")
        self.assertEqual([row["id"] for row in transfer.data["results"]], [str(current_sale.pk)])
        paid = self.client.get("/api/v1/sales/?payment_status=PAID")
        self.assertEqual(paid.data["count"], 3)
        unpaid = self.client.get("/api/v1/sales/?payment_status=UNPAID")
        self.assertEqual(unpaid.data["count"], 1)
        self.assertNotIn(str(previous_sale.pk), {row["id"] for row in same_day.data["results"]})

    def test_invalid_date_ranges_and_pagination_parameters_return_validation_errors(self):
        self.client.force_authenticate(user=self.owner)
        for query in (
            "from=invalid",
            "to=",
            "from=2026-10-03&to=2026-10-02",
            "page=invalid",
            "page_size=0",
        ):
            with self.subTest(query=query):
                response = self.client.get(f"/api/v1/sales/?{query}")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pagination_is_bounded_and_newest_first(self):
        older = self.create_sale(self.cashier_a, "REC-OLD", created_at=timezone.now() - timedelta(days=1))
        middle = self.create_sale(self.cashier_a, "REC-MIDDLE", created_at=timezone.now())
        newest = self.create_sale(self.cashier_a, "REC-NEW", created_at=timezone.now() + timedelta(seconds=1))
        self.client.force_authenticate(user=self.owner)

        first_page = self.client.get("/api/v1/sales/?page=1&page_size=2")
        second_page = self.client.get("/api/v1/sales/?page=2&page_size=2")
        self.assertEqual(first_page.status_code, status.HTTP_200_OK)
        self.assertEqual(first_page.data["count"], 3)
        self.assertEqual([row["id"] for row in first_page.data["results"]], [str(newest.pk), str(middle.pk)])
        self.assertEqual([row["id"] for row in second_page.data["results"]], [str(older.pk)])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SaleReturnAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="Returns A", slug="returns-a")
        self.foreign_organization = Organization.objects.create(name="Returns B", slug="returns-b")
        self.owner = self.make_member("returns-owner@example.com", self.organization, Membership.Role.OWNER)
        self.manager = self.make_member("returns-manager@example.com", self.organization, Membership.Role.MANAGER)
        self.cashier = self.make_member("returns-cashier@example.com", self.organization, Membership.Role.CASHIER)
        self.foreign_owner = self.make_member(
            "returns-foreign@example.com",
            self.foreign_organization,
            Membership.Role.OWNER,
        )
        self.product_a = self.make_product(self.organization, "Return Tea", "RETURN-TEA", 7)
        self.product_b = self.make_product(self.organization, "Return Coffee", "RETURN-COFFEE", 8)
        self.foreign_product = self.make_product(self.foreign_organization, "Foreign", "FOREIGN-RETURN", 3)
        self.sale, self.sale_item_a, self.sale_item_b = self.make_sale(product_b=self.product_b)
        self.foreign_sale, _, _ = self.make_sale(
            organization=self.foreign_organization,
            cashier=self.foreign_owner,
            product_a=self.foreign_product,
            product_b=None,
            receipt="FOREIGN-RETURN-RECEIPT",
        )

    def make_member(self, email, organization, role):
        user = User.objects.create_user(email=email, password=PASSWORD)
        Membership.objects.create(user=user, organization=organization, role=role)
        return user

    def make_product(self, organization, name, sku, stock):
        return Product.objects.create(
            organization=organization,
            name=name,
            sku=sku,
            selling_price=Decimal("5.00"),
            cost_price=Decimal("2.00"),
            stock_quantity=stock,
        )

    def make_sale(self, organization=None, cashier=None, product_a=None, product_b=None,
                  receipt="RETURN-RECEIPT"):
        organization = organization or self.organization
        cashier = cashier or self.cashier
        product_a = product_a or self.product_a
        sale = Sale.objects.create(
            organization=organization,
            receipt_number=receipt,
            cashier=cashier,
            subtotal=Decimal("21.00" if product_b else "15.00"),
            discount=Decimal("0.00"),
            total=Decimal("21.00" if product_b else "15.00"),
            status=Sale.Status.COMPLETED,
            payment_status=Sale.PaymentStatus.PAID,
            idempotency_key=receipt,
        )
        sale_item_a = SaleItem.objects.create(
            sale=sale,
            product=product_a,
            product_name=product_a.name,
            product_sku=product_a.sku,
            quantity=3,
            unit_price=Decimal("5.00"),
            line_total=Decimal("15.00"),
        )
        sale_item_b = None
        if product_b:
            sale_item_b = SaleItem.objects.create(
                sale=sale,
                product=product_b,
                product_name=product_b.name,
                product_sku=product_b.sku,
                quantity=2,
                unit_price=Decimal("3.00"),
                line_total=Decimal("6.00"),
            )
        Payment.objects.create(
            sale=sale,
            organization=organization,
            method=Payment.Method.CASH,
            amount=sale.total,
            created_by=cashier,
        )
        return sale, sale_item_a, sale_item_b

    def return_url(self, sale=None):
        return f"/api/v1/sales/{(sale or self.sale).pk}/returns/"

    def return_payload(self, items=None, **overrides):
        payload = {
            "items": (
                items
                if items is not None
                else [{"sale_item_id": str(self.sale_item_a.pk), "quantity": 1}]
            ),
            "reason": "Customer returned damaged item",
            "refund_method": Payment.Method.CASH,
        }
        payload.update(overrides)
        return payload

    def test_owner_partial_then_full_return_restores_stock_and_preserves_original_sale(self):
        self.client.force_authenticate(user=self.owner)
        original_payment = Payment.objects.get(sale=self.sale)
        original_item_values = (self.sale_item_a.product_name, self.sale_item_a.product_sku, self.sale_item_a.quantity)

        partial = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(partial.status_code, status.HTTP_201_CREATED, partial.data)
        self.assertEqual(partial.data["refund_amount"], "5.00")
        self.assertEqual(partial.data["sale_status"], Sale.Status.PARTIALLY_RETURNED)
        self.assertEqual(partial.data["items"][0]["product_name"], "Return Tea")
        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.stock_quantity, 8)
        movement = StockMovement.objects.get(reference_id=partial.data["id"])
        self.assertEqual(movement.movement_type, StockMovement.MovementType.SALE_RETURN)
        self.assertEqual(movement.quantity, 1)
        self.assertEqual((movement.previous_quantity, movement.new_quantity), (7, 8))
        self.assertEqual(movement.reference_type, "sale_return")

        full = self.client.post(
            self.return_url(),
            self.return_payload(
                items=[
                    {"sale_item_id": str(self.sale_item_a.pk), "quantity": 2},
                    {"sale_item_id": str(self.sale_item_b.pk), "quantity": 2},
                ],
                refund_method=Payment.Method.TRANSFER,
            ),
            format="json",
        )
        self.assertEqual(full.status_code, status.HTTP_201_CREATED, full.data)
        self.assertEqual(full.data["refund_amount"], "16.00")
        self.assertEqual(full.data["sale_status"], Sale.Status.FULLY_RETURNED)
        self.product_a.refresh_from_db()
        self.product_b.refresh_from_db()
        self.assertEqual(self.product_a.stock_quantity, 10)
        self.assertEqual(self.product_b.stock_quantity, 10)
        self.sale.refresh_from_db()
        self.sale_item_a.refresh_from_db()
        self.assertEqual(self.sale.status, Sale.Status.FULLY_RETURNED)
        self.assertEqual(
            (self.sale_item_a.product_name, self.sale_item_a.product_sku, self.sale_item_a.quantity),
            original_item_values,
        )
        original_payment.refresh_from_db()
        self.assertEqual(original_payment.amount, Decimal("21.00"))
        self.assertEqual(Payment.objects.filter(sale=self.sale).count(), 1)
        self.assertEqual(Sale.objects.filter(pk=self.sale.pk).count(), 1)

    def test_manager_can_return_cashier_history_is_scoped_and_cashier_cannot_process(self):
        self.client.force_authenticate(user=self.cashier)
        denied = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.manager)
        created = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        self.client.force_authenticate(user=self.cashier)
        history = self.client.get(self.return_url())
        self.assertEqual(history.status_code, status.HTTP_200_OK)
        self.assertEqual(len(history.data["results"]), 1)
        self.assertEqual(history.data["results"][0]["id"], created.data["id"])

    def test_foreign_sale_and_sale_item_are_not_accessible(self):
        self.client.force_authenticate(user=self.foreign_owner)
        response = self.client.post(
            self.return_url(self.sale),
            self.return_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            self.return_url(),
            self.return_payload(items=[{"sale_item_id": str(self.foreign_sale.items.first().pk), "quantity": 1}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(SaleReturn.objects.count(), 0)

    def test_cannot_return_more_than_remaining_or_return_fully_returned_item(self):
        self.client.force_authenticate(user=self.owner)
        too_many = self.client.post(
            self.return_url(),
            self.return_payload(items=[{"sale_item_id": str(self.sale_item_a.pk), "quantity": 4}]),
            format="json",
        )
        self.assertEqual(too_many.status_code, status.HTTP_400_BAD_REQUEST)

        partial = self.client.post(self.return_url(), self.return_payload(items=[{"sale_item_id": str(self.sale_item_a.pk), "quantity": 2}]), format="json")
        self.assertEqual(partial.status_code, status.HTTP_201_CREATED)
        remaining_over = self.client.post(self.return_url(), self.return_payload(items=[{"sale_item_id": str(self.sale_item_a.pk), "quantity": 2}]), format="json")
        self.assertEqual(remaining_over.status_code, status.HTTP_400_BAD_REQUEST)
        final_quantity = self.client.post(self.return_url(), self.return_payload(items=[{"sale_item_id": str(self.sale_item_a.pk), "quantity": 1}]), format="json")
        self.assertEqual(final_quantity.status_code, status.HTTP_201_CREATED)
        already_returned = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(already_returned.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_empty_zero_negative_duplicate_and_untrusted_fields(self):
        self.client.force_authenticate(user=self.owner)
        cases = [
            self.return_payload(items=[]),
            self.return_payload(items=[{"sale_item_id": str(self.sale_item_a.pk), "quantity": 0}]),
            self.return_payload(items=[{"sale_item_id": str(self.sale_item_a.pk), "quantity": -1}]),
            self.return_payload(items=[
                {"sale_item_id": str(self.sale_item_a.pk), "quantity": 1},
                {"sale_item_id": str(self.sale_item_a.pk), "quantity": 1},
            ]),
            self.return_payload(refund_method="card"),
            self.return_payload(refund_amount="0.01"),
            self.return_payload(items=[{"sale_item_id": str(self.sale_item_a.pk), "quantity": 1, "unit_price": "0.01"}]),
            {"items": "malformed", "reason": "reason", "refund_method": "cash"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post(self.return_url(), payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(SaleReturn.objects.count(), 0)

    def test_unpaid_and_non_completed_sales_cannot_be_returned(self):
        self.client.force_authenticate(user=self.owner)
        self.sale.payment_status = Sale.PaymentStatus.UNPAID
        self.sale.save(update_fields=["payment_status"])
        unpaid = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(unpaid.status_code, status.HTTP_400_BAD_REQUEST)
        self.sale.payment_status = Sale.PaymentStatus.PAID
        self.sale.status = Sale.Status.FULLY_RETURNED
        self.sale.save(update_fields=["payment_status", "status"])
        fully_returned = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(fully_returned.status_code, status.HTTP_400_BAD_REQUEST)

    def test_failure_after_return_records_rolls_back_everything(self):
        self.client.force_authenticate(user=self.owner)
        self.client.raise_request_exception = False
        with patch("apps.sales.return_services.StockMovement.objects.bulk_create", side_effect=RuntimeError("ledger failed")):
            response = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(SaleReturn.objects.count(), 0)
        self.assertEqual(SaleReturnItem.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)
        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.stock_quantity, 7)
        self.assertEqual(Payment.objects.filter(sale=self.sale).count(), 1)

    def test_cancelled_return_does_not_consume_returnable_quantity(self):
        cancelled = SaleReturn.objects.create(
            organization=self.organization,
            sale=self.sale,
            processed_by=self.owner,
            reason="Cancelled before processing",
            status=SaleReturn.Status.CANCELLED,
            refund_amount=Decimal("5.00"),
            refund_method=Payment.Method.CASH,
        )
        SaleReturnItem.objects.create(
            sale_return=cancelled,
            sale_item=self.sale_item_a,
            product=self.product_a,
            product_name=self.sale_item_a.product_name,
            product_sku=self.sale_item_a.product_sku,
            quantity_returned=1,
            unit_price=Decimal("5.00"),
            refund_amount=Decimal("5.00"),
        )
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self.return_url(), self.return_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


@skipUnlessDBFeature("has_select_for_update")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SaleReturnConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Return Lock", slug="return-lock")
        self.user = User.objects.create_user(email="return-lock@example.com", password=PASSWORD)
        Membership.objects.create(
            user=self.user,
            organization=self.organization,
            role=Membership.Role.OWNER,
        )
        product = Product.objects.create(
            organization=self.organization,
            name="Once only",
            sku="ONCE-RETURN",
            selling_price=Decimal("10.00"),
            cost_price=Decimal("4.00"),
            stock_quantity=0,
        )
        self.sale = Sale.objects.create(
            organization=self.organization,
            receipt_number="LOCK-RETURN-1",
            cashier=self.user,
            subtotal=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("10.00"),
            status=Sale.Status.COMPLETED,
            payment_status=Sale.PaymentStatus.PAID,
            idempotency_key="lock-return-1",
        )
        self.sale_item = SaleItem.objects.create(
            sale=self.sale,
            product=product,
            product_name=product.name,
            product_sku=product.sku,
            quantity=1,
            unit_price=Decimal("10.00"),
            line_total=Decimal("10.00"),
        )
        Payment.objects.create(
            sale=self.sale,
            organization=self.organization,
            method=Payment.Method.CASH,
            amount=Decimal("10.00"),
            created_by=self.user,
        )
        self.product = product

    def test_concurrent_requests_cannot_return_one_item_twice(self):
        user_id = self.user.pk
        sale_id = self.sale.pk
        sale_item_id = self.sale_item.pk

        def return_once(_):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=User.objects.get(pk=user_id))
                response = client.post(
                    f"/api/v1/sales/{sale_id}/returns/",
                    {
                        "items": [{"sale_item_id": str(sale_item_id), "quantity": 1}],
                        "reason": "Concurrent return",
                        "refund_method": Payment.Method.CASH,
                    },
                    format="json",
                )
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(return_once, range(2)))

        self.assertCountEqual(responses, [status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)
        self.assertEqual(SaleReturn.objects.filter(sale=self.sale).count(), 1)
        self.assertEqual(SaleReturnItem.objects.filter(sale_return__sale=self.sale).count(), 1)
        self.assertEqual(StockMovement.objects.filter(reference_type="sale_return").count(), 1)
