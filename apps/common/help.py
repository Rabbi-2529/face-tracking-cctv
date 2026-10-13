"""
In-app training: one short guide per role.

Kept as data here rather than prose in a template, so each guide is a list
of concrete steps naming the screens as they appear in the sidebar. Every
signed-in user can read every guide - knowing what a colleague can and
cannot do is part of trusting the system - with their own role open first.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from apps.common.roles import Role

EVERYONE = {
    "title": "Your own account",
    "steps": [
        "<b>My account</b> shows your role, what you can access, your sign-in history, and - if you are an employee - your consent, your enrolled face images and any tracking requests about you.",
        "If you have an authenticator: after your password, enter the six-digit code. Lost your phone? Use a backup code. Issue new backup codes from <b>My account</b>.",
        "Open <b>My own movement history</b> from My account to see what the system recorded about you. If an entry is wrong, use <i>Something wrong here?</i> on that page.",
    ],
}

GUIDES = [
    {
        "role": Role.SUPER_ADMIN,
        "summary": "Runs the system: people, access, approvals, configuration.",
        "sections": [
            {"title": "Daily", "steps": [
                "<b>Dashboard</b>: check open alerts, pending tracking requests and break-glass requests.",
                "<b>Tracking requests</b>: read the reason, then approve (choose the hours) or reject with a note. You cannot approve your own request. Revoke an approval the moment it is no longer needed - the requester's live view closes at once.",
                "<b>Alerts</b>: acknowledge what someone is handling; resolve with a note saying what was done.",
                "<b>Review queues</b>: decide implausible movements (real, or a bad match), answer correction reports (a rejection needs a reason), label unknown faces.",
            ]},
            {"title": "People and access", "steps": [
                "<b>Users &amp; roles</b> &rarr; <i>Add user</i>: choose the role. HR users must be given departments, and Security Operators zones - an empty scope means they see nothing.",
                "A new user gets a temporary password shown once; give it to them in person. They change it at first sign-in.",
                "Lost authenticator: open the user, <i>Reset authenticator</i>, with a reason. You cannot reset your own.",
                "Break-glass requests from Developers appear on Users &amp; roles. Approve for the shortest time that works (maximum 4 hours).",
                "<b>Departments &amp; titles</b>: only you add departments, because they decide which employees each HR user can see.",
                "<b>Data purge</b>: for an erasure request or data captured in error. Request it with a reason; a <i>different</i> Super Admin must approve (typing DELETE). Nothing is deleted until then, and the audit log is never purged.",
            ]},
            {"title": "Configuration", "steps": [
                "<b>Cameras</b> &rarr; <i>Add camera</i>: choose the vendor and let the form build the stream address. After saving, use <i>Test connection</i> and check the face size (60 px minimum, 100+ good).",
                "<b>Cameras &rarr; Vendors</b>: add a camera make and its stream address template.",
                "<b>Site &amp; zones</b>: zones, doors between them, floor plans. Mark washrooms and prayer or medical rooms <i>never tracked</i>.",
                "<b>Alert rules</b>: who may enter each restricted zone, the after-hours window, unknown faces, camera outages.",
            ]},
            {"title": "Before going live", "steps": [
                "Run <code>make golive-check</code> and fix every FAIL. Run <code>make backup</code> daily and <code>make restore-test</code> after the first one.",
            ]},
        ],
        "cannot": "change matching thresholds or read raw face templates (Developer), and approve their own requests.",
    },
    {
        "role": Role.SECURITY_ADMIN,
        "summary": "Security operations: live monitoring, alerts, approvals, cameras.",
        "sections": [
            {"title": "Monitoring", "steps": [
                "<b>Live board</b>: occupancy per room, drawn on the floor plan. A red room marks a camera outage - a gap, not an empty room.",
                "<b>Alerts</b>: work the inbox. Use <i>Show evidence</i> to see the image; every image you open is logged.",
                "<b>Zone search</b>: who was in a room during a time window. Prefer this to following a named person.",
            ]},
            {"title": "Approvals and review", "steps": [
                "<b>Tracking requests</b>: approve or reject with a note; revoke when the need ends.",
                "<b>Review queues</b>: decide implausible movements and unknown faces.",
            ]},
        ],
        "cannot": "manage users, change matching thresholds, or purge data.",
    },
    {
        "role": Role.HR,
        "summary": "Employee records, consent, enrolment, and requests.",
        "sections": [
            {"title": "Adding a person (three steps, in order)", "steps": [
                "<b>Employees</b> &rarr; <i>Add employee</i>: code, name, department, designation. Missing designation? Use <i>+ New</i> beside it.",
                "<b>Consent</b>: record it with the signed notice attached. Without consent the person is never matched, however many photos you add.",
                "<b>Enrol faces</b>: 5 or more photos - one straight on, two turned slightly left and right, with and without glasses. One face per photo, good light, no strong light behind them. The page tells you exactly what is wrong with a rejected photo.",
            ]},
            {"title": "Looking at movement", "steps": [
                "You can open timelines for your own departments. For anyone else, the page offers a <b>tracking request</b>: give a real reason and the dates you need. An approver decides; the access ends by itself.",
                "<b>Reports</b>: daily headcount and space use. Small numbers show as \"&lt;3\" because they would identify people.",
            ]},
            {"title": "When someone withdraws consent", "steps": [
                "Open the employee, <i>Consent</i>, withdraw it with a note. Their face images stop being used within about 30 seconds.",
            ]},
        ],
        "cannot": "see live cameras, follow anyone without an approved request, see evidence images, or add departments.",
    },
    {
        "role": Role.SECURITY_OPERATOR,
        "summary": "Watches assigned zones and responds to alerts.",
        "sections": [
            {"title": "On shift", "steps": [
                "<b>Live board</b>: your assigned rooms only. Counts update by themselves.",
                "<b>Alerts</b>: <i>Acknowledge</i> when you start dealing with one; <i>Resolve</i> with a note when done. Names are not shown to your role - the alert tells you what and where.",
                "Evidence images are available for recent alerts only (about the last 15 minutes).",
            ]},
        ],
        "cannot": "see movement history, names on alerts, employees outside your zones, or older images.",
    },
    {
        "role": Role.AUDITOR,
        "summary": "Checks that access is used properly.",
        "sections": [
            {"title": "Audit", "steps": [
                "<b>Audit log</b>: every view of identity data, every approval, export and setting change. Filter by action (for example <code>history.</code> or <code>snapshot.</code>) or show denials only.",
                "Patterns worth questioning: many denials from one person, approvals the requester then never used, exports outside working hours, repeated break-glass.",
                "<i>Export CSV</i> exports the filtered log; the export itself is logged.",
            ]},
        ],
        "cannot": "see movement, cameras, or employee records.",
    },
    {
        "role": Role.DEVELOPER,
        "summary": "Keeps recognition accurate and the pipeline healthy.",
        "sections": [
            {"title": "Accuracy", "steps": [
                "<b>Accuracy</b>: pilot readiness, camera quality (gate rate over 50% means move the camera), zone errors, and the threshold what-if.",
                "Change thresholds on <b>Matching</b> only with a reason, after checking the what-if against accepted corrections and a benchmark from real cameras (<code>make benchmark</code>).",
            ]},
            {"title": "Cameras and pipeline", "steps": [
                "<b>Cameras</b>: add and test cameras; <b>Cameras &rarr; Vendors</b> for new makes.",
                "After any update with migrations: <code>make migrate &amp;&amp; make restart</code> - every Django service, not just web.",
                "Capacity: <code>make loadtest cams=N</code> on a non-production stack.",
            ]},
            {"title": "Production identity access", "steps": [
                "In production you do not see real identities by default. If a fault needs them, request <b>break-glass</b> from My account with a ticket reference; a Super Admin approves it for a few hours.",
            ]},
        ],
        "cannot": "approve tracking requests, manage users, or purge data - even with break-glass.",
    },
    {
        "role": Role.EMPLOYEE,
        "summary": "Sees what the system holds about them.",
        "sections": [
            {"title": "Your record", "steps": [
                "<b>My account</b>: your consent, your enrolled photos, and tracking requests about you (unless an approver formally recorded that telling you would harm an active investigation).",
                "Your movement history: wrong entry? Report it on the timeline. Reports are reviewed and answered.",
                "To withdraw consent, ask HR. Matching stops within about 30 seconds of it being recorded.",
            ]},
        ],
        "cannot": "see anyone else's information.",
    },
]


class HelpView(LoginRequiredMixin, TemplateView):
    template_name = "help/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        current = self.request.user.role
        guides = []
        for guide in GUIDES:
            guides.append({
                **guide,
                "label": Role(guide["role"]).label,
                "sections": guide["sections"] + [EVERYONE],
            })
        guides.sort(key=lambda g: g["role"] != current)
        ctx["guides"] = guides
        ctx["current_role"] = current
        return ctx
