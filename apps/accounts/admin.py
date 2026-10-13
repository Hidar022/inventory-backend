from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm
from django.contrib.auth.forms import UserCreationForm as DjangoUserCreationForm
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User


class UserChangeForm(DjangoUserChangeForm):
	def clean(self):
		cleaned_data = super().clean()
		from apps.platform_admin.roles import PLATFORM_ROLE_GROUPS

		protected_groups = Group.objects.filter(name__in=PLATFORM_ROLE_GROUPS.values())
		existing_platform_member = bool(
			self.instance.pk
			and self.instance.groups.filter(pk__in=protected_groups.values("pk")).exists()
		)
		selected_groups = cleaned_data.get("groups")
		selected_platform_member = bool(
			selected_groups is not None
			and selected_groups.filter(pk__in=protected_groups.values("pk")).exists()
		)
		if existing_platform_member != selected_platform_member:
			raise ValidationError(
				"Use the audited platform team operation to change platform role access."
			)
		if existing_platform_member and "is_active" in self.changed_data:
			raise ValidationError(
				"Use the audited Platform Control Center action to change this administrator's account status."
			)
		return cleaned_data

	class Meta(DjangoUserChangeForm.Meta):
		model = User
		fields = "__all__"


class UserCreationForm(DjangoUserCreationForm):
	username = None

	def clean(self):
		cleaned_data = super().clean()
		from apps.platform_admin.roles import PLATFORM_ROLE_GROUPS

		groups = cleaned_data.get("groups")
		if groups and groups.filter(name__in=PLATFORM_ROLE_GROUPS.values()).exists():
			raise ValidationError(
				"Use the audited platform team invitation flow to grant platform access."
			)
		return cleaned_data

	class Meta(DjangoUserCreationForm.Meta):
		model = User
		fields = ("email",)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
	form = UserChangeForm
	add_form = UserCreationForm
	ordering = ("email",)
	list_display = (
		"email",
		"first_name",
		"last_name",
		"is_staff",
		"is_active",
		"is_superuser",
	)
	list_filter = ("is_staff", "is_superuser", "is_active", "groups")
	search_fields = ("email", "first_name", "last_name")
	fieldsets = (
		(None, {"fields": ("email", "password")}),
		("Personal info", {"fields": ("first_name", "last_name")}),
		(
			"Permissions",
			{
				"fields": (
					"is_active",
					"is_staff",
					"is_superuser",
					"groups",
					"user_permissions",
				)
			},
		),
		("Important dates", {"fields": ("last_login", "date_joined")}),
	)
	add_fieldsets = (
		(
			None,
			{
				"classes": ("wide",),
				"fields": (
					"email",
					"first_name",
					"last_name",
					"password1",
					"password2",
					"is_active",
					"is_staff",
					"is_superuser",
					"groups",
					"user_permissions",
				),
			},
		),
	)

	def has_delete_permission(self, request, obj=None):
		return False


admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(DjangoGroupAdmin):
	def has_change_permission(self, request, obj=None):
		from apps.platform_admin.roles import PLATFORM_ROLE_GROUPS

		if obj and obj.name in PLATFORM_ROLE_GROUPS.values():
			return False
		return super().has_change_permission(request, obj)

	def has_delete_permission(self, request, obj=None):
		from apps.platform_admin.roles import PLATFORM_ROLE_GROUPS

		if obj and obj.name in PLATFORM_ROLE_GROUPS.values():
			return False
		return super().has_delete_permission(request, obj)

	def delete_queryset(self, request, queryset):
		from apps.platform_admin.roles import PLATFORM_ROLE_GROUPS

		if queryset.filter(name__in=PLATFORM_ROLE_GROUPS.values()).exists():
			raise PermissionDenied(
				"The Platform Super Admin group can only be managed through the audited operator command."
			)
		super().delete_queryset(request, queryset)
