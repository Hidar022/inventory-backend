from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower, Upper

from apps.organizations.models import Organization


class Category(models.Model):
	organization = models.ForeignKey(
		Organization,
		on_delete=models.PROTECT,
		related_name="categories",
	)
	name = models.CharField(max_length=100)
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["name"]
		constraints = [
			models.UniqueConstraint(
				"organization",
				Lower("name"),
				name="unique_category_name_per_organization_ci",
			),
		]

	def __str__(self):
		return self.name


class Product(models.Model):
	organization = models.ForeignKey(
		Organization,
		on_delete=models.PROTECT,
		related_name="products",
	)
	category = models.ForeignKey(
		Category,
		on_delete=models.PROTECT,
		related_name="products",
		null=True,
		blank=True,
	)
	name = models.CharField(max_length=200)
	sku = models.CharField(max_length=100)
	unit = models.CharField(max_length=50, default="unit")
	selling_price = models.DecimalField(max_digits=12, decimal_places=2)
	cost_price = models.DecimalField(max_digits=12, decimal_places=2)
	stock_quantity = models.PositiveIntegerField(default=0)
	low_stock_threshold = models.PositiveIntegerField(default=0)
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["name"]
		constraints = [
			models.UniqueConstraint(
				"organization",
				Upper("sku"),
				name="unique_product_sku_per_organization",
			),
			models.CheckConstraint(
				condition=Q(selling_price__gte=0),
				name="product_selling_price_nonnegative",
			),
			models.CheckConstraint(
				condition=Q(cost_price__gte=0),
				name="product_cost_price_nonnegative",
			),
		]

	def __str__(self):
		return f"{self.name} ({self.sku})"
