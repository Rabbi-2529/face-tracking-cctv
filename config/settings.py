"""
Django settings for the CCTV employee movement tracking system.

Single settings module driven by environment variables (12-factor), so the
same image runs in dev and production with only the environment changing.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    DJANGO_CSRF_TRUSTED_ORIGINS=(list, ["http://localhost:8000"]),
    SNAPSHOT_URL_TTL_SECONDS=(int, 180),
    PRESENCE_GAP_SECONDS=(int, 60),
    MATCH_THRESHOLD_HIGH=(float, 0.45),
    MATCH_THRESHOLD_LOW=(float, 0.30),
    RETENTION_DETECTION_DAYS=(int, 14),
    RETENTION_SNAPSHOT_DAYS=(int, 30),
    RETENTION_PRESENCE_DAYS=(int, 180),
    RETENTION_AUDIT_DAYS=(int, 730),
)

# .env is read only when present; in Docker the environment is already set.
if (BASE_DIR / ".env").exists():
    env.read_env(BASE_DIR / ".env")

DEBUG = env("DJANGO_DEBUG")
SECRET_KEY = env("DJANGO_SECRET_KEY")

# development | staging | production
# Outside production, Developers get employee/enrollment/live-follow access
# directly; in production those three need a break-glass AccessElevation.
# See apps/common/roles.py: DEVELOPER_NONPROD_EXTRA.
ENVIRONMENT = env("ENVIRONMENT", default="development" if DEBUG else "production")

# Django's admin is a generic model editor. It cannot tell an installer that
# their new camera only sees 42px faces, cannot warn that an HR user with no
# department scope will see nobody, and would happily render a raw biometric
# vector in a change form. Every operational task has a purpose-built screen
# in this project's own panel, so the admin stays off unless explicitly
# enabled to reach something the panel does not cover yet.
DJANGO_ADMIN_ENABLED = env.bool("DJANGO_ADMIN_ENABLED", default=False)

ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("DJANGO_CSRF_TRUSTED_ORIGINS")

# ---------------------------------------------------------------- apps

# daphne must precede django.contrib.staticfiles so runserver hands off to
# the ASGI server rather than the WSGI one (checked by daphne.E001).
ASGI_APPS = ["daphne"]

DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "channels",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "django_celery_beat",
    "axes",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
]

LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.org",
    "apps.employees",
    "apps.cameras",
    "apps.tracking",
    "apps.watchlist",
    "apps.alerts",
    "apps.api",
]

INSTALLED_APPS = ASGI_APPS + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

if DJANGO_ADMIN_ENABLED:
    # Inserted before staticfiles so the admin's own templates resolve.
    INSTALLED_APPS.insert(
        INSTALLED_APPS.index("django.contrib.staticfiles"), "django.contrib.admin"
    )

# ---------------------------------------------------------- middleware

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # OTP must sit directly after authentication so request.user.is_verified()
    # is available to views and permission checks.
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Captures actor / IP / user-agent into a context var so the service layer
    # can write a complete AuditLog row without threading `request` everywhere.
    "apps.common.audit.AuditContextMiddleware",
    # Axes last: it needs the authenticated user and the resolved view.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

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
                "apps.common.context.role_context",
            ],
        },
    },
]

# ------------------------------------------------------------ database

DATABASES = {
    "default": {
        **env.db("DATABASE_URL"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": 10,
            "application_name": "cctv-web",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------- cache / redis

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "cctv",
    }
}

SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# ---------------------------------------------------------- channels

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [env("CHANNELS_REDIS_URL", default="redis://redis:6379/2")],
            "capacity": 2000,
            "expiry": 30,
        },
    }
}

# ------------------------------------------------------------- celery

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = None  # fire-and-forget; results are written to models
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = env("TZ", default="Asia/Kolkata")
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# ------------------------------------------------------ auth / security

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # AxesStandaloneBackend must be first so lockouts are enforced before
    # any password is checked.
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# Identity-bearing screens should not stay open on an unattended desk.
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# Roles for which TOTP enrolment is mandatory. Enforced by
# apps.common.permissions.PermissionService and the MFARequiredMixin.
MFA_ENFORCED_ROLES = ["developer", "super_admin", "security_admin", "hr"]
MFA_ENFORCED = env.bool("MFA_ENFORCED", default=not DEBUG)

# django-axes: brute-force protection
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hours
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "accounts/lockout.html"

# Transport / browser hardening. Relaxed in DEBUG so http://localhost works.
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Encrypts Camera.rtsp_url and camera credentials at rest.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY")

# ---------------------------------------------------------------- DRF

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Deny by default. Every viewset opts in with an explicit permission class.
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.api.permissions.DenyAll",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # Bulk identity lookups and exports are the misuse vectors worth
        # rate-limiting even for authorised users.
        "identity_lookup": "120/minute",
        "export": "10/hour",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "CCTV Employee Movement Tracking API",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ------------------------------------------------- static / media / S3

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

S3_ENDPOINT_URL = env("S3_ENDPOINT_URL", default="")
S3_PUBLIC_ENDPOINT_URL = env("S3_PUBLIC_ENDPOINT_URL", default=S3_ENDPOINT_URL)
S3_BUCKET = env("S3_BUCKET", default="cctv-snapshots")
S3_ACCESS_KEY = env("S3_ACCESS_KEY", default="")
S3_SECRET_KEY = env("S3_SECRET_KEY", default="")

SNAPSHOT_URL_TTL_SECONDS = env("SNAPSHOT_URL_TTL_SECONDS")

# Object storage is optional. With S3_* configured, snapshots go to a private
# bucket; otherwise they go to a local volume. Either way they are served
# ONLY through the permission-checked streaming view in apps/tracking/views.py
# (`snapshot_file`), never from a browsable path and never from a URL that
# could be forwarded. Streaming rather than signing costs a little bandwidth
# and buys one audit point and one permission check for every image viewed -
# the right trade for biometric evidence.
USE_S3 = bool(S3_ENDPOINT_URL and S3_ACCESS_KEY and S3_SECRET_KEY)

if USE_S3:
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": S3_BUCKET,
                "endpoint_url": S3_ENDPOINT_URL,
                "access_key": S3_ACCESS_KEY,
                "secret_key": S3_SECRET_KEY,
                "default_acl": None,
                "querystring_auth": True,
                "querystring_expire": SNAPSHOT_URL_TTL_SECONDS,
                "file_overwrite": False,
                "signature_version": "s3v4",
                "addressing_style": "path",
            },
        },
    }
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {
                "location": str(MEDIA_ROOT),
                # No base_url: nothing may construct a directly fetchable
                # link to a snapshot.
                "base_url": None,
            },
        },
    }

STORAGES["staticfiles"] = {
    "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
}

# ------------------------------------------------------------- pipeline

EVENT_STREAM = env("EVENT_STREAM", default="events:detections")
EVENT_STREAM_GROUP = env("EVENT_STREAM_GROUP", default="aggregators")

# A person must be unseen in a zone for this long before the presence
# interval is closed and a movement event can be emitted.
PRESENCE_GAP_SECONDS = env("PRESENCE_GAP_SECONDS")

# Accept above HIGH, reject below LOW, mark "probable" in between.
# Probable matches are never used to raise an alert on their own.
MATCH_THRESHOLD_HIGH = env("MATCH_THRESHOLD_HIGH")
MATCH_THRESHOLD_LOW = env("MATCH_THRESHOLD_LOW")

RETENTION = {
    "detection_days": env("RETENTION_DETECTION_DAYS"),
    "snapshot_days": env("RETENTION_SNAPSHOT_DAYS"),
    "presence_days": env("RETENTION_PRESENCE_DAYS"),
    "audit_days": env("RETENTION_AUDIT_DAYS"),
}

# ------------------------------------------------------------- i18n / tz

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TZ", default="Asia/Kolkata")
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------- logging

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {process:d} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        # Never let an audit write fail silently.
        "cctv.audit": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "cctv.aggregator": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "cctv.alerts": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# debug_toolbar ships in requirements/dev.txt only, so enable it only when it
# is actually installed - otherwise the production image fails to boot with
# DJANGO_DEBUG=1.
if DEBUG:
    try:
        import debug_toolbar  # noqa: F401
    except ImportError:
        pass
    else:
        INSTALLED_APPS += ["debug_toolbar"]
        MIDDLEWARE.insert(1, "debug_toolbar.middleware.DebugToolbarMiddleware")
        INTERNAL_IPS = ["127.0.0.1"]

        def _show_toolbar(request) -> bool:
            """Enable the toolbar, except on streaming endpoints.

            The toolbar buffers responses to inject its panel, which silently
            breaks an MJPEG stream: the live camera view produces frames
            forever and the browser receives nothing. Skip anything that
            streams.
            """
            if request.path.endswith("/live/") or "/snapshot/" in request.path:
                return False
            return DEBUG

        # In Docker the request IP is the bridge gateway, not 127.0.0.1, so
        # decide from the DEBUG flag rather than INTERNAL_IPS.
        DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": _show_toolbar}
