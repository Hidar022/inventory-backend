from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Category, Product
from apps.inventory.models import StockMovement
from apps.organizations.models import Membership, Organization
from apps.purchases.models import Purchase, PurchaseItem, Supplier

User = get_user_model()
PASSWORD = "StrongPass123!"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PurchaseAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org_a = Organization.objects.create(name="Org A", slug="org-a")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b")
        self.owner = self.make_member("owner@example.com", self.org_a, Membership.Role.OWNER)
        self.manager = self.make_member("manager@example.com", self.org_a, Membership.Role.MANAGER)
        self.cashier = self.make_member("cashier@example.com", self.org_a, Membership.Role.CASHIER)
        self.other_owner = self.make_member("other-owner@example.com", self.org_b, Membership.Role.OWNER)
        self.category = Category.objects.create(organization=self.org_a, name="Office")
        self.product = Product.objects.create(
            organization=self.org_a,
            category=self.category,
            name="Notebook",
            sku="NOTE-001",
            unit="box",
            selling_price=Decimal("12.50"),
            cost_price=Decimal("8.00"),
            stock_quantity=5,
            low_stock_threshold=2,
            is_active=True,
        )
        self.foreign_product = Product.objects.create(
            organization=self.org_b,
            category=Category.objects.create(organization=self.org_b, name="Other"),
            name="Foreign Item",
            sku="FOREIGN-1",
            unit="box",
            selling_price=Decimal("20.00"),
            cost_price=Decimal("10.00"),
            stock_quantity=2,
            low_stock_threshold=1,
            is_active=True,
        )
        self.supplier = Supplier.objects.create(
            organization=self.org_a,
            name="North Supplies",
            phone="123",
            email="supplier@example.com",
            address="Main street",
            notes="Preferred",
        )
        self.foreign_supplier = Supplier.objects.create(
            organization=self.org_b,
            name="Foreign Supplier",
        )

    def make_member(self, email, organization, role):
        user = User.objects.create_user(email=email, password=PASSWORD)
        Membership.objects.create(user=user, organization=organization, role=role)
        return user

    def authenticate(self, user):
        self.client.force_authenticate(user=user)

    def test_supplier_create_list_retrieve_update_and_deactivate(self):
        self.authenticate(self.owner)
        response = self.client.post(
            "/api/v1/suppliers/",
            {
                "name": "South Supplies",
                "phone": "555",
                "email": "south@example.com",
                "address": "South Road",
                "notes": "New supplier",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        supplier_id = response.data["id"]
        self.assertEqual(self.client.get("/api/v1/suppliers/").data["count"], 2)
        self.assertEqual(
            self.client.get(f"/api/v1/suppliers/{supplier_id}/").status_code,
            status.HTTP_200_OK,
        )
        patch_response = self.client.patch(
            f"/api/v1/suppliers/{supplier_id}/",
            {"phone": "888"},
            format="json",
        )
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_response.data["phone"], "888")
        deactivate_response = self.client.post(f"/api/v1/suppliers/{supplier_id}/deactivate/")
        self.assertEqual(deactivate_response.status_code, status.HTTP_200_OK)
        self.assertFalse(deactivate_response.data["is_active"])

        self.authenticate(self.cashier)
        list_response = self.client.get("/api/v1/suppliers/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 2)
        self.assertEqual(
            self.client.post(
                "/api/v1/suppliers/",
                {"name": "Blocked Supplier"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_purchase_creation_and_tenant_isolation(self):
        self.authenticate(self.owner)
        response = self.client.post(
            "/api/v1/purchases/",
            {
                "supplier": str(self.supplier.pk),
                "reference_number": "PO-1001",
                "notes": "Office supply",
                "discount": "0.00",
                "items": [
                    {"product": self.product.pk, "quantity": 2, "unit_cost": "4.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        purchase = Purchase.objects.get(pk=response.data["id"])
        self.assertEqual(purchase.status, Purchase.Status.DRAFT)
        self.assertEqual(purchase.subtotal, Decimal("8.00"))
        self.assertEqual(purchase.total, Decimal("8.00"))
        self.assertEqual(purchase.items.get().product_name, "Notebook")
        self.assertEqual(purchase.items.get().line_total, Decimal("8.00"))

        self.assertEqual(
            self.client.get(f"/api/v1/purchases/{purchase.pk}/").status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/purchases/{self.make_purchase_for_org_b().pk}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def make_purchase_for_org_b(self):
        return Purchase.objects.create(
            organization=self.org_b,
            supplier=self.foreign_supplier,
            reference_number="PO-B-1",
            subtotal=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("10.00"),
            created_by=self.other_owner,
            status=Purchase.Status.DRAFT,
        )

    def test_purchase_validation_rejects_foreign_product_and_inactive_product(self):
        self.authenticate(self.owner)
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])
        response = self.client.post(
            "/api/v1/purchases/",
            {
                "supplier": str(self.supplier.pk),
                "items": [{"product": self.product.pk, "quantity": 1, "unit_cost": "3.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            "/api/v1/purchases/",
            {
                "supplier": str(self.supplier.pk),
                "items": [{"product": self.foreign_product.pk, "quantity": 1, "unit_cost": "3.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_receive_purchase_increases_inventory_and_creates_stock_movement(self):
        self.authenticate(self.owner)
        purchase = Purchase.objects.create(
            organization=self.org_a,
            supplier=self.supplier,
            reference_number="PO-2001",
            subtotal=Decimal("12.00"),
            discount=Decimal("0.00"),
            total=Decimal("12.00"),
            created_by=self.owner,
            status=Purchase.Status.DRAFT,
        )
        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            product_name=self.product.name,
            product_sku=self.product.sku,
            quantity=3,
            unit_cost=Decimal("4.00"),
            line_total=Decimal("12.00"),
        )

        response = self.client.post(f"/api/v1/purchases/{purchase.pk}/receive/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        purchase.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertEqual(purchase.status, Purchase.Status.RECEIVED)
        self.assertIsNotNone(purchase.received_at)
        movement = StockMovement.objects.filter(
            organization=self.org_a,
            product=self.product,
            reference_type="purchase",
            reference_id=str(purchase.pk),
        ).latest("created_at")
        self.assertEqual(movement.quantity, 3)
        self.assertEqual(movement.previous_quantity, 5)
        self.assertEqual(movement.new_quantity, 8)
        self.assertEqual(movement.movement_type, StockMovement.MovementType.STOCK_IN)

    def test_purchase_receive_is_idempotent_and_cannot_be_received_twice(self):
        self.authenticate(self.owner)
        purchase = Purchase.objects.create(
            organization=self.org_a,
            supplier=self.supplier,
            reference_number="PO-2002",
            subtotal=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("10.00"),
            created_by=self.owner,
            status=Purchase.Status.DRAFT,
        )
        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            product_name=self.product.name,
            product_sku=self.product.sku,
            quantity=2,
            unit_cost=Decimal("5.00"),
            line_total=Decimal("10.00"),
        )

        self.assertEqual(self.client.post(f"/api/v1/purchases/{purchase.pk}/receive/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post(f"/api/v1/purchases/{purchase.pk}/receive/").status_code, status.HTTP_400_BAD_REQUEST)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 7)
        self.assertEqual(StockMovement.objects.filter(reference_id=str(purchase.pk)).count(), 1)

    def test_cancellation_requires_owner_or_manager_and_preserves_stock(self):
        self.authenticate(self.owner)
        purchase = Purchase.objects.create(
            organization=self.org_a,
            supplier=self.supplier,
            reference_number="PO-3001",
            subtotal=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("10.00"),
            created_by=self.owner,
            status=Purchase.Status.DRAFT,
        )
        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            product_name=self.product.name,
            product_sku=self.product.sku,
            quantity=1,
            unit_cost=Decimal("10.00"),
            line_total=Decimal("10.00"),
        )

        response = self.client.post(f"/api/v1/purchases/{purchase.pk}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, Purchase.Status.CANCELLED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 5)
        self.assertEqual(StockMovement.objects.filter(reference_id=str(purchase.pk)).count(), 0)

        self.authenticate(self.cashier)
        self.assertEqual(
            self.client.post(f"/api/v1/purchases/{purchase.pk}/cancel/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_cashier_has_read_only_access(self):
        self.authenticate(self.cashier)
        self.assertEqual(self.client.get("/api/v1/purchases/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post("/api/v1/purchases/", {"supplier": str(self.supplier.pk), "items": []}, format="json").status_code, status.HTTP_403_FORBIDDEN)
