from decimal import Decimal
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Product
from apps.dashboard.models import ActivityEvent
from apps.expenses.models import Expense, ExpenseCategory
from apps.organizations.models import Membership, Organization
from apps.purchases.models import Purchase, Supplier
from apps.sales.models import Payment, Sale, SaleItem
from apps.dashboard.services import log_activity_event
from apps.sales.services import checkout_sale

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


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DashboardReportsAndActivityAPITests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.organization = Organization.objects.create(name="Living Reports", slug="living-reports")
		self.other_organization = Organization.objects.create(name="Other Org", slug="other-org")
		self.owner = self.make_member("reports-owner@example.com", self.organization, Membership.Role.OWNER)
		self.manager = self.make_member("reports-manager@example.com", self.organization, Membership.Role.MANAGER)
		self.cashier = self.make_member("reports-cashier@example.com", self.organization, Membership.Role.CASHIER)
		self.foreign_owner = self.make_member("reports-other-owner@example.com", self.other_organization, Membership.Role.OWNER)
		self.product = Product.objects.create(
			organization=self.organization,
			name="Milk",
			sku="MILK-1",
			unit="each",
			selling_price=Decimal("12.50"),
			cost_price=Decimal("7.00"),
			stock_quantity=2,
			low_stock_threshold=3,
		)
		self.category = ExpenseCategory.objects.create(
			organization=self.organization,
			name="Office",
		)
		supplier = Supplier.objects.create(organization=self.organization, name="Fresh Farm")
		self.purchase = Purchase.objects.create(
			organization=self.organization,
			supplier=supplier,
			reference_number="PO-100",
			status=Purchase.Status.RECEIVED,
			subtotal=Decimal("80.00"),
			discount=Decimal("0.00"),
			total=Decimal("80.00"),
			created_by=self.owner,
			received_at=timezone.now(),
		)
		Expense.objects.create(
			organization=self.organization,
			category=self.category,
			amount=Decimal("25.00"),
			payment_method=Payment.Method.CASH,
			description="Utility bill",
			created_by=self.owner,
		)
		sale = Sale.objects.create(
			organization=self.organization,
			receipt_number="REP-001",
			cashier=self.cashier,
			subtotal=Decimal("50.00"),
			discount=Decimal("0.00"),
			total=Decimal("50.00"),
			status=Sale.Status.COMPLETED,
			payment_status=Sale.PaymentStatus.PAID,
			idempotency_key="rep-001",
		)
		SaleItem.objects.create(
			sale=sale,
			product=self.product,
			product_name=self.product.name,
			product_sku=self.product.sku,
			quantity=2,
			unit_price=Decimal("25.00"),
			line_total=Decimal("50.00"),
		)
		Payment.objects.create(
			sale=sale,
			organization=self.organization,
			method=Payment.Method.CASH,
			amount=Decimal("50.00"),
			created_by=self.cashier,
		)
		ActivityEvent.objects.create(
			organization=self.organization,
			actor=self.owner,
			action="sale.created",
			entity_type="Sale",
			entity_id=str(sale.pk),
			description="Completed sale REP-001",
		)
		ActivityEvent.objects.create(
			organization=self.other_organization,
			actor=self.foreign_owner,
			action="product.created",
			entity_type="Product",
			entity_id="other-product",
			description="Created product outside scope",
		)

	def make_member(self, email, organization, role):
		user = User.objects.create_user(email=email, password="StrongPass123!")
		Membership.objects.create(user=user, organization=organization, role=role)
		return user

	def test_reports_endpoint_returns_authoritative_monthly_summary(self):
		self.client.force_authenticate(user=self.owner)
		response = self.client.get("/api/v1/dashboard/reports/")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["sales_total"], "50.00")
		self.assertEqual(response.data["sales_count"], 1)
		self.assertEqual(response.data["purchase_total"], "80.00")
		self.assertEqual(response.data["expense_total"], "25.00")
		self.assertEqual(response.data["cash_summary"]["cash_sales"], "50.00")
		self.assertEqual(response.data["cash_summary"]["cash_out"], "25.00")
		self.assertEqual(response.data["stock_summary"]["low_stock"], 1)
		self.assertEqual(response.data["stock_summary"]["out_of_stock"], 0)
		self.assertEqual(response.data["top_products"][0]["name"], "Milk")
		self.assertEqual(response.data["returns_summary"]["count"], 0)
		self.assertEqual(response.data["purchasing_summary"]["received_count"], 1)
		self.assertEqual(response.data["expenses_summary"]["count"], 1)
		self.assertEqual(response.data["inventory_summary"]["units_on_hand"], 2)

		self.client.force_authenticate(user=self.manager)
		manager_response = self.client.get("/api/v1/dashboard/reports/")
		self.assertEqual(manager_response.status_code, status.HTTP_200_OK)

	def test_reports_endpoint_filters_by_inclusive_lagos_business_dates(self):
		self.client.force_authenticate(user=self.owner)
		Purchase.objects.filter(pk=self.purchase.pk).update(
			created_at=timezone.now() - timedelta(days=2),
		)
		day = timezone.localdate().isoformat()
		response = self.client.get(f"/api/v1/dashboard/reports/?from={day}&to={day}")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["date_range"], {"from": day, "to": day})
		self.assertEqual(response.data["sales_count"], 1)
		self.assertEqual(response.data["purchase_total"], "80.00")

		invalid = self.client.get(f"/api/v1/dashboard/reports/?from={day}&to=not-a-date")
		self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

	def test_reports_endpoint_restricts_cashiers(self):
		self.client.force_authenticate(user=self.cashier)
		response = self.client.get("/api/v1/dashboard/reports/")
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_activity_endpoint_is_tenant_scoped_and_serialized(self):
		self.client.force_authenticate(user=self.owner)
		response = self.client.get(
			"/api/v1/dashboard/activity/",
			{"organization_id": str(self.other_organization.pk)},
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["count"], 1)
		self.assertEqual(response.data["results"][0]["actor_name"], "reports-owner@example.com")
		self.assertEqual(response.data["results"][0]["entity_type"], "Sale")
		self.assertEqual(response.data["results"][0]["organization"], str(self.organization.pk))

	def test_activity_endpoint_restricts_non_owners(self):
		for user in (self.manager, self.cashier):
			self.client.force_authenticate(user=user)
			response = self.client.get("/api/v1/dashboard/activity/")
			self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_activity_metadata_excludes_sensitive_keys(self):
		event = log_activity_event(
			organization=self.organization,
			actor=self.owner,
			action="security.metadata_test",
			entity_type="Test",
			entity_id="test-only",
			description="Metadata sanitizer test",
			metadata={
				"safe_value": "kept",
				"invitation_token": "never-store",
				"password": "never-store",
				"api_key": "never-store",
			},
		)
		self.assertEqual(event.metadata, {"safe_value": "kept"})

	def test_sale_checkout_creates_activity_event_for_activity_feed(self):
		request = SimpleNamespace(organization=self.organization, user=self.owner, role=Membership.Role.OWNER)
		validated_data = {
			"idempotency_key": "audit-sale-activity",
			"items": [{"product_id": self.product, "quantity": 1}],
			"discount": Decimal("0.00"),
			"payments": [{"method": Payment.Method.CASH, "amount": Decimal("12.50")}],
		}

		sale, created = checkout_sale(request, validated_data)
		self.assertTrue(created)
		self.assertTrue(
			ActivityEvent.objects.filter(
				organization=self.organization,
				action="sale.created",
				entity_type="Sale",
				entity_id=str(sale.pk),
			).exists()
		)

		self.client.force_authenticate(user=self.owner)
		response = self.client.get("/api/v1/dashboard/activity/")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertGreaterEqual(response.data["count"], 1)
