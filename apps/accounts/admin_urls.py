from django.urls import path

from apps.accounts import admin_views

app_name = "useradmin"

urlpatterns = [
    path("", admin_views.UserListView.as_view(), name="list"),
    path("new/", admin_views.UserEditView.as_view(), name="create"),
    path("<uuid:user_uuid>/edit/", admin_views.UserEditView.as_view(), name="edit"),
    path("<uuid:user_uuid>/toggle/", admin_views.UserToggleView.as_view(), name="toggle"),
    path(
        "<uuid:user_uuid>/mfa-reset/",
        admin_views.UserMFAResetView.as_view(),
        name="mfa_reset",
    ),
    path(
        "elevation/<uuid:elevation_uuid>/decide/",
        admin_views.ElevationDecideView.as_view(),
        name="elevation_decide",
    ),
]
