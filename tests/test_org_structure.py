"""Departments and designations as managed lists for employee records."""

from __future__ import annotations

import json

from django.urls import reverse


def _audit(action):
    from apps.accounts.models import AuditLog

    return AuditLog.objects.filter(action=action)


def test_structure_page_lists_both(client, users, employees, org):
    client.force_login(users["hr"])
    response = client.get(reverse("employees:structure"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Engineering" in html and "Designations" in html
    assert response.context["can_designations"] is True
    assert response.context["can_departments"] is False


def test_hr_adds_designations_but_not_departments(client, users):
    from apps.org.models import Department, Designation

    client.force_login(users["hr"])
    client.post(reverse("employees:structure_create", args=["designation"]), {"title": "Site Engineer", "level": 2})
    assert Designation.objects.filter(title="Site Engineer").exists()
    assert _audit("designation.created").exists()

    response = client.post(
        reverse("employees:structure_create", args=["department"]), {"name": "Legal", "code": "LEG"}
    )
    assert response.status_code == 403
    assert not Department.objects.filter(code="LEG").exists()


def test_admin_adds_a_department(client, users):
    from apps.org.models import Department

    client.force_login(users["super_admin"])
    client.post(reverse("employees:structure_create", args=["department"]), {"name": "Legal", "code": "leg"})
    assert Department.objects.get(name="Legal").code == "LEG"


def test_duplicates_are_refused(client, users, org):
    from apps.org.models import Designation

    Designation.objects.create(title="Analyst")
    client.force_login(users["super_admin"])
    client.post(reverse("employees:structure_create", args=["designation"]), {"title": "  analyst ", "level": 0})
    assert Designation.objects.filter(title__iexact="analyst").count() == 1
    client.post(reverse("employees:structure_create", args=["department"]), {"name": "Other", "code": "ENG"})
    assert Designation.objects.count() == 1
    from apps.org.models import Department

    assert not Department.objects.filter(name="Other").exists()


def test_in_use_entries_cannot_be_deleted(client, users, employees, org):
    from apps.org.models import Department, Designation

    spare = Designation.objects.create(title="Spare")
    client.force_login(users["super_admin"])

    # Engineering has an employee and an HR user scoped to it.
    client.post(reverse("employees:structure_delete", args=["department", org["engineering"].pk]))
    assert Department.objects.filter(pk=org["engineering"].pk).exists()

    client.post(reverse("employees:structure_delete", args=["designation", spare.pk]))
    assert not Designation.objects.filter(pk=spare.pk).exists()
    assert _audit("designation.deleted").exists()


def test_quick_add_from_the_employee_form(client, users):
    from apps.org.models import Designation

    client.force_login(users["hr"])
    response = client.post(
        reverse("employees:structure_quick_add", args=["designation"]),
        data=json.dumps({"title": "Security Lead"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "Security Lead"
    assert Designation.objects.filter(pk=body["id"]).exists()

    refused = client.post(
        reverse("employees:structure_quick_add", args=["department"]),
        data=json.dumps({"name": "Legal", "code": "LEG"}),
        content_type="application/json",
    )
    assert refused.status_code == 403

    invalid = client.post(
        reverse("employees:structure_quick_add", args=["designation"]),
        data=json.dumps({"title": ""}),
        content_type="application/json",
    )
    assert invalid.status_code == 400


def test_employee_list_filters_by_department_and_designation(client, users, employees, org):
    from apps.org.models import Designation

    lead = Designation.objects.create(title="Lead")
    employees["eng"].designation = lead
    employees["eng"].save()

    client.force_login(users["super_admin"])
    url = reverse("tracking:employee_list")
    codes = lambda r: [e.employee_code for e in r.context["employees"]]  # noqa: E731

    assert codes(client.get(url, {"department": org["operations"].pk})) == ["OPS001"]
    assert codes(client.get(url, {"designation": lead.pk})) == ["ENG001"]
    assert codes(client.get(url, {"designation": "none"})) == ["OPS001"]


def test_hr_filter_offers_only_its_departments(client, users, employees, org):
    client.force_login(users["hr"])
    response = client.get(reverse("tracking:employee_list"))
    assert [d.name for d in response.context["departments"]] == ["Engineering"]
    # Filtering by a department outside scope returns nobody, never them.
    response = client.get(reverse("tracking:employee_list"), {"department": org["operations"].pk})
    assert list(response.context["employees"]) == []


def test_employee_form_offers_quick_add_by_permission(client, users):
    client.force_login(users["super_admin"])
    html = client.get(reverse("employees:create")).content.decode()
    assert 'data-target="department"' in html and 'data-target="designation"' in html

    client.force_login(users["hr"])
    html = client.get(reverse("employees:create")).content.decode()
    assert 'data-target="designation"' in html
    assert 'data-target="department"' not in html
