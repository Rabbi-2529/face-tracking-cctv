from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("password/change/", views.PasswordChangeView.as_view(), name="password_change"),
    path("mfa/setup/", views.MFASetupView.as_view(), name="mfa_setup"),
    path("mfa/verify/", views.MFAVerifyView.as_view(), name="mfa_verify"),
    path("mfa/backup-codes/", views.BackupCodesView.as_view(), name="mfa_backup_codes"),
    path("locked/", views.LockoutView.as_view(), name="lockout"),
    path("me/", views.my_account, name="my_account"),
    path("me/elevation/", views.ElevationRequestView.as_view(), name="elevation_request"),
    path(
        "me/enrollments/<uuid:enrollment_uuid>/image/",
        views.own_enrollment_image,
        name="own_enrollment_image",
    ),
]
