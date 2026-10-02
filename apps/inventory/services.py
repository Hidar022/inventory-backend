from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from apps.catalog.models import Product
from apps.inventory.models import StockMovement


@transaction.atomic
def create_stock_movement(
	*, organization, product_id, actor, quantity, reason, movement_type
):
	try:
		product = Product.objects.select_for_update().get(
			pk=product_id,
			organization=organization,
		)
	except Product.DoesNotExist as exc:
		raise NotFound("Product not found.") from exc

	if not product.is_active:
		raise ValidationError({"product_id": ["Inactive products cannot be changed."]})

	previous_quantity = product.stock_quantity
	new_quantity = previous_quantity + quantity
	if new_quantity < 0:
		raise ValidationError(
			{"quantity": ["Adjustment would result in negative stock."]}
		)

	product.stock_quantity = new_quantity
	product.save(update_fields=["stock_quantity", "updated_at"])
	return StockMovement.objects.create(
		organization=organization,
		product=product,
		movement_type=movement_type,
		quantity=quantity,
		previous_quantity=previous_quantity,
		new_quantity=new_quantity,
		reason=reason,
		created_by=actor,
	)