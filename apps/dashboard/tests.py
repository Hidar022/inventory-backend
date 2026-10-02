from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Product
from apps.organizations.models import Membership, Organization
from apps.sales.models import Payment, Sale, SaleItem

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DashboardAPITests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.organization = Organization.objects.create(name="Dashboard A", slug="dashboard-a")
		self.other_organization = Organization.objects.create(name="Dashboard B", slug="dashboard-b")
		self.owner = self.make_member("dashboard-owner@example.com", self.organization, Membership.Role.OWNER)
		self.manager = self.make_member("dashboard-manager@example.com", self.organization, Membership.Role.MANAGER)
		self.cashier_a = self.make_member("dashboard-a@example.com", self.organization, Membership.Role.CASHIER)
		self.cashier_b = self.make_member("dashboard-b@example.com", self.organization, Membership.Role.CASHIER)
		self.foreign_owner = self.make_member(
			"dashboard-foreign@example.com",
			self.other_organization,
			Membership.Role.OWNER,
		)
		self.product = self.make_product(self.organization, "Active", "ACTIVE-1", 8, 3)
		self.low_product = self.make_product(self.organization, "Low", "LOW-1", 2, 3)
		self.out_product = self.make_product(self.organization, "Out", "OUT-1", 0, 2)
		self.inactive_product = self.make_product(self.organization, "Inactive", "INACTIVE-1", 0, 0, False)
		self.foreign_product = self.make_product(self.other_organization, "Foreign", "FOREIGN-1", 0, 0)

	def make_member(self, email, organization, role):
		user = User.objects.create_user(email=email, password="StrongPass123!")
		Membership.objects.create(user=user, organization=organization, role=role)
		return user

	def make_product(self, organization, name, sku, stock, threshold, active=True):
		return Product.objects.create(
			organization=organization,
			name=name,
			sku=sku,
			selling_price=Decimal("20.00"),
			cost_price=Decimal("8.00"),
			stock_quantity=stock,
			low_stock_threshold=threshold,
			is_active=active,
		)

	def create_sale(
		self,
		cashier,
		receipt,
		amount,
		*,
		organization=None,
		product=None,
		status_value=Sale.Status.COMPLETED,
		payment_status=Sale.PaymentStatus.PAID,
		created_at=None,
	):
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
				method=Payment.Method.CASH,
				amount=Decimal(amount),
				created_by=cashier,
			)
		if created_at:
			Sale.objects.filter(pk=sale.pk).update(created_at=created_at)
		return sale

	def test_owner_and_manager_get_tenant_scoped_dashboard_metrics(self):
		first = self.create_sale(self.cashier_a, "DASH-A-1", "25.00")
		second = self.create_sale(self.cashier_b, "DASH-B-1", "100.00")
		first.status = Sale.Status.PARTIALLY_RETURNED
		first.save(update_fields=["status"])
		second.status = Sale.Status.FULLY_RETURNED
		second.save(update_fields=["status"])
		self.create_sale(
			self.foreign_owner,
			"DASH-FOREIGN",
			"900.00",
			organization=self.other_organization,
			product=self.foreign_product,
		)
		self.create_sale(
			self.cashier_a,
			"DASH-UNPAID",
			"500.00",
			payment_status=Sale.PaymentStatus.UNPAID,
		)
		self.create_sale(
			self.cashier_a,
			"DASH-REFUNDED",
			"400.00",
			status_value=Sale.Status.FULLY_RETURNED,
			payment_status=Sale.PaymentStatus.REFUNDED,
		)

		for user in (self.owner, self.manager):
			self.client.force_authenticate(user=user)
			response = self.client.get(f"/api/v1/dashboard/?organization_id={self.other_organization.pk}")
			self.assertEqual(response.status_code, status.HTTP_200_OK)
			self.assertEqual(response.data["sales_today"], "125.00")
			self.assertEqual(response.data["sales_count_today"], 2)
			self.assertEqual(response.data["period_sales_total"], "125.00")
			self.assertEqual(response.data["period_sales_count"], 2)
			self.assertEqual(response.data["active_products"], 3)
			self.assertEqual(response.data["low_stock_products"], 2)
			self.assertEqual(response.data["out_of_stock_products"], 1)
			self.assertCountEqual(
				[sale["id"] for sale in response.data["recent_sales"]],
				[str(first.pk), str(second.pk)],
			)

	def test_cashier_dashboard_uses_only_the_cashiers_own_sales(self):
		first = self.create_sale(self.cashier_a, "CASHIER-A-1", "25.00")
		second = self.create_sale(self.cashier_a, "CASHIER-A-2", "10.00")
		self.create_sale(self.cashier_b, "CASHIER-B-1", "100.00")
		self.client.force_authenticate(user=self.cashier_a)

		response = self.client.get("/api/v1/dashboard/")

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["sales_today"], "35.00")
		self.assertEqual(response.data["sales_count_today"], 2)
		self.assertEqual({sale["id"] for sale in response.data["recent_sales"]}, {str(first.pk), str(second.pk)})
		self.assertNotIn("profit", response.data)
		self.assertNotIn("cost_price", response.data)

	def test_recent_sales_are_bounded_and_empty_dashboard_returns_zeroes(self):
		for index in range(7):
			self.create_sale(self.cashier_a, f"RECENT-{index}", "1.00")
		self.client.force_authenticate(user=self.owner)
		populated = self.client.get("/api/v1/dashboard/")
		self.assertEqual(len(populated.data["recent_sales"]), 5)

		empty_org = Organization.objects.create(name="Empty", slug="dashboard-empty")
		empty_owner = self.make_member("dashboard-empty@example.com", empty_org, Membership.Role.OWNER)
		self.client.force_authenticate(user=empty_owner)
		empty = self.client.get("/api/v1/dashboard/")
		self.assertEqual(empty.status_code, status.HTTP_200_OK)
		self.assertEqual(empty.data["sales_today"], "0.00")
		self.assertEqual(empty.data["sales_count_today"], 0)
		self.assertEqual(empty.data["active_products"], 0)
		self.assertEqual(empty.data["low_stock_products"], 0)
		self.assertEqual(empty.data["out_of_stock_products"], 0)
		self.assertEqual(empty.data["recent_sales"], [])
