from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm
from django.contrib.auth.forms import UserCreationForm as DjangoUserCreationForm

from apps.accounts.models import User


class UserChangeForm(DjangoUserChangeForm):
	class Meta(DjangoUserChangeForm.Meta):
		model = User
		fields = "__all__"


class UserCreationForm(DjangoUserCreationForm):
	username = None

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
