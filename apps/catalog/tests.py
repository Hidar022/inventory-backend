from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.test.utils import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Category, Product
from apps.catalog.serializers import CategorySerializer, ProductSerializer
from apps.organizations.models import Membership, Organization

User = get_user_model()
PASSWORD = "StrongPass123!"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CatalogAPITests(TestCase):
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
			selling_price=Decimal("3.25"),
			cost_price=Decimal("2.10"),
		)
		self.other_product = Product.objects.create(
			organization=self.org_b,
			category=self.other_category,
			name="Other Milk",
			sku="MILK-1",
			selling_price=Decimal("4.00"),
			cost_price=Decimal("2.00"),
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

	def category_url(self, category=None):
		return "/api/v1/categories/" + (f"{category.pk}/" if category else "")

	def product_url(self, product=None):
		return "/api/v1/products/" + (f"{product.pk}/" if product else "")

	def product_payload(self, **overrides):
		payload = {
			"name": "  Green   Tea ",
			"sku": " tea-001 ",
			"category_id": self.category.pk,
			"unit": "box",
			"selling_price": "12.30",
			"cost_price": "7.15",
		}
		payload.update(overrides)
		return payload

	def test_category_create_edit_lifecycle_and_role_permissions(self):
		for user in (self.owner, self.manager):
			self.authenticate(user)
			category_name = f" {user.email.split('@')[0]} pantry "
			response = self.client.post(self.category_url(), {"name": category_name}, format="json")
			self.assertEqual(response.status_code, status.HTTP_201_CREATED)
			category_id = response.data["id"]
			self.assertEqual(response.data["name"], f"{user.email.split('@')[0]} pantry")
			self.assertTrue(
				self.client.patch(
					f"/api/v1/categories/{category_id}/",
					{"is_active": False},
					format="json",
				).data["is_active"]
			)
			self.assertEqual(
				self.client.patch(
					f"/api/v1/categories/{category_id}/",
					{"name": f"{user.email.split('@')[0]} dry goods"},
					format="json",
				).status_code,
				status.HTTP_200_OK,
			)
			self.assertEqual(
				self.client.post(f"/api/v1/categories/{category_id}/deactivate/").status_code,
				status.HTTP_200_OK,
			)
			self.assertEqual(
				self.client.post(f"/api/v1/categories/{category_id}/reactivate/").status_code,
				status.HTTP_200_OK,
			)

		self.authenticate(self.cashier)
		self.assertEqual(
			self.client.post(self.category_url(), {"name": "Forbidden"}, format="json").status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(
			self.client.patch(self.category_url(self.category), {"name": "Changed"}, format="json").status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(
			self.client.post(f"{self.category_url(self.category)}deactivate/").status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(
			self.client.post(f"{self.category_url(self.category)}reactivate/").status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(self.client.get(self.category_url()).status_code, status.HTTP_200_OK)

	def test_category_names_are_normalized_and_unique_per_organization(self):
		self.authenticate(self.owner)
		duplicate = self.client.post(self.category_url(), {"name": "  beverages "}, format="json")
		self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
		other_org_duplicate = Category.objects.create(organization=self.org_b, name="beverages")
		self.assertEqual(other_org_duplicate.name, "beverages")

		self.category.is_active = False
		self.category.save()
		duplicate_inactive = self.client.post(
			self.category_url(), {"name": "BEVERAGES"}, format="json"
		)
		self.assertEqual(duplicate_inactive.status_code, status.HTTP_400_BAD_REQUEST)

	def test_product_create_edit_lifecycle_and_role_permissions(self):
		for user in (self.owner, self.manager):
			self.authenticate(user)
			sku = f"TEA-{user.email.split('@')[0].upper()}"
			response = self.client.post(
				self.product_url(), self.product_payload(sku=sku), format="json"
			)
			self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
			product_id = response.data["id"]
			self.assertEqual(response.data["name"], "Green Tea")
			self.assertEqual(response.data["sku"], sku)
			self.assertEqual(response.data["category"], "Beverages")
			self.assertEqual(response.data["selling_price"], "12.30")
			self.assertTrue(
				self.client.patch(
					f"/api/v1/products/{product_id}/",
					{"is_active": False},
					format="json",
				).data["is_active"]
			)
			self.assertEqual(
				self.client.patch(
					f"/api/v1/products/{product_id}/", {"name": "Tea"}, format="json"
				).status_code,
				status.HTTP_200_OK,
			)
			self.assertEqual(
				self.client.post(f"/api/v1/products/{product_id}/deactivate/").status_code,
				status.HTTP_200_OK,
			)
			self.assertEqual(
				self.client.post(f"/api/v1/products/{product_id}/reactivate/").status_code,
				status.HTTP_200_OK,
			)

		self.authenticate(self.cashier)
		self.assertEqual(
			self.client.post(self.product_url(), self.product_payload(), format="json").status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(
			self.client.patch(self.product_url(self.product), {"name": "Changed"}, format="json").status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(
			self.client.post(f"{self.product_url(self.product)}deactivate/").status_code,
			status.HTTP_403_FORBIDDEN,
		)
		self.assertEqual(
			self.client.post(f"{self.product_url(self.product)}reactivate/").status_code,
			status.HTTP_403_FORBIDDEN,
		)

	def test_sku_normalization_uniqueness_and_updates(self):
		self.authenticate(self.owner)
		duplicate = self.client.post(
			self.product_url(), self.product_payload(sku=" milk-1 "), format="json"
		)
		self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
		same_sku_other_org = self.client.post(
			self.product_url(),
			self.product_payload(sku="milk-1", category_id=self.other_category.pk),
			format="json",
		)
		self.assertEqual(same_sku_other_org.status_code, status.HTTP_400_BAD_REQUEST)

		self.assertEqual(
			self.client.patch(self.product_url(self.product), {"name": "Updated"}, format="json").status_code,
			status.HTTP_200_OK,
		)
		self.product.refresh_from_db()
		self.assertEqual(self.product.sku, "MILK-1")
		created = self.client.post(
			self.product_url(), self.product_payload(sku=" sku-a "), format="json"
		)
		self.assertEqual(created.status_code, status.HTTP_201_CREATED)
		self.assertEqual(created.data["sku"], "SKU-A")

		org_c = Organization.objects.create(name="Org C", slug="org-c")
		user_c = self.make_member("third@example.com", org_c, Membership.Role.OWNER)
		category_c = Category.objects.create(organization=org_c, name="Third Category")
		other_client = APIClient()
		other_client.force_authenticate(user=user_c)
		response = other_client.post(
			self.product_url(),
			self.product_payload(sku="milk-1", category_id=category_c.pk),
			format="json",
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

	def test_database_uniqueness_conflicts_return_validation_errors(self):
		self.authenticate(self.owner)
		conflicting_category = Category.objects.create(
			organization=self.org_a,
			name="Pantry",
		)
		with patch.object(CategorySerializer, "validate_name", return_value="Beverages"):
			category_create = self.client.post(
				self.category_url(), {"name": "Beverages"}, format="json"
			)
		self.assertEqual(category_create.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn("name", category_create.data)

		with patch.object(CategorySerializer, "validate_name", return_value="Beverages"):
			category_update = self.client.patch(
				self.category_url(conflicting_category),
				{"name": "Beverages"},
				format="json",
			)
		self.assertEqual(category_update.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn("name", category_update.data)

		conflicting_product = Product.objects.create(
			organization=self.org_a,
			name="Tea",
			sku="TEA-1",
			selling_price=Decimal("1.00"),
			cost_price=Decimal("0.50"),
		)
		with patch.object(ProductSerializer, "validate_sku", return_value="MILK-1"):
			product_create = self.client.post(
				self.product_url(), self.product_payload(sku="MILK-1"), format="json"
			)
		self.assertEqual(product_create.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn("sku", product_create.data)

		with patch.object(ProductSerializer, "validate_sku", return_value="MILK-1"):
			product_update = self.client.patch(
				self.product_url(conflicting_product),
				{"sku": "MILK-1"},
				format="json",
			)
		self.assertEqual(product_update.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn("sku", product_update.data)

		def raise_unrelated_integrity_error(*args, **kwargs):
			raise IntegrityError("unrelated database integrity failure")

		with patch.object(ProductSerializer, "save", raise_unrelated_integrity_error):
			with self.assertRaises(IntegrityError):
				self.client.post(
					self.product_url(), self.product_payload(sku="UNRELATED"), format="json"
				)

	def test_category_ownership_and_inactive_category_rules(self):
		self.authenticate(self.owner)
		cross_tenant = self.client.post(
			self.product_url(), self.product_payload(category_id=self.other_category.pk), format="json"
		)
		nonexistent_category = self.client.post(
			self.product_url(), self.product_payload(category_id=999999), format="json"
		)
		self.assertEqual(cross_tenant.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(cross_tenant.data, nonexistent_category.data)

		valid_category_product = self.client.post(
			self.product_url(), self.product_payload(sku="VALID-CATEGORY"), format="json"
		)
		self.assertEqual(valid_category_product.status_code, status.HTTP_201_CREATED)

		self.category.is_active = False
		self.category.save()
		new_product = self.client.post(self.product_url(), self.product_payload(), format="json")
		self.assertEqual(new_product.status_code, status.HTTP_400_BAD_REQUEST)

		self.assertEqual(
			self.client.patch(
				self.product_url(self.product), {"category_id": None}, format="json"
			).status_code,
			status.HTTP_200_OK,
		)
		reassignment = self.client.patch(
			self.product_url(self.product), {"category_id": self.category.pk}, format="json"
		)
		self.assertEqual(reassignment.status_code, status.HTTP_400_BAD_REQUEST)

	def test_prices_and_stock_are_validated_and_stock_is_read_only(self):
		self.authenticate(self.owner)
		for field in ("selling_price", "cost_price"):
			response = self.client.post(
				self.product_url(), self.product_payload(**{field: "-0.01"}), format="json"
			)
			self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

		valid = self.client.post(
			self.product_url(),
			self.product_payload(
				selling_price="0.10",
				cost_price="0.03",
				stock_quantity=99,
				organization_id=str(self.org_b.pk),
			),
			format="json",
		)
		self.assertEqual(valid.status_code, status.HTTP_201_CREATED)
		self.assertEqual(valid.data["selling_price"], "0.10")
		self.assertEqual(valid.data["cost_price"], "0.03")
		self.assertEqual(valid.data["stock_quantity"], 0)
		self.assertEqual(Product.objects.get(pk=valid.data["id"]).organization_id, self.org_a.pk)
		product_id = valid.data["id"]
		updated = self.client.patch(
			f"/api/v1/products/{product_id}/", {"stock_quantity": 999}, format="json"
		)
		self.assertEqual(updated.status_code, status.HTTP_200_OK)
		self.assertEqual(updated.data["stock_quantity"], 0)
		self.assertFalse(hasattr(Product, "movements"))

		self.authenticate(self.cashier)
		self.assertEqual(
			self.client.patch(
				self.product_url(self.product), {"stock_quantity": 20}, format="json"
			).status_code,
			status.HTTP_403_FORBIDDEN,
		)
		cashier_product = self.client.get(self.product_url(self.product))
		self.assertEqual(cashier_product.status_code, status.HTTP_200_OK)
		self.assertNotIn("cost_price", cashier_product.data)
		self.assertIn("stock_quantity", cashier_product.data)

	def test_tenant_isolation_filters_search_and_no_hard_deletes(self):
		self.authenticate(self.owner)
		products = self.client.get(self.product_url())
		categories = self.client.get(self.category_url())
		self.assertEqual(products.status_code, status.HTTP_200_OK)
		self.assertEqual(products.data["count"], 1)
		self.assertEqual(categories.data["count"], 1)
		self.assertEqual(
			self.client.get(self.product_url(self.other_product)).status_code,
			status.HTTP_404_NOT_FOUND,
		)
		self.assertEqual(
			self.client.get(self.category_url(self.other_category)).status_code,
			status.HTTP_404_NOT_FOUND,
		)
		self.assertEqual(self.client.get(f"{self.product_url()}?search=milk").data["count"], 1)
		self.assertEqual(
			self.client.get(f"{self.product_url()}?category={self.category.pk}").data["count"],
			1,
		)
		self.assertEqual(
			self.client.get(f"{self.product_url()}?category=invalid").status_code,
			status.HTTP_400_BAD_REQUEST,
		)
		self.assertEqual(
			self.client.get(f"{self.category_url()}?search=bev").data["count"], 1
		)
		self.assertEqual(
			self.client.patch(self.product_url(self.other_product), {"name": "Taken"}, format="json").status_code,
			status.HTTP_404_NOT_FOUND,
		)
		self.assertEqual(
			self.client.post(f"{self.product_url(self.other_product)}deactivate/").status_code,
			status.HTTP_404_NOT_FOUND,
		)
		self.assertEqual(
			self.client.patch(self.category_url(self.other_category), {"name": "Taken"}, format="json").status_code,
			status.HTTP_404_NOT_FOUND,
		)
		self.assertEqual(
			self.client.post(f"{self.category_url(self.other_category)}deactivate/").status_code,
			status.HTTP_404_NOT_FOUND,
		)
		self.assertEqual(
			self.client.get(f"{self.product_url()}?is_active=false").data["count"], 0
		)
		self.assertEqual(
			self.client.get(f"{self.category_url()}?is_active=true").data["count"], 1
		)
		self.assertEqual(self.client.delete(self.product_url(self.product)).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
		self.assertEqual(self.client.delete(self.category_url(self.category)).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

	def test_authentication_and_inactive_membership_are_required(self):
		self.assertEqual(self.client.get(self.product_url()).status_code, status.HTTP_401_UNAUTHORIZED)
		inactive = self.make_member(
			"inactive@example.com", self.org_a, Membership.Role.OWNER, is_active=False
		)
		self.authenticate(inactive)
		self.assertEqual(self.client.get(self.product_url()).status_code, status.HTTP_403_FORBIDDEN)

	def test_category_deactivation_does_not_change_existing_product(self):
		self.authenticate(self.owner)
		response = self.client.post(f"{self.category_url(self.category)}deactivate/")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.product.refresh_from_db()
		self.category.refresh_from_db()
		self.assertEqual(self.product.category_id, self.category.pk)
		self.assertFalse(self.category.is_active)

