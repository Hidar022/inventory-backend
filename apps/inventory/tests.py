from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Category, Product
from apps.dashboard.models import ActivityEvent
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_movement
from apps.organizations.models import Membership, Organization

User = get_user_model()
PASSWORD = "StrongPass123!"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InventoryAPITests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.org_a = Organization.objects.create(name="Inventory A", slug="inventory-a")
		self.org_b = Organization.objects.create(name="Inventory B", slug="inventory-b")
		self.owner = self.make_member("inventory-owner@example.com", self.org_a, Membership.Role.OWNER)
		self.manager = self.make_member("inventory-manager@example.com", self.org_a, Membership.Role.MANAGER)
		self.cashier = self.make_member("inventory-cashier@example.com", self.org_a, Membership.Role.CASHIER)
		self.other_owner = self.make_member("inventory-other@example.com", self.org_b, Membership.Role.OWNER)
		self.category = Category.objects.create(organization=self.org_a, name="Beverages")
		self.other_category = Category.objects.create(organization=self.org_b, name="Other")
		self.product = self.make_product(
			self.org_a, self.category, "Milk", "MILK-1", quantity=10, threshold=3
		)
		self.low_product = self.make_product(
			self.org_a, self.category, "Soap", "SOAP-1", quantity=2, threshold=2
		)
		self.foreign_product = self.make_product(
			self.org_b, self.other_category, "Foreign", "FOREIGN-1", quantity=7, threshold=1
		)

	def make_member(self, email, organization, role):
		user = User.objects.create_user(email=email, password=PASSWORD)
		Membership.objects.create(user=user, organization=organization, role=role)
		return user

	def make_product(self, organization, category, name, sku, quantity, threshold):
		return Product.objects.create(
			organization=organization,
			category=category,
			name=name,
			sku=sku,
			unit="each",
			selling_price=Decimal("5.00"),
			cost_price=Decimal("3.00"),
			stock_quantity=quantity,
			low_stock_threshold=threshold,
		)

	def authenticate(self, user):
		self.client.force_authenticate(user=user)

	def inventory_url(self):
		return "/api/v1/inventory/"

	def post_stock_in(self, product, quantity=1, reason="Supplier delivery", **extra):
		return self.client.post(
			f"{self.inventory_url()}stock-in/",
			{"product_id": product.pk, "quantity": quantity, "reason": reason, **extra},
			format="json",
		)

	def post_adjustment(self, product, quantity, reason="Stock count", **extra):
		return self.client.post(
			f"{self.inventory_url()}adjust/",
			{"product_id": product.pk, "quantity": quantity, "reason": reason, **extra},
			format="json",
		)

	def test_all_roles_can_read_inventory_and_low_stock_filters(self):
		for user in (self.owner, self.manager, self.cashier):
			self.authenticate(user)
			response = self.client.get(self.inventory_url())
			self.assertEqual(response.status_code, status.HTTP_200_OK)
			self.assertEqual(response.data["count"], 2)
			self.assertEqual(
				self.client.get(f"{self.inventory_url()}?low_stock=true").data["count"], 1
			)
			self.assertEqual(
				self.client.get(f"{self.inventory_url()}?low_stock=false").data["count"], 1
			)
			item = response.data["results"][0]
			self.assertIn("stock_quantity", item)
			self.assertIn("quantity", item)
			self.assertNotIn("cost_price", item)

		self.authenticate(self.owner)
		self.assertEqual(
			self.client.get(f"{self.inventory_url()}?search=milk").data["count"], 1
		)
		self.assertEqual(
			self.client.get(
				f"{self.inventory_url()}?category={self.category.pk}"
			).data["count"],
			2,
		)
		self.low_product.is_active = False
		self.low_product.save(update_fields=["is_active"])
		self.assertEqual(
			self.client.get(f"{self.inventory_url()}?active=false").data["count"], 1
		)
		self.assertEqual(
			self.client.get(f"{self.inventory_url()}?is_active=false").data["count"], 1
		)
		self.assertEqual(
			self.client.get(f"{self.inventory_url()}?low_stock=maybe").status_code,
			status.HTTP_400_BAD_REQUEST,
		)

	def test_owner_and_manager_can_mutate_but_cashier_cannot(self):
		for user in (self.owner, self.manager):
			self.authenticate(user)
			response = self.post_stock_in(self.product, quantity=1)
			self.assertEqual(response.status_code, status.HTTP_201_CREATED)
			response = self.post_adjustment(self.product, quantity=1)
			self.assertEqual(response.status_code, status.HTTP_201_CREATED)

		self.authenticate(self.cashier)
		self.assertEqual(
			self.post_stock_in(self.product).status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(
			self.post_adjustment(self.product, quantity=1).status_code,
			status.HTTP_403_FORBIDDEN,
		)

	def test_stock_in_updates_balance_and_writes_server_controlled_audit(self):
		self.authenticate(self.owner)
		response = self.post_stock_in(
			self.product,
			quantity=5,
			reason="  New delivery  ",
			organization=str(self.org_b.pk),
			created_by=self.other_owner.pk,
			previous_quantity=900,
			new_quantity=901,
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.product.refresh_from_db()
		movement = StockMovement.objects.get(pk=response.data["id"])
		self.assertEqual(self.product.stock_quantity, 15)
		self.assertEqual(movement.quantity, 5)
		self.assertEqual(movement.previous_quantity, 10)
		self.assertEqual(movement.new_quantity, 15)
		self.assertEqual(movement.reason, "New delivery")
		self.assertEqual(movement.created_by, self.owner)
		self.assertEqual(movement.organization, self.org_a)
		self.assertEqual(response.data["movement_type"], StockMovement.MovementType.STOCK_IN)
		event = ActivityEvent.objects.get(
			organization=self.org_a,
			action="inventory.stock_added",
		)
		self.assertEqual(event.actor, self.owner)
		self.assertEqual(event.entity_id, str(movement.pk))
		self.assertEqual(event.metadata["previous_quantity"], 10)

	def test_stock_in_rejects_invalid_quantity_inactive_and_empty_reason(self):
		self.authenticate(self.owner)
		for quantity in (0, -1):
			response = self.post_stock_in(self.product, quantity=quantity)
			self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(
			self.post_stock_in(self.product, reason="   ").status_code,
			status.HTTP_400_BAD_REQUEST,
		)
		self.low_product.is_active = False
		self.low_product.save(update_fields=["is_active"])
		response = self.post_stock_in(self.low_product)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(StockMovement.objects.count(), 0)

	def test_adjustments_use_signed_deltas_and_require_reason(self):
		self.authenticate(self.owner)
		increase = self.post_adjustment(self.product, 5, "  Count increase  ")
		decrease = self.post_adjustment(self.product, -3, "Damaged stock")
		self.assertEqual(increase.status_code, status.HTTP_201_CREATED)
		self.assertEqual(decrease.status_code, status.HTTP_201_CREATED)
		self.assertEqual(increase.data["movement_type"], StockMovement.MovementType.ADJUSTMENT_IN)
		self.assertEqual(increase.data["quantity"], 5)
		self.assertEqual(decrease.data["movement_type"], StockMovement.MovementType.ADJUSTMENT_OUT)
		self.assertEqual(decrease.data["quantity"], -3)
		self.assertEqual(decrease.data["new_quantity"], 12)
		self.assertEqual(decrease.data["reason"], "Damaged stock")
		self.assertEqual(
			ActivityEvent.objects.filter(
				organization=self.org_a,
				action="inventory.adjusted",
			).count(),
			2,
		)
		self.assertEqual(self.post_adjustment(self.product, 0).status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(
			self.client.post(
				f"{self.inventory_url()}adjust/",
				{"product_id": self.product.pk, "quantity": 1},
				format="json",
			).status_code,
			status.HTTP_400_BAD_REQUEST,
		)
		self.assertEqual(
			self.post_adjustment(self.product, 1, "   ").status_code,
			status.HTTP_400_BAD_REQUEST,
		)

	def test_insufficient_adjustment_does_not_change_stock_or_create_movement(self):
		self.authenticate(self.owner)
		response = self.post_adjustment(self.product, -11)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.product.refresh_from_db()
		self.assertEqual(self.product.stock_quantity, 10)
		self.assertEqual(StockMovement.objects.count(), 0)

	def test_inactive_product_cannot_be_mutated(self):
		self.authenticate(self.owner)
		self.product.is_active = False
		self.product.save(update_fields=["is_active"])
		self.assertEqual(self.post_stock_in(self.product).status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(self.post_adjustment(self.product, 1).status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(StockMovement.objects.count(), 0)

	def test_movement_history_filtering_ordering_dates_and_pagination(self):
		self.authenticate(self.manager)
		for index in range(21):
			response = self.post_stock_in(self.product, reason=f"Delivery {index}")
			self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		response = self.client.get(f"{self.inventory_url()}movements/")
		self.assertEqual(response.data["count"], 21)
		self.assertEqual(len(response.data["results"]), 20)
		first_page_ids = [item["id"] for item in response.data["results"]]
		second_page = self.client.get(f"{self.inventory_url()}movements/?page=2")
		self.assertEqual(len(second_page.data["results"]), 1)
		self.assertNotIn(second_page.data["results"][0]["id"], first_page_ids)
		newest = response.data["results"][0]
		self.assertEqual(newest["movement_type"], StockMovement.MovementType.STOCK_IN)
		self.assertEqual(newest["type"], "stock_in")
		self.assertEqual(newest["quantity_delta"], 1)
		self.assertIn("product_name", newest)
		self.assertIn("created_by", newest)
		self.assertEqual(
			self.client.get(f"{self.inventory_url()}movements/?product={self.product.pk}").data["count"],
			21,
		)
		self.assertEqual(
			self.client.get(
				f"{self.inventory_url()}movements/?movement_type=stock_in"
			).data["count"],
			21,
		)
		self.assertEqual(
			self.client.get(
				f"{self.inventory_url()}movements/?created_by={self.manager.pk}"
			).data["count"],
			21,
		)
		today = timezone.localdate().isoformat()
		self.assertEqual(
			self.client.get(
				f"{self.inventory_url()}movements/?from={today}&to={today}"
			).data["count"],
			21,
		)
		self.assertEqual(
			self.client.get(
				f"{self.inventory_url()}movements/?from=not-a-date"
			).status_code,
			status.HTTP_400_BAD_REQUEST,
		)

	def test_tenant_isolation_and_foreign_product_mutation_resistance(self):
		create_stock_movement(
			organization=self.org_b,
			product_id=self.foreign_product.pk,
			actor=self.other_owner,
			quantity=2,
			reason="Other tenant",
			movement_type=StockMovement.MovementType.STOCK_IN,
		)
		self.authenticate(self.owner)
		inventory = self.client.get(self.inventory_url())
		self.assertEqual(inventory.data["count"], 2)
		self.assertNotIn(self.foreign_product.pk, [item["product_id"] for item in inventory.data["results"]])
		movements = self.client.get(f"{self.inventory_url()}movements/")
		self.assertEqual(movements.data["count"], 0)
		foreign = self.post_stock_in(self.foreign_product)
		missing = self.client.post(
			f"{self.inventory_url()}stock-in/",
			{"product_id": 999999, "quantity": 1, "reason": "Missing"},
			format="json",
		)
		self.assertEqual(foreign.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(foreign.data, missing.data)
		self.assertEqual(
			self.client.get(f"{self.inventory_url()}movements/?product={self.foreign_product.pk}").data["count"],
			0,
		)

	def test_movement_api_is_read_only(self):
		self.authenticate(self.owner)
		movement = self.post_stock_in(self.product).data
		url = f"{self.inventory_url()}movements/"
		self.assertEqual(self.client.post(url, {}).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
		self.assertEqual(self.client.patch(url, {}).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
		self.assertEqual(self.client.delete(url).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
		self.assertEqual(
			self.client.get(f"{url}{movement['id']}/").status_code,
			status.HTTP_404_NOT_FOUND,
		)

	def test_movement_failure_rolls_back_stock_and_update_failure_creates_no_movement(self):
		self.authenticate(self.owner)
		with patch(
			"apps.inventory.services.StockMovement.objects.create",
			side_effect=RuntimeError("movement insert failed"),
		):
			with self.assertRaises(RuntimeError):
				self.post_stock_in(self.product, quantity=4)
		self.product.refresh_from_db()
		self.assertEqual(self.product.stock_quantity, 10)
		self.assertEqual(StockMovement.objects.count(), 0)

		with patch.object(Product, "save", side_effect=RuntimeError("stock update failed")):
			with self.assertRaises(RuntimeError):
				self.post_stock_in(self.product, quantity=4)
		self.assertEqual(StockMovement.objects.count(), 0)


@skipUnlessDBFeature("has_select_for_update")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InventoryConcurrencyTests(TransactionTestCase):
	reset_sequences = True

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

	def setUp(self):
		self.organization = Organization.objects.create(name="Locking", slug="locking")
		self.user = User.objects.create_user(email="locking@example.com", password=PASSWORD)
		Membership.objects.create(
			user=self.user,
			organization=self.organization,
			role=Membership.Role.OWNER,
		)
		self.product = Product.objects.create(
			organization=self.organization,
			name="Locked stock",
			sku="LOCK-1",
			selling_price=Decimal("1.00"),
			cost_price=Decimal("0.50"),
			stock_quantity=0,
		)

	def test_concurrent_stock_ins_do_not_lose_updates(self):
		product_id = self.product.pk
		organization_id = self.organization.pk
		user_id = self.user.pk

		def stock_in_once(_):
			close_old_connections()
			user = User.objects.get(pk=user_id)
			organization = Organization.objects.get(pk=organization_id)
			create_stock_movement(
				organization=organization,
				product_id=product_id,
				actor=user,
				quantity=1,
				reason="Concurrent delivery",
				movement_type=StockMovement.MovementType.STOCK_IN,
			)
			close_old_connections()

		with ThreadPoolExecutor(max_workers=2) as executor:
			list(executor.map(stock_in_once, range(2)))
		self.product.refresh_from_db()
		self.assertEqual(self.product.stock_quantity, 2)
		self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 2)
