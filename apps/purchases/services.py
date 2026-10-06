from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Product
from apps.inventory.models import StockMovement
from apps.purchases.models import Purchase, PurchaseItem


@transaction.atomic
def receive_purchase(*, organization, purchase_id, actor):
    purchase = (
        Purchase.objects.select_for_update()
        .select_related("supplier", "created_by")
        .get(pk=purchase_id, organization=organization)
    )

    if purchase.status != Purchase.Status.DRAFT:
        raise ValidationError({"status": ["Only DRAFT purchases can be received."]})

    items = list(
        purchase.items.select_related("product").order_by("id")
    )
    if not items:
        raise ValidationError({"items": ["A purchase requires at least one item."]})

    product_ids = [item.product_id for item in items]
    products = list(
        Product.objects.select_for_update()
        .filter(organization=organization, pk__in=product_ids)
        .order_by("id")
    )
    products_by_id = {product.pk: product for product in products}
    if len(products_by_id) != len(product_ids):
        raise ValidationError({"items": ["One or more products are no longer valid in this organization."]})

    stock_movements = []
    for item in items:
        product = products_by_id.get(item.product_id)
        if product is None:
            raise ValidationError({"items": [f"Product {item.product_name} is unavailable in this organization."]})
        if not product.is_active:
            raise ValidationError({"items": [f"Product {item.product_name} is inactive."]})
        previous_quantity = product.stock_quantity
        new_quantity = previous_quantity + int(item.quantity)
        product.stock_quantity = new_quantity
        product.save(update_fields=["stock_quantity", "updated_at"])
        stock_movements.append(
            StockMovement(
                organization=organization,
                product=product,
                movement_type=StockMovement.MovementType.STOCK_IN,
                quantity=int(item.quantity),
                previous_quantity=previous_quantity,
                new_quantity=new_quantity,
                reason=f"Purchase {purchase.reference_number or str(purchase.pk)[:8]}",
                reference_type="purchase",
                reference_id=str(purchase.pk),
                created_by=actor,
            )
        )

    purchase.status = Purchase.Status.RECEIVED
    purchase.received_at = timezone.now()
    purchase.save(update_fields=["status", "received_at", "updated_at"])
    StockMovement.objects.bulk_create(stock_movements)
    purchase.refresh_from_db()
    return purchase


@transaction.atomic
def cancel_purchase(*, organization, purchase_id):
    purchase = (
        Purchase.objects.select_for_update()
        .get(pk=purchase_id, organization=organization)
    )
    if purchase.status != Purchase.Status.DRAFT:
        raise ValidationError({"status": ["Only DRAFT purchases can be cancelled."]})
    purchase.status = Purchase.Status.CANCELLED
    purchase.save(update_fields=["status", "updated_at"])
    return purchase


def calculate_purchase_totals(items, discount):
    subtotal = sum(
        (Decimal(str(item["quantity"])) * Decimal(str(item["unit_cost"])) for item in items),
        Decimal("0.00"),
    )
    discount = Decimal(str(discount))
    if discount > subtotal:
        raise ValidationError({"discount": ["Discount cannot exceed the subtotal."]})
    total = subtotal - discount
    if total < 0:
        raise ValidationError({"total": ["Purchase total cannot be negative."]})
    return subtotal, discount, total
