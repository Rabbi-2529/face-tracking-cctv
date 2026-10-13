from django.urls import path

from apps.alerts import views

app_name = "alerts"

urlpatterns = [
    path("", views.AlertInboxView.as_view(), name="inbox"),
    path("<uuid:alert_uuid>/action/", views.AlertActionView.as_view(), name="action"),
    path("<uuid:alert_uuid>/snapshot/", views.alert_snapshot, name="snapshot"),
    path("rules/", views.AlertRuleListView.as_view(), name="rules"),
    path("rules/new/", views.AlertRuleEditView.as_view(), name="rule_create"),
    path("rules/<int:pk>/edit/", views.AlertRuleEditView.as_view(), name="rule_edit"),
]
