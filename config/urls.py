"""
Root URL configuration.

Everything an operator needs is a purpose-built screen under this project's
own panel. Django's admin is **off by default**: it is a generic model editor
that cannot tell an installer their new camera only sees 42px faces, cannot
warn that an HR user with no department scope will see nobody, and would
happily expose raw biometric vectors in a change form.

Set ``DJANGO_ADMIN_ENABLED=1`` to mount it temporarily - useful only for
reaching a model this panel does not cover yet.
"""

from django.conf import settings
from django.urls import include, path

from apps.common.help import HelpView
from apps.common.views import healthz, readyz

urlpatterns = [
    # Container healthchecks. No auth, no DB write, cheap.
    path("healthz/", healthz, name="healthz"),
    path("readyz/", readyz, name="readyz"),
    path("accounts/", include("apps.accounts.urls")),
    path("help/", HelpView.as_view(), name="help"),
    path("api/v1/", include("apps.api.urls")),
    # Each of these is its own top-level namespace so templates can reverse
    # `watchlist:list`, `cameras:list` and so on.
    path("requests/", include("apps.watchlist.urls")),
    path("alerts/", include("apps.alerts.urls")),
    path("employees/manage/", include("apps.employees.urls")),
    path("cameras/", include("apps.cameras.urls")),
    path("settings/site/", include("apps.org.urls")),
    path("settings/users/", include("apps.accounts.admin_urls")),
    path("", include("apps.tracking.urls")),
]

if settings.DJANGO_ADMIN_ENABLED:
    from django.contrib import admin

    admin.site.site_header = "CCTV Tracking - raw model access"
    admin.site.site_title = "CCTV Tracking"
    admin.site.index_title = (
        "Direct model editing. Prefer the main panel, which carries the "
        "validation and the warnings this does not."
    )
    urlpatterns.insert(0, path("django-admin/", admin.site.urls))

if settings.DEBUG:
    try:
        import debug_toolbar  # noqa: F401
    except ImportError:
        pass
    else:
        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
