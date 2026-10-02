from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.catalog.models import Product
from apps.organizations.models import Organization


class StockMovement(models.Model):
	class MovementType(models.TextChoices):
		STOCK_IN = "stock_in", "Stock in"
		ADJUSTMENT_IN = "adjustment_in", "Adjustment in"
		ADJUSTMENT_OUT = "adjustment_out", "Adjustment out"

	organization = models.ForeignKey(
		Organization,
		on_delete=models.PROTECT,
		related_name="stock_movements",
	)
	product = models.ForeignKey(
		Product,
		on_delete=models.PROTECT,
		related_name="stock_movements",
	)
	movement_type = models.CharField(max_length=20, choices=MovementType.choices)
	quantity = models.IntegerField()
	previous_quantity = models.PositiveIntegerField()
	new_quantity = models.PositiveIntegerField()
	reason = models.CharField(max_length=500)
	reference_type = models.CharField(max_length=50, null=True, blank=True)
	reference_id = models.CharField(max_length=100, null=True, blank=True)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.PROTECT,
		related_name="stock_movements_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["-created_at", "-id"]
		indexes = [
			models.Index(
				fields=["organization", "-created_at"],
				name="inv_org_created_at_idx",
			),
			models.Index(
				fields=["organization", "product", "-created_at"],
				name="inv_org_product_created_idx",
			),
		]
		constraints = [
			models.CheckConstraint(
				condition=~Q(quantity=0),
				name="stock_movement_nonzero_quantity",
			),
			models.CheckConstraint(
				condition=Q(previous_quantity__gte=0)
				& Q(new_quantity__gte=0)
				& Q(new_quantity=F("previous_quantity") + F("quantity")),
				name="stock_movement_balances_match",
			),
			models.CheckConstraint(
				condition=(
					Q(movement_type__in=["stock_in", "adjustment_in"], quantity__gt=0)
					| Q(movement_type="adjustment_out", quantity__lt=0)
				),
				name="stock_movement_type_matches_sign",
			),
		]

	def __str__(self):
		return f"{self.product.sku}: {self.movement_type} {self.quantity}"
