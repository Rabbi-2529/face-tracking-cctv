from django.urls import path

from apps.employees import record_views, views

app_name = "employees"

urlpatterns = [
    # Employee records and consent.
    path("new/", record_views.EmployeeCreateView.as_view(), name="create"),
    path("<uuid:employee_uuid>/", record_views.EmployeeSetupView.as_view(), name="setup"),
    path("<uuid:employee_uuid>/edit/", record_views.EmployeeEditView.as_view(), name="edit"),
    path("<uuid:employee_uuid>/consent/", record_views.ConsentView.as_view(), name="consent"),
    # Face enrolment.
    path("<uuid:employee_uuid>/enroll/", views.EnrollmentView.as_view(), name="enroll"),
    path(
        "<uuid:employee_uuid>/enroll/<uuid:enrollment_uuid>/deactivate/",
        views.EnrollmentDeactivateView.as_view(),
        name="enroll_deactivate",
    ),
    path(
        "<uuid:employee_uuid>/enroll/<uuid:enrollment_uuid>/conflict/",
        views.EnrollmentConflictView.as_view(),
        name="enroll_conflict",
    ),
]
