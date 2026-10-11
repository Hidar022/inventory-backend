from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.exceptions import ValidationError

MAX_ORGANIZATION_LOGO_SIZE = 2 * 1024 * 1024
MAX_ORGANIZATION_LOGO_DIMENSION = 4096
MAX_ORGANIZATION_LOGO_PIXELS = 16_000_000
LOGO_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def validate_organization_logo(upload):
    if not settings.LOGO_STORAGE_IS_PERSISTENT and not settings.DEBUG:
        raise ValidationError(
            "Business logo uploads are unavailable until persistent media storage is configured."
        )

    if upload.size > MAX_ORGANIZATION_LOGO_SIZE:
        raise ValidationError("Logo must be 2 MB or smaller.")

    try:
        upload.seek(0)
        with Image.open(upload) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValidationError("Upload a valid PNG, JPEG, or WebP image.") from error
    finally:
        upload.seek(0)

    if image_format not in LOGO_EXTENSIONS:
        raise ValidationError("Upload a valid PNG, JPEG, or WebP image.")
    if (
        width > MAX_ORGANIZATION_LOGO_DIMENSION
        or height > MAX_ORGANIZATION_LOGO_DIMENSION
        or width * height > MAX_ORGANIZATION_LOGO_PIXELS
    ):
        raise ValidationError("Logo dimensions must not exceed 4096 by 4096 pixels.")

    upload.name = f"logo.{LOGO_EXTENSIONS[image_format]}"