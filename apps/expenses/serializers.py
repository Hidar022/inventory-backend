from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.expenses.models import Expense, ExpenseCategory


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        organization = getattr(self.context.get("request"), "organization", None)
        queryset = ExpenseCategory.objects.filter(
            organization=organization,
            name__iexact=value.strip(),
        )
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                "An expense category with this name already exists in this organization."
            )
        return value.strip()


class ExpenseSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id",
            "category",
            "category_name",
            "amount",
            "payment_method",
            "description",
            "reference",
            "expense_date",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]
        extra_kwargs = {"amount": {"min_value": Decimal("0.01")}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        organization = getattr(request, "organization", None)
        self.fields["category"].queryset = ExpenseCategory.objects.filter(
            organization=organization,
            is_active=True,
        )

    def validate_expense_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Expense date cannot be in the future.")
        return value


class CashSummarySerializer(serializers.Serializer):
    date = serializers.DateField()
    cash_sales = serializers.DecimalField(max_digits=14, decimal_places=2)
    cash_expenses = serializers.DecimalField(max_digits=14, decimal_places=2)
    cash_refunds = serializers.DecimalField(max_digits=14, decimal_places=2)
    cash_in = serializers.DecimalField(max_digits=14, decimal_places=2)
    cash_out = serializers.DecimalField(max_digits=14, decimal_places=2)
    net_cash_change = serializers.DecimalField(max_digits=14, decimal_places=2)