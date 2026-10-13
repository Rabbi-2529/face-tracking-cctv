from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from apps.api import views

router = DefaultRouter()
router.register("zones", views.ZoneViewSet, basename="zone")
router.register("cameras", views.CameraViewSet, basename="camera")
router.register("employees", views.EmployeeViewSet, basename="employee")
router.register("presence", views.PresenceViewSet, basename="presence")
router.register(
    "tracking-requests", views.TrackingRequestViewSet, basename="trackingrequest"
)

urlpatterns = [
    path("", include(router.urls)),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
