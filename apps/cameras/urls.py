from django.urls import path

from apps.cameras import views

app_name = "cameras"

urlpatterns = [
    path("", views.CameraListView.as_view(), name="list"),
    path("new/", views.CameraEditView.as_view(), name="create"),
    path("pipeline/", views.PipelineConfigView.as_view(), name="pipeline"),
    path("<slug:code>/", views.CameraDetailView.as_view(), name="detail"),
    path("<slug:code>/edit/", views.CameraEditView.as_view(), name="edit"),
    path("<slug:code>/toggle/", views.CameraToggleView.as_view(), name="toggle"),
    path("<slug:code>/test/", views.CameraTestView.as_view(), name="test"),
    path("<slug:code>/preview.jpg", views.CameraPreviewView.as_view(), name="preview"),
    path("<slug:code>/watch/", views.CameraWatchView.as_view(), name="watch"),
]
