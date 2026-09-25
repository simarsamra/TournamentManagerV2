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

# Database.
#
# SQLite remains the default so an existing checkout keeps working with no
# configuration. Set DJANGO_DB_ENGINE=postgresql (or give a DATABASE_URL) to
# run on PostgreSQL, which is what a deployment with concurrent writers wants.
#
# Note the direction of the risk. SQLite was not protecting concurrency here by
# design; it was doing so by accident, locking whole tables so that a second
# concurrent writer failed loudly. PostgreSQL commits both, so check-then-act
# code that looked safe on SQLite can silently corrupt on PostgreSQL. The
# registration and reschedule paths take explicit row locks for exactly this
# reason -- see core/views/helpers._claim_participant_slot.
#
# DATABASE_URL wins when set, so a platform that injects one (Heroku, Render,
# Fly, docker-compose) needs nothing else.

def _database_from_url(url):
    """Parse postgres://user:password@host:port/name into Django's format."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql", "postgresql+psycopg2"):
        raise ImproperlyConfigured(
            f"DATABASE_URL must be a postgres:// URL, got {parsed.scheme!r}://"
        )
    name = parsed.path.lstrip("/")
    if not name:
        raise ImproperlyConfigured("DATABASE_URL is missing a database name")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": int(os.environ.get("DJANGO_CONN_MAX_AGE", "60")),
    }


_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_DB_ENGINE = os.environ.get("DJANGO_DB_ENGINE", "sqlite3").strip().lower()

if _DATABASE_URL:
    DATABASES = {"default": _database_from_url(_DATABASE_URL)}
elif _DB_ENGINE in ("postgresql", "postgres", "psql"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DJANGO_DB_NAME", "tournament_manager"),
            "USER": os.environ.get("DJANGO_DB_USER", "tournament_manager"),
            "PASSWORD": os.environ.get("DJANGO_DB_PASSWORD", ""),
            "HOST": os.environ.get("DJANGO_DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DJANGO_DB_PORT", "5432"),
            # Reuse connections between requests; 0 opens a new one each time.
            "CONN_MAX_AGE": int(os.environ.get("DJANGO_CONN_MAX_AGE", "60")),
        }
    }
elif _DB_ENGINE in ("sqlite3", "sqlite"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("DJANGO_DB_NAME", BASE_DIR / "db.sqlite3"),
        }
    }
else:
    raise ImproperlyConfigured(
        f"DJANGO_DB_ENGINE must be 'sqlite3' or 'postgresql', got {_DB_ENGINE!r}"
    )

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

# Login throttling counts attempts in the cache. LocMemCache is per-process and
# wiped on restart, which makes the limit worth 5 x worker_count in production.
# DatabaseCache needs `manage.py createcachetable`; without it the throttle
# helpers fail open rather than locking everybody out.
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

# Backups serialize auth.User including password hashes, so they default to a
# location outside the git working tree. Override with DJANGO_BACKUP_DIR.
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


# AI analytics: questions about a tournament answered by a local model served
# by Ollama (AI_ANALYTICS_PLAN.md). Off unless enabled; `manage.py ai_doctor`
# checks the configuration against the running Ollama.
def _env_bool(name, default):
    return os.environ.get(name, "True" if default else "False").lower() in ("true", "1", "yes")


AI_ANALYTICS_ENABLED = _env_bool("DJANGO_AI_ANALYTICS_ENABLED", False)
# Loopback by default: Ollama has no authentication, so it should never listen
# on a public interface (OLLAMA_HOST=127.0.0.1:11434 is Ollama's own default).
OLLAMA_URL = os.environ.get("DJANGO_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
# Default sized for a 12 GB GPU (D-1); compare alternatives with `ai_eval`.
OLLAMA_MODEL = os.environ.get("DJANGO_OLLAMA_MODEL", "qwen3.5:9b").strip()
# Sent as Ollama's `think` flag. "false" skips the hidden reasoning pass on
# thinking models such as Qwen 3.5 (much faster); leave empty for models that
# don't support the flag.
_ollama_think = os.environ.get("DJANGO_OLLAMA_THINK", "false").strip().lower()
OLLAMA_THINK = None if _ollama_think == "" else _ollama_think in ("true", "1", "yes")
OLLAMA_NUM_CTX = int(os.environ.get("DJANGO_OLLAMA_NUM_CTX", "8192"))
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("DJANGO_OLLAMA_TIMEOUT_SECONDS", "60"))
OLLAMA_KEEP_ALIVE = os.environ.get("DJANGO_OLLAMA_KEEP_ALIVE", "30m")
# Who may ask: "managers" (tournament managers only) or "all" (everyone who
# can open the tournament's analytics).
AI_ANALYTICS_AUDIENCE = os.environ.get("DJANGO_AI_ANALYTICS_AUDIENCE", "managers").strip().lower()
if AI_ANALYTICS_AUDIENCE not in ("managers", "all"):
    raise ImproperlyConfigured(
        f"DJANGO_AI_ANALYTICS_AUDIENCE must be 'managers' or 'all', got {AI_ANALYTICS_AUDIENCE!r}"
    )
AI_QUESTIONS_PER_USER_PER_HOUR = int(os.environ.get("DJANGO_AI_QUESTIONS_PER_USER_PER_HOUR", "60"))
AI_MAX_PENDING = int(os.environ.get("DJANGO_AI_MAX_PENDING", "20"))
AI_MAX_QUESTION_CHARS = int(os.environ.get("DJANGO_AI_MAX_QUESTION_CHARS", "300"))
AI_JOB_STALE_SECONDS = int(os.environ.get("DJANGO_AI_JOB_STALE_SECONDS", "600"))
AI_RETENTION_DAYS = int(os.environ.get("DJANGO_AI_RETENTION_DAYS", "30"))
# Written explanations (AI-7, D-5). When off, answers show the routed card only.
AI_EXPLANATIONS_ENABLED = _env_bool("DJANGO_AI_EXPLANATIONS", True)
# Conversational answers from the whole tournament (core/ai/conversation.py).
# When off, or when a tournament is too big for the snapshot, questions are
# routed to one card as before.
AI_CONVERSATION_ENABLED = _env_bool("DJANGO_AI_CONVERSATION", True)
# How many earlier questions and answers a follow-up is sent with.
AI_CONVERSATION_TURNS = int(os.environ.get("DJANGO_AI_CONVERSATION_TURNS", "3"))
# The tournament news board on every dashboard (ai/recap.py): the worker
# writes one update per tournament when new results are in, at most once per
# interval. Off = recaps are only written when an organizer asks.
AI_NEWS_AUTO = _env_bool("DJANGO_AI_NEWS_AUTO", True)
AI_NEWS_INTERVAL_MINUTES = int(os.environ.get("DJANGO_AI_NEWS_INTERVAL_MINUTES", "30"))

# The AI worker's log (routing problems, hidden explanations, model errors)
# goes to stderr, which systemd captures: journalctl -u tm-ai-worker.
# Django's own logging defaults are left in place.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"stderr": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "core.ai": {
            "handlers": ["stderr"],
            "level": "WARNING" if RUNNING_TESTS else os.environ.get("DJANGO_AI_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

# Tests must never reach a real model; see core.test_runner.
TEST_RUNNER = "core.test_runner.NoNetworkTestRunner"
