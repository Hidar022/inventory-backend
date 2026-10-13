from rest_framework.permissions import BasePermission

from apps.platform_admin.roles import (
    PLATFORM_ADMIN_GROUP,
    PLATFORM_ADMIN_PERMISSION,
    PLATFORM_FINANCE,
    PLATFORM_ROLE_GROUPS,
    PLATFORM_ROLE_PERMISSIONS,
    PLATFORM_SUPER_ADMIN,
    PLATFORM_SUPPORT,
)


ROLE_REQUIRED_PERMISSION = {
    PLATFORM_SUPER_ADMIN: "manage_platform_team",
    PLATFORM_SUPPORT: "view_platform_support_data",
    PLATFORM_FINANCE: "view_platform_finance_data",
}


def assigned_platform_roles(user):
    if not user or not user.is_authenticated:
        return []
    roles = []
    for role in (PLATFORM_SUPER_ADMIN, PLATFORM_FINANCE, PLATFORM_SUPPORT):
        if user.groups.filter(
            name=PLATFORM_ROLE_GROUPS[role],
            permissions__content_type__app_label="platform_admin",
            permissions__codename=ROLE_REQUIRED_PERMISSION[role],
        ).exists():
            roles.append(role)
    return roles


def user_platform_role(user):
    if not user or not user.is_authenticated or not user.is_active:
        return None
    roles = assigned_platform_roles(user)
    return roles[0] if roles else None


def user_has_platform_access(user):
    return user_platform_role(user) is not None


def configure_platform_role_group(group, role):
    from django.contrib.auth.models import Permission

    codenames = PLATFORM_ROLE_PERMISSIONS[role]
    permissions = Permission.objects.filter(
        content_type__app_label="platform_admin",
        codename__in=codenames,
    )
    if permissions.count() != len(codenames):
        return False
    group.permissions.set(permissions)
    return True


def get_or_configure_platform_role_group(role, *, lock=False):
    from django.contrib.auth.models import Group

    group, _created = Group.objects.get_or_create(name=PLATFORM_ROLE_GROUPS[role])
    if lock:
        group = Group.objects.select_for_update().get(pk=group.pk)
    if not configure_platform_role_group(group, role):
        raise RuntimeError("Platform role permissions are not installed. Run migrations first.")
    return group


class PlatformRolePermission(BasePermission):
    message = "This platform role is not authorized for the requested operation."
    allowed_roles = ()

    def has_permission(self, request, view):
        return user_platform_role(request.user) in self.allowed_roles


class IsPlatformSuperAdmin(PlatformRolePermission):
    allowed_roles = (PLATFORM_SUPER_ADMIN,)


class IsPlatformSupportReader(PlatformRolePermission):
    allowed_roles = (PLATFORM_SUPER_ADMIN, PLATFORM_SUPPORT)


class IsPlatformFinanceReader(PlatformRolePermission):
    allowed_roles = (PLATFORM_SUPER_ADMIN, PLATFORM_FINANCE)


class IsPlatformSupportOrFinanceReader(PlatformRolePermission):
    allowed_roles = (PLATFORM_SUPER_ADMIN, PLATFORM_SUPPORT, PLATFORM_FINANCE)