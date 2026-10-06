from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.expenses.models import Expense, ExpenseCategory
from apps.organizations.models import Membership, Organization
from apps.purchases.models import Purchase, Supplier
from apps.sales.models import Payment, Sale, SaleReturn
from apps.sales.queries import local_day_bounds

User = get_user_model()
PASSWORD = "StrongPass123!"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExpensesAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org_a = Organization.objects.create(name="Org A", slug="org-a")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b")
        self.owner = self.make_member("owner@example.com", self.org_a, Membership.Role.OWNER)
        self.manager = self.make_member("manager@example.com", self.org_a, Membership.Role.MANAGER)
        self.cashier = self.make_member("cashier@example.com", self.org_a, Membership.Role.CASHIER)
        self.other_owner = self.make_member(
            "other-owner@example.com", self.org_b, Membership.Role.OWNER
        )
        self.category = ExpenseCategory.objects.create(
            organization=self.org_a,
            name="Utilities",
            description="Monthly utilities",
        )
        self.foreign_category = ExpenseCategory.objects.create(
            organization=self.org_b,
            name="Utilities",
        )

    def make_member(self, email, organization, role):
        user = User.objects.create_user(email=email, password=PASSWORD)
        Membership.objects.create(user=user, organization=organization, role=role)
        return user

    def authenticate(self, user):
        self.client.force_authenticate(user=user)

    def expense_payload(self, **overrides):
        payload = {
            "category": str(self.category.pk),
            "amount": "12.34",
            "payment_method": Payment.Method.CASH,
            "description": "Office supplies",
            "reference": "INV-100",
            "expense_date": timezone.localdate().isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_category_crud_deactivation_and_cashier_read_only_access(self):
        self.authenticate(self.owner)
        created = self.client.post(
            "/api/v1/expense-categories/",
            {
                "name": "Transport",
                "description": "Local deliveries",
                "organization": str(self.org_b.pk),
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(
            ExpenseCategory.objects.get(pk=created.data["id"]).organization,
            self.org_a,
        )
        category_id = created.data["id"]
        self.assertEqual(self.client.get("/api/v1/expense-categories/").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/v1/expense-categories/{category_id}/").status_code,
            status.HTTP_200_OK,
        )

        self.authenticate(self.manager)
        updated = self.client.patch(
            f"/api/v1/expense-categories/{category_id}/",
            {"description": "Vehicle and delivery costs"},
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        self.assertEqual(updated.data["description"], "Vehicle and delivery costs")
        deactivated = self.client.post(
            f"/api/v1/expense-categories/{category_id}/deactivate/"
        )
        self.assertEqual(deactivated.status_code, status.HTTP_200_OK)
        self.assertFalse(deactivated.data["is_active"])
        self.assertEqual(
            self.client.delete(f"/api/v1/expense-categories/{category_id}/").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

        self.authenticate(self.cashier)
        self.assertEqual(self.client.get("/api/v1/expense-categories/").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/v1/expense-categories/",
                {"name": "Blocked"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/expense-categories/{category_id}/",
                {"is_active": True},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_category_names_are_unique_per_organization_case_insensitively(self):
        self.assertEqual(self.category.name, self.foreign_category.name)
        self.authenticate(self.owner)
        duplicate = self.client.post(
            "/api/v1/expense-categories/",
            {"name": "utilities"},
            format="json",
        )
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)

        self.foreign_category.name = "Transport"
        self.foreign_category.save(update_fields=["name"])
        self.authenticate(self.other_owner)
        same_name_other_org = self.client.post(
            "/api/v1/expense-categories/",
            {"name": "utilities"},
            format="json",
        )
        self.assertEqual(same_name_other_org.status_code, status.HTTP_201_CREATED)

    def test_categories_are_tenant_scoped(self):
        self.authenticate(self.owner)
        self.assertEqual(
            self.client.get(f"/api/v1/expense-categories/{self.foreign_category.pk}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(self.client.get("/api/v1/expense-categories/").data["count"], 1)

    def test_expense_create_uses_server_actor_and_tenant_and_is_immutable(self):
        self.authenticate(self.owner)
        response = self.client.post(
            "/api/v1/expenses/",
            self.expense_payload(
                organization=str(self.org_b.pk),
                created_by=self.other_owner.pk,
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        expense = Expense.objects.get(pk=response.data["id"])
        self.assertEqual(expense.organization, self.org_a)
        self.assertEqual(expense.created_by, self.owner)
        self.assertEqual(expense.amount, Decimal("12.34"))
        self.assertEqual(expense.category, self.category)
        self.assertEqual(self.client.get("/api/v1/expenses/").data["count"], 1)
        self.client.post(f"/api/v1/expense-categories/{self.category.pk}/deactivate/")
        self.assertEqual(
            self.client.get(f"/api/v1/expenses/{expense.pk}/").status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/expenses/{expense.pk}/").data["category_name"],
            "Utilities",
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/expenses/{expense.pk}/",
                {"amount": "1.00"},
                format="json",
            ).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.delete(f"/api/v1/expenses/{expense.pk}/").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("12.34"))

    def test_manager_can_create_expenses(self):
        self.authenticate(self.manager)
        response = self.client.post(
            "/api/v1/expenses/",
            self.expense_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(Expense.objects.get(pk=response.data["id"]).created_by, self.manager)

    def test_expense_amount_method_category_and_date_validation(self):
        self.authenticate(self.owner)
        for invalid_amount in ("0.00", "-1.00"):
            response = self.client.post(
                "/api/v1/expenses/",
                self.expense_payload(amount=invalid_amount),
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(
            self.client.post(
                "/api/v1/expenses/",
                self.expense_payload(payment_method="card"),
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/expenses/",
                self.expense_payload(category=str(self.foreign_category.pk)),
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.category.is_active = False
        self.category.save(update_fields=["is_active"])
        self.assertEqual(
            self.client.post(
                "/api/v1/expenses/",
                self.expense_payload(),
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.category.is_active = True
        self.category.save(update_fields=["is_active"])
        future_date = (timezone.localdate() + timezone.timedelta(days=1)).isoformat()
        self.assertEqual(
            self.client.post(
                "/api/v1/expenses/",
                self.expense_payload(expense_date=future_date),
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_expenses_and_summary_are_not_visible_to_cashiers(self):
        self.authenticate(self.cashier)
        self.assertEqual(self.client.get("/api/v1/expenses/").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/v1/expenses/",
                self.expense_payload(),
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(self.client.get("/api/v1/cash/summary/").status_code, 403)

    def make_sale(self, organization, cashier, receipt, payment_status, sale_status=None):
        return Sale.objects.create(
            organization=organization,
            receipt_number=receipt,
            cashier=cashier,
            subtotal=Decimal("10.00"),
            discount=Decimal("0.00"),
            total=Decimal("10.00"),
            status=sale_status or Sale.Status.COMPLETED,
            payment_status=payment_status,
            idempotency_key=receipt,
        )

    def add_payment(self, sale, method, amount, created_at):
        payment = Payment.objects.create(
            sale=sale,
            organization=sale.organization,
            method=method,
            amount=Decimal(amount),
            created_by=sale.cashier,
        )
        Payment.objects.filter(pk=payment.pk).update(created_at=created_at)
        return payment

    def add_return(self, sale, method, amount, created_at, return_status):
        sale_return = SaleReturn.objects.create(
            organization=sale.organization,
            sale=sale,
            processed_by=sale.cashier,
            reason="Customer return",
            status=return_status,
            refund_amount=Decimal(amount),
            refund_method=method,
        )
        SaleReturn.objects.filter(pk=sale_return.pk).update(created_at=created_at)
        return sale_return

    def test_cash_summary_aggregates_cash_sales_expenses_and_cash_refunds(self):
        business_date = timezone.localdate()
        start, _ = local_day_bounds(business_date)
        self.make_sale(self.org_a, self.owner, "CASH-1", Sale.PaymentStatus.PAID)
        paid_cash_sale = Sale.objects.get(receipt_number="CASH-1")
        self.add_payment(paid_cash_sale, Payment.Method.CASH, "20.00", start)
        paid_transfer_sale = self.make_sale(
            self.org_a, self.owner, "TRANSFER-1", Sale.PaymentStatus.PAID
        )
        self.add_payment(paid_transfer_sale, Payment.Method.TRANSFER, "10.00", start)
        unpaid_sale = self.make_sale(self.org_a, self.owner, "UNPAID-1", Sale.PaymentStatus.UNPAID)
        self.add_payment(unpaid_sale, Payment.Method.CASH, "33.00", start)
        cancelled_sale = self.make_sale(
            self.org_a, self.owner, "INVALID-1", Sale.PaymentStatus.PAID, "CANCELLED"
        )
        self.add_payment(cancelled_sale, Payment.Method.CASH, "44.00", start)

        Expense.objects.create(
            organization=self.org_a,
            category=self.category,
            amount=Decimal("3.25"),
            payment_method=Payment.Method.CASH,
            description="Cash expense",
            expense_date=business_date,
            created_by=self.owner,
        )
        Expense.objects.create(
            organization=self.org_a,
            category=self.category,
            amount=Decimal("4.00"),
            payment_method=Payment.Method.TRANSFER,
            description="Transfer expense",
            expense_date=business_date,
            created_by=self.owner,
        )
        self.add_return(
            paid_cash_sale,
            Payment.Method.CASH,
            "2.50",
            start,
            SaleReturn.Status.COMPLETED,
        )
        self.add_return(
            paid_cash_sale,
            Payment.Method.TRANSFER,
            "1.00",
            start,
            SaleReturn.Status.COMPLETED,
        )
        self.add_return(
            paid_cash_sale,
            Payment.Method.CASH,
            "5.00",
            start,
            SaleReturn.Status.CANCELLED,
        )
        supplier = Supplier.objects.create(organization=self.org_a, name="No payment data")
        Purchase.objects.create(
            organization=self.org_a,
            supplier=supplier,
            reference_number="RECEIVED-NO-PAYMENT",
            status=Purchase.Status.RECEIVED,
            subtotal=Decimal("500.00"),
            total=Decimal("500.00"),
            created_by=self.owner,
            received_at=start,
        )

        self.authenticate(self.owner)
        response = self.client.get(
            "/api/v1/cash/summary/",
            {"date": business_date.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["cash_sales"], "20.00")
        self.assertEqual(response.data["cash_expenses"], "3.25")
        self.assertEqual(response.data["cash_refunds"], "2.50")
        self.assertEqual(response.data["cash_in"], "20.00")
        self.assertEqual(response.data["cash_out"], "5.75")
        self.assertEqual(response.data["net_cash_change"], "14.25")

    def test_cash_summary_uses_lagos_day_boundaries(self):
        business_date = timezone.localdate()
        start, end = local_day_bounds(business_date)
        inside_sale = self.make_sale(self.org_a, self.owner, "BOUNDARY-IN", Sale.PaymentStatus.PAID)
        self.add_payment(inside_sale, Payment.Method.CASH, "7.00", start)
        outside_sale = self.make_sale(
            self.org_a, self.owner, "BOUNDARY-OUT", Sale.PaymentStatus.PAID
        )
        self.add_payment(outside_sale, Payment.Method.CASH, "9.00", end)

        self.authenticate(self.owner)
        response = self.client.get(
            "/api/v1/cash/summary/",
            {"date": business_date.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["cash_in"], "7.00")

    def test_empty_cash_summary_and_date_validation(self):
        business_date = timezone.localdate()
        self.authenticate(self.manager)
        empty = self.client.get(
            "/api/v1/cash/summary/",
            {"date": business_date.isoformat()},
        )
        self.assertEqual(empty.status_code, status.HTTP_200_OK)
        self.assertEqual(empty.data["cash_in"], "0.00")
        self.assertEqual(empty.data["cash_out"], "0.00")
        self.assertEqual(empty.data["net_cash_change"], "0.00")
        default_date = self.client.get("/api/v1/cash/summary/")
        self.assertEqual(default_date.status_code, status.HTTP_200_OK)
        self.assertEqual(default_date.data["date"], business_date.isoformat())
        self.assertEqual(
            self.client.get("/api/v1/cash/summary/?date=not-a-date").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.get("/api/v1/cash/summary/?date=2026-02-30").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        tomorrow = (business_date + timezone.timedelta(days=1)).isoformat()
        self.assertEqual(
            self.client.get(f"/api/v1/cash/summary/?date={tomorrow}").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_cash_summary_and_expenses_are_tenant_scoped(self):
        business_date = timezone.localdate()
        start, _ = local_day_bounds(business_date)
        foreign_sale = self.make_sale(
            self.org_b, self.other_owner, "FOREIGN-SALE", Sale.PaymentStatus.PAID
        )
        self.add_payment(foreign_sale, Payment.Method.CASH, "88.00", start)
        foreign_expense = Expense.objects.create(
            organization=self.org_b,
            category=self.foreign_category,
            amount=Decimal("50.00"),
            payment_method=Payment.Method.CASH,
            description="Foreign expense",
            expense_date=business_date,
            created_by=self.other_owner,
        )

        self.authenticate(self.owner)
        summary = self.client.get(
            "/api/v1/cash/summary/",
            {"date": business_date.isoformat()},
        )
        self.assertEqual(summary.status_code, status.HTTP_200_OK)
        self.assertEqual(summary.data["cash_in"], "0.00")
        self.assertEqual(summary.data["cash_out"], "0.00")
        self.assertEqual(self.client.get("/api/v1/expenses/").data["count"], 0)
        self.assertEqual(
            self.client.get(f"/api/v1/expenses/{foreign_expense.pk}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/expenses/",
                self.expense_payload(category=str(self.foreign_category.pk)),
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )