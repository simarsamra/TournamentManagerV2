import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

_DEV_SECRET_KEY = "django-insecure-change-me-in-production-x7k9m2p4q8r1s5t3u6v0w"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", _DEV_SECRET_KEY)

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

# Test Maker creates real user accounts, mass-registers users and rewrites live
# match scores. Off unless explicitly enabled; defaults on only in DEBUG.
ENABLE_TEST_MAKER = os.environ.get(
    "DJANGO_ENABLE_TEST_MAKER", "True" if DEBUG else "False"
).lower() in ("true", "1", "yes")

# Username prefix for accounts Test Maker creates, so its bulk actions can be
# confined to them instead of sweeping up real users.
TEST_MAKER_USER_PREFIX = os.environ.get("DJANGO_TEST_MAKER_PREFIX", "tm_")

# How many reverse proxies sit in front of the app. 0 means X-Forwarded-For is
# untrusted and REMOTE_ADDR is used directly. Setting this higher than the real
# proxy count lets clients spoof their recorded IP again.
TRUSTED_PROXY_COUNT = int(os.environ.get("DJANGO_TRUSTED_PROXY_COUNT", "0"))

# How far ahead the scheduler projects a court-availability row that has no end
# date, when the tournament has no usable end date either. Slot building is
# linear in this, so lowering it speeds up scheduling on large tournaments --
# but a tournament with sparse availability may then report "not enough court
# availability" where a longer horizon would have found slots further out.
OPEN_AVAILABILITY_HORIZON_DAYS = int(
    os.environ.get("DJANGO_OPEN_AVAILABILITY_DAYS", "365")
)

# The suite creates hundreds of users and logs them in, and PBKDF2 at Django's
# default work factor is by far the largest cost in it -- roughly 190 of the
# ~200 seconds a full run took before this. Swap in a fast hasher, but only for
# an actual `manage.py test` invocation.
#
# The guard is an exact match on the first argument. A WSGI or ASGI server
# never reaches this module through manage.py, so this cannot weaken password
# storage in a deployment; a management command whose name merely contains
# "test" will not trip it either.
RUNNING_TESTS = sys.argv[1:2] == ["test"]
if RUNNING_TESTS:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


if not DEBUG and SECRET_KEY == _DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a unique value when DEBUG is False. "
        "The fallback key is committed to this repository, so sessions and "
        "password-reset tokens signed with it are forgeable. Generate one with: "
        'python -c "from django.core.management.utils import get_random_secret_key; '
        'print(get_random_secret_key())"'
    )


def _env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


ALLOWED_HOSTS = _env_list(
    "DJANGO_ALLOWED_HOSTS",
    "127.0.0.1,localhost",
)

CSRF_TRUSTED_ORIGINS = _env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "http://127.0.0.1,http://localhost",
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "tournament_manager.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.notification_count",
                "core.context_processors.user_organizer_status",
                "core.context_processors.test_maker_enabled",
            ],
        },
    },
]

WSGI_APPLICATION = "tournament_manager.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Backups serialize auth.User including password hashes, so they default to a
# location outside the git working tree. Override with DJANGO_BACKUP_DIR.
# Login throttling counts attempts in the cache. LocMemCache is per-process and
# wiped on restart, which makes the limit worth 5 x worker_count in production.
CACHES = {
    "default": {
        "BACKEND": os.environ.get(
            "DJANGO_CACHE_BACKEND",
            "django.core.cache.backends.locmem.LocMemCache"
            if DEBUG
            else "django.core.cache.backends.db.DatabaseCache",
        ),
        "LOCATION": os.environ.get("DJANGO_CACHE_LOCATION", "tm_cache_table"),
    }
}

BACKUP_DIR = Path(
    os.environ.get(
        "DJANGO_BACKUP_DIR", BASE_DIR.parent / "tournament_manager_backups"
    )
)

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/login/"

SESSION_COOKIE_AGE = 86400
SESSION_SAVE_EVERY_REQUEST = True

if not DEBUG:
    # Honor original scheme when TLS is terminated by a reverse proxy (e.g., Nginx).
    # Only safe when that proxy always overwrites X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
    SECURE_SSL_REDIRECT = os.environ.get(
        "DJANGO_SECURE_SSL_REDIRECT", "True"
    ).lower() in ("true", "1", "yes")
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
