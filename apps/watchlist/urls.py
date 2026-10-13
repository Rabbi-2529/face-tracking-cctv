from django.urls import path

from apps.watchlist import views

app_name = "watchlist"

urlpatterns = [
    path("", views.TrackingRequestListView.as_view(), name="list"),
    path("new/", views.TrackingRequestCreateView.as_view(), name="create"),
    path(
        "<uuid:request_uuid>/decide/",
        views.TrackingRequestDecideView.as_view(),
        name="decide",
    ),
    path(
        "<uuid:request_uuid>/follow/",
        views.TrackingFollowView.as_view(),
        name="follow",
    ),
]
