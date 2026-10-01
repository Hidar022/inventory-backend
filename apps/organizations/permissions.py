from rest_framework.permissions import BasePermission

from apps.organizations.models import Membership


class OrganizationContextMixin:
    def initial(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            membership = (
                Membership.objects.select_related("organization")
                .filter(user=request.user, is_active=True)
                .order_by("-created_at")
                .first()
            )
            request.membership = membership
            request.organization = membership.organization if membership else None
            request.role = membership.role.lower() if membership else None
        else:
            request.membership = None
            request.organization = None
            request.role = None
        super().initial(request, *args, **kwargs)


class IsOrganizationMember(BasePermission):
    message = "You do not have an active organization membership."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        membership = getattr(request, "membership", None)
        return bool(membership and membership.is_active)


class IsOwner(BasePermission):
    message = "Only organization owners can perform this action."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        membership = getattr(request, "membership", None)
        return bool(
            membership
            and membership.is_active
            and membership.role == Membership.Role.OWNER,
        )


class IsOwnerOrManager(BasePermission):
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        membership = getattr(request, "membership", None)
        return bool(
            membership
            and membership.is_active
            and membership.role in {Membership.Role.OWNER, Membership.Role.MANAGER},
        )
