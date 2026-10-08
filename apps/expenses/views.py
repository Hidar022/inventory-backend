from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.dashboard.services import log_activity_event
from apps.expenses.models import Expense, ExpenseCategory
from apps.expenses.serializers import (
    CashSummarySerializer,
    ExpenseCategorySerializer,
    ExpenseSerializer,
)
from apps.expenses.services import get_daily_cash_summary
from apps.organizations.permissions import (
    IsOrganizationMember,
    IsOwnerOrManager,
    OrganizationContextMixin,
)


@extend_schema_view(
    list=extend_schema(description="List expense categories in the active organization."),
    create=extend_schema(description="Owner and Manager only. Create an expense category."),
    retrieve=extend_schema(description="Retrieve an expense category in the active organization."),
    update=extend_schema(description="Owner and Manager only. Update an expense category."),
    partial_update=extend_schema(description="Owner and Manager only. Update an expense category."),
)
class ExpenseCategoryViewSet(
    OrganizationContextMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ExpenseCategorySerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

    def get_permissions(self):
        permission_list = super().get_permissions()
        if self.request.method not in permissions.SAFE_METHODS:
            permission_list.append(IsOwnerOrManager())
        return permission_list

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return ExpenseCategory.objects.none()
        return ExpenseCategory.objects.filter(organization=organization)

    def perform_create(self, serializer):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            raise PermissionDenied("You do not have access to an active organization.")
        try:
            with transaction.atomic():
                category = serializer.save(organization=organization)
                log_activity_event(
                    organization=organization,
                    actor=self.request.user,
                    action="expense_category.created",
                    entity_type="ExpenseCategory",
                    entity_id=str(category.pk),
                    description=f"Created expense category {category.name}",
                    metadata={"name": category.name},
                )
        except IntegrityError as exc:
            raise ValidationError({"name": ["This category name is already in use."]}) from exc

    def perform_update(self, serializer):
        changed_fields = sorted(serializer.validated_data)
        try:
            with transaction.atomic():
                category = serializer.save()
                log_activity_event(
                    organization=category.organization,
                    actor=self.request.user,
                    action="expense_category.updated",
                    entity_type="ExpenseCategory",
                    entity_id=str(category.pk),
                    description=f"Updated expense category {category.name}",
                    metadata={"changed_fields": changed_fields},
                )
        except IntegrityError as exc:
            raise ValidationError({"name": ["This category name is already in use."]}) from exc

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="Expense categories cannot be deleted.")

    @action(detail=True, methods=["post"])
    @extend_schema(request=None, responses={200: ExpenseCategorySerializer})
    def deactivate(self, request, *args, **kwargs):
        category = self.get_object()
        if category.is_active:
            with transaction.atomic():
                category.is_active = False
                category.save(update_fields=["is_active", "updated_at"])
                log_activity_event(
                    organization=category.organization,
                    actor=request.user,
                    action="expense_category.deactivated",
                    entity_type="ExpenseCategory",
                    entity_id=str(category.pk),
                    description=f"Deactivated expense category {category.name}",
                )
        return Response(self.get_serializer(category).data, status=status.HTTP_200_OK)


@extend_schema_view(
    list=extend_schema(description="Owner and Manager only. List expenses in the active organization."),
    create=extend_schema(description="Owner and Manager only. Record an immutable expense."),
    retrieve=extend_schema(description="Owner and Manager only. Retrieve an expense."),
)
class ExpenseViewSet(
    OrganizationContextMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember, IsOwnerOrManager]

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Expense.objects.none()
        return Expense.objects.filter(organization=organization).select_related(
            "category", "created_by"
        )

    def perform_create(self, serializer):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            raise PermissionDenied("You do not have access to an active organization.")
        with transaction.atomic():
            instance = serializer.save(organization=organization, created_by=self.request.user)
            log_activity_event(
                organization=organization,
                actor=self.request.user,
                action="expense.created",
                entity_type="Expense",
                entity_id=str(instance.pk),
                description=f"Recorded expense {instance.description or 'expense'} for {instance.amount}",
                metadata={
                    "amount": str(instance.amount),
                    "category_id": str(instance.category_id),
                    "payment_method": instance.payment_method,
                    "expense_date": instance.expense_date.isoformat(),
                },
            )
        return instance

class DailyCashSummaryView(OrganizationContextMixin, APIView):
    permission_classes = [
        permissions.IsAuthenticated,
        IsOrganizationMember,
        IsOwnerOrManager,
    ]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "date",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                description="Business date in Africa/Lagos; defaults to today.",
            ),
        ],
        responses=CashSummarySerializer,
        description=(
            "Returns daily cash movement, not an accounting balance. Includes paid cash sales, "
            "cash expenses, and completed cash refunds. Supplier purchases are excluded because "
            "purchase records do not track payment method."
        ),
    )
    def get(self, request, *args, **kwargs):
        raw_date = request.query_params.get("date")
        if raw_date:
            try:
                business_date = parse_date(raw_date)
            except ValueError:
                business_date = None
            if business_date is None:
                raise ValidationError({"date": "Use a valid date in YYYY-MM-DD format."})
        else:
            business_date = timezone.localdate()

        if business_date > timezone.localdate():
            raise ValidationError({"date": "Cash summary date cannot be in the future."})

        summary = get_daily_cash_summary(request.organization, business_date)
        return Response(CashSummarySerializer(summary).data)