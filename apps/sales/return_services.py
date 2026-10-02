from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import NotFound, ValidationError

from apps.catalog.models import Product
from apps.inventory.models import StockMovement
from apps.sales.models import Sale, SaleItem, SaleReturn, SaleReturnItem


@transaction.atomic
def process_sale_return(*, organization, sale_id, processed_by, reason, refund_method, items):
    sale = (
        Sale.objects.select_for_update()
        .filter(organization=organization, pk=sale_id)
        .first()
    )
    if sale is None:
        raise NotFound("Sale not found in this organization.")
    if sale.status not in {Sale.Status.COMPLETED, Sale.Status.PARTIALLY_RETURNED}:
        raise ValidationError({"sale": ["Only completed sales with returnable quantities can be returned."]})
    if sale.payment_status != Sale.PaymentStatus.PAID:
        raise ValidationError({"sale": ["Only fully paid sales can be returned."]})

    sale_items = list(
        SaleItem.objects.select_for_update()
        .filter(sale=sale)
        .order_by("id")
    )
    items_by_id = {sale_item.pk: sale_item for sale_item in sale_items}
    requested = {item["sale_item_id"]: item["quantity"] for item in items}
    unknown_ids = set(requested) - set(items_by_id)
    if unknown_ids:
        raise ValidationError({"items": ["Each item must belong to the requested sale."]})

    prior_returns = (
        SaleReturnItem.objects.filter(
            sale_item_id__in=requested,
            sale_return__sale=sale,
            sale_return__status=SaleReturn.Status.COMPLETED,
        )
        .values("sale_item_id")
        .annotate(returned=Sum("quantity_returned"))
    )
    returned_by_item = {row["sale_item_id"]: row["returned"] for row in prior_returns}

    for sale_item_id, quantity in requested.items():
        sale_item = items_by_id[sale_item_id]
        remaining = sale_item.quantity - returned_by_item.get(sale_item_id, 0)
        if quantity > remaining:
            raise ValidationError(
                {
                    "items": [
                        f"Return quantity for {sale_item.product_name} exceeds the remaining quantity ({remaining})."
                    ]
                }
            )

    product_ids = sorted({items_by_id[sale_item_id].product_id for sale_item_id in requested})
    products = list(
        Product.objects.select_for_update()
        .filter(organization=organization, pk__in=product_ids)
        .order_by("id")
    )
    products_by_id = {product.pk: product for product in products}
    if len(products_by_id) != len(product_ids):
        raise ValidationError({"items": ["A returned product is unavailable in this organization."]})

    return_item_values = []
    refund_amount = Decimal("0.00")
    for sale_item_id, quantity in requested.items():
        sale_item = items_by_id[sale_item_id]
        item_refund = sale_item.unit_price * quantity
        refund_amount += item_refund
        return_item_values.append(
            {
                "sale_item": sale_item,
                "product": products_by_id[sale_item.product_id],
                "quantity": quantity,
                "refund_amount": item_refund,
            }
        )

    sale_return = SaleReturn.objects.create(
        organization=organization,
        sale=sale,
        processed_by=processed_by,
        reason=reason,
        status=SaleReturn.Status.COMPLETED,
        refund_amount=refund_amount,
        refund_method=refund_method,
    )
    SaleReturnItem.objects.bulk_create(
        [
            SaleReturnItem(
                sale_return=sale_return,
                sale_item=value["sale_item"],
                product=value["product"],
                product_name=value["sale_item"].product_name,
                product_sku=value["sale_item"].product_sku,
                quantity_returned=value["quantity"],
                unit_price=value["sale_item"].unit_price,
                refund_amount=value["refund_amount"],
            )
            for value in return_item_values
        ]
    )

    movements = []
    for value in return_item_values:
        product = value["product"]
        previous_quantity = product.stock_quantity
        product.stock_quantity += value["quantity"]
        product.save(update_fields=["stock_quantity", "updated_at"])
        movements.append(
            StockMovement(
                organization=organization,
                product=product,
                movement_type=StockMovement.MovementType.SALE_RETURN,
                quantity=value["quantity"],
                previous_quantity=previous_quantity,
                new_quantity=product.stock_quantity,
                reason=f"Sale return: {reason}"[:500],
                reference_type="sale_return",
                reference_id=str(sale_return.pk),
                created_by=processed_by,
            )
        )
    StockMovement.objects.bulk_create(movements)

    for sale_item_id, quantity in requested.items():
        returned_by_item[sale_item_id] = returned_by_item.get(sale_item_id, 0) + quantity
    fully_returned = all(
        returned_by_item.get(sale_item.pk, 0) >= sale_item.quantity
        for sale_item in sale_items
    )
    sale.status = Sale.Status.FULLY_RETURNED if fully_returned else Sale.Status.PARTIALLY_RETURNED
    sale.save(update_fields=["status", "updated_at"])
    return sale_return