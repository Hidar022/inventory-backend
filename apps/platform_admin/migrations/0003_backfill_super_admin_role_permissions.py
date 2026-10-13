from django.db import migrations


SUPER_ADMIN_GROUP = "Platform Super Admins"
SUPER_ADMIN_PERMISSIONS = (
    ("access_platform_control_center", "Can access the platform control center"),
    ("view_platform_support_data", "Can view platform support data"),
    ("view_platform_finance_data", "Can view platform finance data"),
    ("manage_platform_organizations", "Can manage platform organizations"),
    ("manage_platform_users", "Can manage platform users"),
    ("manage_platform_billing", "Can manage platform billing"),
    ("manage_platform_team", "Can manage the platform team"),
)


def upgrade_existing_super_admin_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    database = schema_editor.connection.alias

    group = Group.objects.using(database).filter(name=SUPER_ADMIN_GROUP).first()
    if group is None:
        return

    legacy_access = Permission.objects.using(database).filter(
        content_type__app_label="platform_admin",
        content_type__model="platformauditevent",
        codename="access_platform_control_center",
    ).first()
    if legacy_access is None or not group.permissions.filter(
        pk=legacy_access.pk
    ).exists():
        return

    content_type_id = legacy_access.content_type_id
    permissions = []
    for codename, name in SUPER_ADMIN_PERMISSIONS:
        permission, _ = Permission.objects.using(database).get_or_create(
            content_type_id=content_type_id,
            codename=codename,
            defaults={"name": name},
        )
        permissions.append(permission)
    group.permissions.add(*permissions)


def noop_reverse(apps, schema_editor):
    """Keep upgraded grants intact when rolling back application code."""


class Migration(migrations.Migration):
    dependencies = [
        ("platform_admin", "0002_alter_platformauditevent_options_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(
            upgrade_existing_super_admin_group,
            noop_reverse,
        ),
    ]
