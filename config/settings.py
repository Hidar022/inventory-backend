import sys
from datetime import timedelta
from pathlib import Path

import environ
from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
)
env.read_env(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# Core environment
# ---------------------------------------------------------------------------

DEBUG = env("DJANGO_DEBUG")

SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="unsafe-development-key-do-not-deploy",
)

if not DEBUG and SECRET_KEY == "unsafe-development-key-do-not-deploy":
    raise RuntimeError(
        "DJANGO_SECRET_KEY must be set to a secure value when DEBUG=False."
    )

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1"],
)
if any(arg.startswith("test") for arg in sys.argv):
    ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, "testserver"]))


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "drf_spectacular",

    "apps.accounts.apps.AccountsConfig",
    "apps.organizations.apps.OrganizationsConfig",
    "apps.catalog.apps.CatalogConfig",
    "apps.inventory.apps.InventoryConfig",
    "apps.sales.apps.SalesConfig",
    "apps.purchases.apps.PurchasesConfig",
    "apps.expenses.apps.ExpensesConfig",
    "apps.dashboard.apps.DashboardConfig",
    "apps.billing.apps.BillingConfig",
]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ---------------------------------------------------------------------------
# URL / application servers
# ---------------------------------------------------------------------------

ROOT_URLCONF = "config.urls"

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

DATABASES["default"]["CONN_MAX_AGE"] = env.int(
    "DATABASE_CONN_MAX_AGE",
    default=0 if DEBUG else 60,
)

DATABASES["default"]["CONN_HEALTH_CHECKS"] = env.bool(
    "DATABASE_CONN_HEALTH_CHECKS",
    default=not DEBUG,
)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]


# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": (
        "rest_framework.pagination.PageNumberPagination"
    ),
    "PAGE_SIZE": 20,
}


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}


# ---------------------------------------------------------------------------
# API schema
# ---------------------------------------------------------------------------

SPECTACULAR_SETTINGS = {
    "TITLE": "Inventory SaaS API",
    "DESCRIPTION": "Multi-tenant inventory and point-of-sale API.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "PaymentMethodEnum": "apps.sales.models.Payment.Method",
    },
}


# ---------------------------------------------------------------------------
# CORS / CSRF
# ---------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=["http://localhost:5173", "http://localhost:5174"],
)

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=CORS_ALLOWED_ORIGINS,
)


# ---------------------------------------------------------------------------
# HTTPS / security
# ---------------------------------------------------------------------------

SECURE_SSL_REDIRECT = env.bool(
    "SECURE_SSL_REDIRECT",
    default=not DEBUG,
)

SECURE_HSTS_SECONDS = env.int(
    "SECURE_HSTS_SECONDS",
    default=31536000 if not DEBUG else 0,
)

SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=False,
)

SECURE_HSTS_PRELOAD = env.bool(
    "SECURE_HSTS_PRELOAD",
    default=False,
)

SECURE_CONTENT_TYPE_NOSNIFF = True

SECURE_REFERRER_POLICY = env(
    "SECURE_REFERRER_POLICY",
    default="same-origin",
)

SECURE_CROSS_ORIGIN_OPENER_POLICY = env(
    "SECURE_CROSS_ORIGIN_OPENER_POLICY",
    default="same-origin",
)

SESSION_COOKIE_SECURE = env.bool(
    "SESSION_COOKIE_SECURE",
    default=not DEBUG,
)

CSRF_COOKIE_SECURE = env.bool(
    "CSRF_COOKIE_SECURE",
    default=not DEBUG,
)

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = env.bool(
    "CSRF_COOKIE_HTTPONLY",
    default=False,
)

SESSION_COOKIE_SAMESITE = env(
    "SESSION_COOKIE_SAMESITE",
    default="Lax",
)

CSRF_COOKIE_SAMESITE = env(
    "CSRF_COOKIE_SAMESITE",
    default="Lax",
)

USE_X_FORWARDED_PROTO = env.bool(
    "USE_X_FORWARDED_PROTO",
    default=False,
)

SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if USE_X_FORWARDED_PROTO
    else None
)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)

if any(arg.startswith("test") for arg in sys.argv):
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=25)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL",
    default="noreply@localhost",
)


# ---------------------------------------------------------------------------
# Application URLs / invitations
# ---------------------------------------------------------------------------

FRONTEND_BASE_URL = env(
    "FRONTEND_BASE_URL",
    default="http://localhost:5173",
)

STAFF_INVITATION_EXPIRY_HOURS = env.int(
    "STAFF_INVITATION_EXPIRY_HOURS",
    default=48,
)

PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_PUBLIC_KEY = env("PAYSTACK_PUBLIC_KEY", default="")
PAYSTACK_BASE_URL = env("PAYSTACK_BASE_URL", default="https://api.paystack.co")
PAYSTACK_USER_AGENT = env(
    "PAYSTACK_USER_AGENT",
    default=(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/51.0.2704.103 Safari/537.36"
    ),
)
PAYSTACK_CALLBACK_URL = env(
    "PAYSTACK_CALLBACK_URL",
    default="http://localhost:5174/billing/callback",
)

PASSWORD_RESET_EXPIRY_HOURS = env.int(
    "PASSWORD_RESET_EXPIRY_HOURS",
    default=2,
)

if DEBUG and not PAYSTACK_SECRET_KEY:
    PAYSTACK_SECRET_KEY = "test_secret_key"
    PAYSTACK_PUBLIC_KEY = "pk_test_xxx"


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Africa/Lagos"

USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static / media
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = env("MEDIA_URL", default="/media/")
MEDIA_ROOT = BASE_DIR / "media"

MEDIA_STORAGE_BACKEND = env("MEDIA_STORAGE_BACKEND", default="filesystem").lower()
LOGO_STORAGE_IS_PERSISTENT = MEDIA_STORAGE_BACKEND == "s3"

if MEDIA_STORAGE_BACKEND == "s3":
    bucket_name = env("AWS_STORAGE_BUCKET_NAME", default="")
    access_key = env("AWS_ACCESS_KEY_ID", default="")
    secret_key = env("AWS_SECRET_ACCESS_KEY", default="")
    if not bucket_name:
        raise ImproperlyConfigured("AWS_STORAGE_BUCKET_NAME is required for S3 media storage.")
    if bool(access_key) != bool(secret_key):
        raise ImproperlyConfigured("Set both S3 access key variables, or neither when using workload identity.")

    s3_options = {
        "bucket_name": bucket_name,
        "region_name": env("AWS_S3_REGION_NAME", default="us-east-1"),
        "endpoint_url": env("AWS_S3_ENDPOINT_URL", default="") or None,
        "custom_domain": env("AWS_S3_CUSTOM_DOMAIN", default="") or None,
        "access_key": access_key or None,
        "secret_key": secret_key or None,
        "querystring_auth": False,
        "default_acl": None,
        "file_overwrite": False,
        "object_parameters": {"CacheControl": "public, max-age=31536000, immutable"},
    }
    STORAGES = {
        **global_settings.STORAGES,
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": s3_options,
        },
    }
elif MEDIA_STORAGE_BACKEND == "filesystem":
    STORAGES = {
        **global_settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
else:
    raise ImproperlyConfigured("MEDIA_STORAGE_BACKEND must be 'filesystem' or 's3'.")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"