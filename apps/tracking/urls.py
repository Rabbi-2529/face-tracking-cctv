from django.urls import path

from apps.tracking import accuracy_views, purge_views, report_views, review_views, views

app_name = "tracking"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("live/", views.LiveBoardView.as_view(), name="live_board"),
    # Zone-first search. Lower risk than person-first and answers most real
    # investigations, so it is the prominent entry point.
    path("search/zone/", views.ZoneSearchView.as_view(), name="zone_search"),
    path("employees/", views.EmployeeListView.as_view(), name="employee_list"),
    path(
        "employees/<uuid:employee_uuid>/timeline/",
        views.EmployeeTimelineView.as_view(),
        name="employee_timeline",
    ),
    path(
        "employees/<uuid:employee_uuid>/report/",
        views.report_correction,
        name="report_correction",
    ),
    # Review queues.
    path(
        "review/movements/",
        review_views.MovementReviewView.as_view(),
        name="movement_review",
    ),
    path(
        "review/movements/<int:pk>/decide/",
        review_views.MovementDecideView.as_view(),
        name="movement_decide",
    ),
    path(
        "review/corrections/",
        review_views.CorrectionQueueView.as_view(),
        name="correction_queue",
    ),
    path(
        "review/corrections/<int:pk>/decide/",
        review_views.CorrectionDecideView.as_view(),
        name="correction_decide",
    ),
    path("review/unknown/", review_views.UnknownFaceQueueView.as_view(), name="unknown_faces"),
    path(
        "review/unknown/<uuid:cluster_uuid>/resolve/",
        review_views.UnknownFaceResolveView.as_view(),
        name="unknown_resolve",
    ),
    # Pilot instrumentation: per-camera quality and the threshold what-if.
    path("accuracy/", accuracy_views.AccuracyView.as_view(), name="accuracy"),
    # Early deletion under dual authorisation (Super Admins).
    path("governance/purge/", purge_views.PurgeListView.as_view(), name="purge_list"),
    path("governance/purge/new/", purge_views.PurgeCreateView.as_view(), name="purge_create"),
    path(
        "governance/purge/<uuid:request_uuid>/",
        purge_views.PurgeDetailView.as_view(),
        name="purge_detail",
    ),
    # Reports and exports.
    path("reports/", report_views.ReportView.as_view(), name="reports"),
    path(
        "reports/export/<str:kind>/",
        report_views.ReportExportView.as_view(),
        name="report_export",
    ),
    path("audit/", views.AuditLogView.as_view(), name="audit_log"),
    path("audit/export/", report_views.AuditExportView.as_view(), name="audit_export"),
    # Imagery. Every endpoint re-checks permission and audits the fetch.
    # The detection pair is two steps on purpose: the first returns the
    # protected path, the second streams the bytes.
    path("snapshot/<int:detection_id>/url/", views.snapshot_url, name="snapshot_url"),
    path("snapshot/<int:detection_id>/file/", views.snapshot_file, name="snapshot_file"),
    path(
        "snapshot/interval/<int:interval_id>/<str:which>/",
        views.interval_snapshot,
        name="interval_snapshot",
    ),
    path(
        "snapshot/cluster/<uuid:cluster_uuid>/",
        views.cluster_snapshot,
        name="cluster_snapshot",
    ),
]
# Note: apps.watchlist.urls is included at the project level in config/urls.py,
# not here. Nesting one namespaced URLconf inside another would make its names
# resolve as `tracking:watchlist:*`, which is not what the templates expect.
