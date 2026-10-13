from django.urls import path

from apps.org import views

app_name = "org"

urlpatterns = [
    path("", views.SiteOverview.as_view(), name="overview"),
    # Specific floor-plan routes before the generic <kind>/<identifier> ones.
    path("floor/<int:pk>/plan/", views.FloorPlanEditorView.as_view(), name="floor_plan"),
    path(
        "floor/<int:pk>/plan/image/",
        views.FloorPlanImageView.as_view(),
        name="floor_plan_image",
    ),
    path("<str:kind>/new/", views.ModelEditView.as_view(), name="create"),
    path("<str:kind>/<str:identifier>/edit/", views.ModelEditView.as_view(), name="edit"),
    path(
        "adjacency/<int:identifier>/delete/",
        views.AdjacencyDeleteView.as_view(),
        name="adjacency_delete",
    ),
]
