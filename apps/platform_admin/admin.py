from django.contrib import admin

from apps.platform_admin.models import PlatformAuditEvent


@admin.register(PlatformAuditEvent)
class PlatformAuditEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "actor",
        "action",
        "target_type",
        "target_id",
        "organization",
    )
    list_filter = ("action", "target_type", "created_at")
    search_fields = ("actor__email", "target_id", "organization__name", "reason")
    readonly_fields = tuple(field.name for field in PlatformAuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False