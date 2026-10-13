PLATFORM_SUPER_ADMIN = "PLATFORM_SUPER_ADMIN"
PLATFORM_SUPPORT = "PLATFORM_SUPPORT"
PLATFORM_FINANCE = "PLATFORM_FINANCE"

PLATFORM_ROLE_CHOICES = (
    (PLATFORM_SUPER_ADMIN, "Platform Super Admin"),
    (PLATFORM_SUPPORT, "Platform Support"),
    (PLATFORM_FINANCE, "Platform Finance"),
)

PLATFORM_ROLE_GROUPS = {
    PLATFORM_SUPER_ADMIN: "Platform Super Admins",
    PLATFORM_SUPPORT: "Platform Support",
    PLATFORM_FINANCE: "Platform Finance",
}

PLATFORM_ROLE_PERMISSIONS = {
    PLATFORM_SUPER_ADMIN: {
        "access_platform_control_center",
        "view_platform_support_data",
        "view_platform_finance_data",
        "manage_platform_organizations",
        "manage_platform_users",
        "manage_platform_billing",
        "manage_platform_team",
    },
    PLATFORM_SUPPORT: {
        "access_platform_control_center",
        "view_platform_support_data",
    },
    PLATFORM_FINANCE: {
        "access_platform_control_center",
        "view_platform_finance_data",
    },
}

PLATFORM_ADMIN_GROUP = PLATFORM_ROLE_GROUPS[PLATFORM_SUPER_ADMIN]
PLATFORM_ADMIN_PERMISSION = "access_platform_control_center"