import json
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Product
from apps.dashboard.services import log_activity_event
from apps.inventory.models import StockMovement
from apps.organizations.models import Organization
from apps.sales.models import Payment, Sale, SaleItem


def normalize_item_quantities(items):
    normalized = defaultdict(int)
    product_map = {}
    for item in items:
        product = item["product_id"]
        product_map[product.pk] = product
        normalized[product.pk] += int(item["quantity"])
    return [
        {"product_id": product_map[product_id], "quantity": quantity}
        for product_id, quantity in sorted(normalized.items())
    ]


def canonical_request_hash(idempotency_key, items, discount, payments):
    payload = {
        "idempotency_key": idempotency_key,
        "discount": str(discount),
        "items": [
            {
                "product_id": item["product_id"].pk,
                "quantity": int(item["quantity"]),
            }
            for item in normalize_item_quantities(items)
        ],
        "payments": [
            {"method": payment["method"], "amount": str(payment["amount"])}
            for payment in payments
        ],
    }
    normalized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return sha256(normalized.encode("utf-8")).hexdigest()


def generate_receipt_number(organization):
    today = timezone.localdate().strftime("%Y%m%d")
    prefix = f"REC-{today}-"
    latest = (
        Sale.objects.filter(organization=organization, receipt_number__startswith=prefix)
        .order_by("-created_at", "-id")
        .values_list("receipt_number", flat=True)
        .first()
    )
    if latest is None:
        return f"{prefix}000001"
    try:
        sequence = int(latest.rsplit("-", 1)[-1]) + 1
    except ValueError:
        sequence = 1
    return f"{prefix}{sequence:06d}"


def create_sale_record(organization, user, subtotal, discount, total, idempotency_key, request_hash):
    for _ in range(20):
        receipt_number = generate_receipt_number(organization)
        try:
            with transaction.atomic():
                return Sale.objects.create(
                    organization=organization,
                    receipt_number=receipt_number,
                    cashier=user,
                    subtotal=subtotal,
                    discount=discount,
                    total=total,
                    status=Sale.Status.COMPLETED,
                    payment_status=Sale.PaymentStatus.PAID,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
        except IntegrityError:
            existing_idempotent_sale = Sale.objects.filter(
                organization=organization,
                idempotency_key=idempotency_key,
            ).first()
            if existing_idempotent_sale is not None:
                if existing_idempotent_sale.request_hash != request_hash:
                    raise ValidationError(
                        {
                            "idempotency_key": [
                                "This idempotency key has already been used with different checkout data."
                            ]
                        }
                    )
                return existing_idempotent_sale
            existing = Sale.objects.filter(
                organization=organization,
                receipt_number=receipt_number,
            ).first()
            if existing is not None:
                continue
    raise ValidationError({"receipt_number": ["Unable to generate a unique receipt number."]})


@transaction.atomic
def checkout_sale(request, validated_data):
    organization = request.organization
    user = request.user
    idempotency_key = (validated_data["idempotency_key"] or "").strip()
    items = normalize_item_quantities(validated_data["items"])
    payments = list(validated_data.get("payments", []))
    discount = Decimal(str(validated_data.get("discount", "0.00")))
    request_hash = canonical_request_hash(idempotency_key, items, discount, payments)

    existing_sale = (
        Sale.objects.select_for_update()
        .filter(organization=organization, idempotency_key=idempotency_key)
        .first()
    )
    if existing_sale is not None:
        if existing_sale.request_hash != request_hash:
            raise ValidationError(
                {
                    "idempotency_key": [
                        "This idempotency key has already been used with different checkout data."
                    ]
                }
            )
        return existing_sale, False

    product_ids = [item["product_id"].pk for item in items]
    product_queryset = (
        Product.objects.select_for_update()
        .filter(organization=organization, pk__in=product_ids)
        .order_by("id")
    )
    products = list(product_queryset)
    products_by_id = {product.pk: product for product in products}

    if len(products) != len(set(product_ids)):
        invalid_product_ids = set(product_ids) - set(products_by_id)
        raise ValidationError(
            {
                "items": [
                    f"Product {product_id} is unavailable in this organization."
                    for product_id in sorted(invalid_product_ids)
                ]
            }
        )

    for item in items:
        product = item["product_id"]
        quantity = int(item["quantity"])
        if not product.is_active:
            raise ValidationError({"items": [f"Product {product.name} is inactive."]})
        if product.stock_quantity < quantity:
            raise ValidationError(
                {"items": [f"Insufficient stock for product {product.name}."]}
            )

    subtotal = sum(
        (
            products_by_id[item["product_id"].pk].selling_price
            * Decimal(str(item["quantity"]))
        )
        for item in items
    )
    if discount > subtotal:
        raise ValidationError({"discount": ["Discount cannot exceed the subtotal."]})
    total = subtotal - discount
    if total < 0:
        raise ValidationError({"discount": ["Discount cannot make the total negative."]})

    if total == 0:
        if payments and sum((payment["amount"] for payment in payments), Decimal("0.00")) > 0:
            raise ValidationError(
                {"payments": ["Zero-total sales should not include positive payments."]}
            )
        payments = []
    else:
        if not payments:
            raise ValidationError({"payments": ["At least one payment is required."]})
        payment_total = sum((payment["amount"] for payment in payments), Decimal("0.00"))
        if payment_total != total:
            raise ValidationError({"payments": ["Payment total must match the sale total."]})

    try:
        with transaction.atomic():
            # Another transaction may have inserted the same idempotency key before this block commits.
            sale = Sale.objects.filter(
                organization=organization,
                idempotency_key=idempotency_key,
            ).first()
            if sale is not None:
                if sale.request_hash != request_hash:
                    raise ValidationError(
                        {
                            "idempotency_key": [
                                "This idempotency key has already been used with different checkout data."
                            ]
                        }
                    )
                return sale, False

            sale = create_sale_record(
                organization,
                user,
                subtotal,
                discount,
                total,
                idempotency_key,
                request_hash,
            )

            sale_items = []
            for item in items:
                product = item["product_id"]
                quantity = int(item["quantity"])
                line_total = product.selling_price * Decimal(str(quantity))
                sale_items.append(
                    SaleItem(
                        sale=sale,
                        product=product,
                        product_name=product.name,
                        product_sku=product.sku,
                        quantity=quantity,
                        unit_price=product.selling_price,
                        line_total=line_total,
                    )
                )
            SaleItem.objects.bulk_create(sale_items)

            stock_movements = []
            for item in items:
                product = item["product_id"]
                quantity = int(item["quantity"])
                previous_quantity = product.stock_quantity
                new_quantity = previous_quantity - quantity
                product.stock_quantity = new_quantity
                product.save(update_fields=["stock_quantity", "updated_at"])
                stock_movements.append(
                    StockMovement(
                        organization=organization,
                        product=product,
                        movement_type=StockMovement.MovementType.SALE_OUT,
                        quantity=-quantity,
                        previous_quantity=previous_quantity,
                        new_quantity=new_quantity,
                        reason=f"Sale {sale.receipt_number}",
                        reference_type="sale",
                        reference_id=str(sale.pk),
                        created_by=user,
                    )
                )
            StockMovement.objects.bulk_create(stock_movements)

            if payments:
                Payment.objects.bulk_create(
                    [
                        Payment(
                            sale=sale,
                            organization=organization,
                            method=payment["method"],
                            amount=payment["amount"],
                            created_by=user,
                        )
                        for payment in payments
                    ]
                )

            sale.refresh_from_db()
            log_activity_event(
                organization=organization,
                actor=user,
                action="sale.created",
                entity_type="Sale",
                entity_id=str(sale.pk),
                description=f"Completed sale {sale.receipt_number}",
                metadata={
                    "receipt_number": sale.receipt_number,
                    "total": str(sale.total),
                    "item_count": len(items),
                },
            )
            return sale, True
    except IntegrityError:
        sale = Sale.objects.filter(organization=organization, idempotency_key=idempotency_key).first()
        if sale is not None:
            if sale.request_hash != request_hash:
                raise ValidationError(
                    {
                        "idempotency_key": [
                            "This idempotency key has already been used with different checkout data."
                        ]
                    }
                )
            return sale, False
        raise
