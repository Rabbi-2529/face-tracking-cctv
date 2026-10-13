"""
Camera configuration forms for the operator panel.

Two things here earn their keep:

* **A vendor-aware URL builder.** Nobody should hand-type
  ``/cam/realmonitor?channel=1&subtype=0`` and debug a typo against a camera
  they cannot see. Enter host, credentials and channel; the form builds the
  URL for the chosen vendor. Pasting a full URL is still allowed for odd
  devices.
* **An honest capability prompt.** The form asks for mount height and tilt,
  and warns when a camera is marked for identity work but its measured face
  size cannot support it. That mismatch is the single most common reason
  these systems disappoint.

The stored URL contains working credentials for a device on your network, so
it is written to an encrypted column and never rendered back into a page.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

from django import forms
from django.core.exceptions import ValidationError

from apps.cameras.models import Camera, CameraGroup, PipelineConfig
from apps.org.models import Zone

# Stream templates per vendor. {channel} is 1-based; {stream} is 0 for the
# main (full-resolution) stream and 1 for the sub stream.
STREAM_TEMPLATES = {
    Camera.Vendor.DAHUA: (
        "rtsp://{credentials}{host}:{port}/cam/realmonitor"
        "?channel={channel}&subtype={stream}"
    ),
    # CP Plus hardware is Dahua under the badge and uses the same path.
    Camera.Vendor.CP_PLUS: (
        "rtsp://{credentials}{host}:{port}/cam/realmonitor"
        "?channel={channel}&subtype={stream}"
    ),
    # Hikvision encodes channel and stream as a single number: 101 is
    # channel 1 main stream, 102 is channel 1 sub stream.
    Camera.Vendor.HIKVISION: (
        "rtsp://{credentials}{host}:{port}/Streaming/Channels/{hik_channel}"
    ),
    Camera.Vendor.AXIS: (
        "rtsp://{credentials}{host}:{port}/axis-media/media.amp"
        "?videocodec=h264&resolution={width}x{height}"
    ),
}

VENDOR_HELP = {
    Camera.Vendor.DAHUA: (
        "Dahua: create an Operator-level user under Setting &rarr; System &rarr; "
        "Account. Enable RTSP (port 554) and ONVIF, and turn OFF UPnP and P2P. "
        "Use the main stream for identity cameras."
    ),
    Camera.Vendor.CP_PLUS: (
        "CP Plus uses the Dahua stream path. Create a non-admin user and "
        "disable P2P."
    ),
    Camera.Vendor.HIKVISION: (
        "Hikvision: create an Operator user under Configuration &rarr; System "
        "&rarr; User Management. Channel 1 main stream is 101."
    ),
    Camera.Vendor.AXIS: "Axis: create a viewer account; RTSP is on by default.",
    Camera.Vendor.ONVIF: (
        "Generic ONVIF: paste the full RTSP URL from the device's own "
        "configuration page."
    ),
    Camera.Vendor.FILE: (
        "Video file, for testing. Give a path as the VISION container sees it "
        "(the project's ./samples directory is mounted at /samples)."
    ),
}


class CameraForm(forms.ModelForm):
    """Create or edit a camera, building the stream URL where possible."""

    class ConnectionMode(forms.ChoiceField):
        pass

    connection_mode = forms.ChoiceField(
        choices=[
            ("build", "Build the URL for me (recommended)"),
            ("raw", "I will paste the full stream URL"),
        ],
        initial="build",
        widget=forms.RadioSelect,
        label="Stream address",
    )

    # --- built from parts -------------------------------------------------
    host = forms.GenericIPAddressField(
        required=False,
        label="Camera IP address",
        help_text="Give every camera a static IP or a DHCP reservation.",
    )
    rtsp_port = forms.IntegerField(
        required=False, initial=554, min_value=1, max_value=65535, label="RTSP port"
    )
    username = forms.CharField(
        required=False,
        max_length=64,
        label="Service account username",
        help_text="A dedicated Operator-level account. Never the camera's admin login.",
    )
    password = forms.CharField(
        required=False,
        max_length=128,
        widget=forms.PasswordInput(render_value=False),
        label="Service account password",
        help_text="Stored encrypted. Leave blank when editing to keep the current one.",
    )
    channel = forms.IntegerField(
        required=False,
        initial=1,
        min_value=1,
        max_value=64,
        help_text="1 for a camera addressed directly; the channel number via an NVR.",
    )
    stream = forms.ChoiceField(
        required=False,
        choices=[
            ("0", "Main stream - full resolution (use for identity cameras)"),
            ("1", "Sub stream - low resolution (presence only)"),
        ],
        initial="0",
    )

    # --- pasted whole -----------------------------------------------------
    raw_url = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": "rtsp://user:pass@192.168.1.64:554/..."}
        ),
        label="Full stream URL",
        help_text="Including credentials. Stored encrypted.",
    )

    class Meta:
        model = Camera
        fields = [
            "name",
            "code",
            "zone",
            "group",
            "vendor",
            "purpose",
            "width",
            "height",
            "native_fps",
            "sample_fps",
            "mount_height_cm",
            "tilt_degrees",
            "calibration_notes",
            "is_enabled",
        ]
        widgets = {
            "calibration_notes": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "code": "Short stable identifier, e.g. 'cam-entry-01'. Used in logs.",
            "purpose": (
                "Identity cameras put a name to a face and must be at a doorway "
                "at face height. Presence cameras only contribute occupancy."
            ),
        }

    def __init__(self, *args, **kwargs):
        self.editing = kwargs.pop("editing", False)
        super().__init__(*args, **kwargs)

        # Only zones that may be monitored at all, so a camera cannot be
        # pointed at a toilet or changing room from this form.
        self.fields["zone"].queryset = Zone.objects.alive().filter(
            tracking_prohibited=False
        ).select_related("floor")
        self.fields["group"].queryset = CameraGroup.objects.all()
        self.fields["group"].required = False

        if self.editing:
            # Credentials are never rendered back; blank means "keep".
            self.fields["connection_mode"].initial = "raw"
            self.fields["raw_url"].required = False
            self.fields["raw_url"].help_text = (
                "Leave blank to keep the stored address unchanged. The current "
                "one is not shown because it contains a password."
            )
            self.fields["password"].help_text = (
                "Leave blank to keep the stored password."
            )

    # ------------------------------------------------------------ validation

    def clean_code(self) -> str:
        code = self.cleaned_data["code"].strip().lower()
        existing = Camera.objects.filter(code=code)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise ValidationError(f"A camera with code '{code}' already exists.")
        return code

    def clean(self):
        data = super().clean()
        mode = data.get("connection_mode")
        vendor = data.get("vendor")

        if vendor in {Camera.Vendor.ONVIF, Camera.Vendor.FILE} and mode == "build":
            raise ValidationError(
                {
                    "connection_mode": (
                        f"There is no standard stream path for "
                        f"'{Camera.Vendor(vendor).label}'. Paste the full URL "
                        "or path instead."
                    )
                }
            )

        url = ""
        if mode == "raw":
            raw = (data.get("raw_url") or "").strip()
            if raw:
                url = self._validate_raw(raw, vendor)
            elif not self.editing:
                raise ValidationError({"raw_url": "Give the stream URL."})
        else:
            url = self._build_url(data)

        data["_resolved_url"] = url

        # A camera in a prohibited zone must never be enabled. The model
        # enforces it too; catching it here gives a field-level message.
        zone = data.get("zone")
        if zone is not None and zone.tracking_prohibited and data.get("is_enabled"):
            raise ValidationError(
                {"zone": f"Zone '{zone.name}' has tracking prohibited."}
            )

        # Honesty check, a warning rather than an error - the operator may be
        # configuring a camera before it has been audited.
        if data.get("purpose") == Camera.Purpose.IDENTITY:
            measured = self.instance.measured_face_width_px
            if measured is not None and measured < 60:
                self.add_warning(
                    f"This camera is marked for identity work, but the audit "
                    f"measured faces at only {measured}px. It will not reliably "
                    "name anyone until it is repositioned."
                )
            tilt = data.get("tilt_degrees")
            if tilt is not None and tilt > 25:
                self.add_warning(
                    f"A downward tilt of {tilt:.0f} degrees foreshortens faces. "
                    "Identity cameras work best under 15-20 degrees."
                )
            height = data.get("mount_height_cm")
            if height is not None and height > 260:
                self.add_warning(
                    f"Mounted at {height}cm, this camera mostly sees the tops of "
                    "heads. Identity cameras belong at face height (180-220cm)."
                )

        return data

    def _validate_raw(self, raw: str, vendor: str) -> str:
        if vendor == Camera.Vendor.FILE:
            # A path, not a URL. The vision container has to be able to see it.
            if not raw.startswith("/"):
                raise ValidationError(
                    {
                        "raw_url": (
                            "Give an absolute path as the vision container sees "
                            "it, e.g. /samples/walk.mp4"
                        )
                    }
                )
            return raw

        parsed = urlparse(raw)
        if parsed.scheme not in {"rtsp", "rtsps", "http", "https"}:
            raise ValidationError(
                {"raw_url": "The URL must start with rtsp:// (or rtsps://)."}
            )
        if not parsed.hostname:
            raise ValidationError({"raw_url": "The URL has no host."})
        if not parsed.password:
            self.add_warning(
                "The URL carries no password. If the camera requires "
                "authentication, the stream will fail to open."
            )
        return raw

    def _build_url(self, data: dict) -> str:
        vendor = data.get("vendor")
        template = STREAM_TEMPLATES.get(vendor)
        if template is None:
            raise ValidationError(
                {"vendor": f"No stream template for {vendor}; paste a full URL."}
            )

        host = data.get("host")
        if not host:
            raise ValidationError({"host": "Give the camera IP address."})

        username = (data.get("username") or "").strip()
        password = data.get("password") or ""

        if self.editing and not password:
            # Keep the stored password: pull it back out of the existing URL
            # rather than asking the operator to retype it.
            password = self._existing_password()

        if username and not password:
            self.add_warning(
                "A username was given with no password. The stream will "
                "probably be refused."
            )

        credentials = ""
        if username:
            # Percent-encode: camera passwords frequently contain @ : / #,
            # any of which would otherwise break the URL.
            credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"

        stream = data.get("stream") or "0"
        channel = data.get("channel") or 1

        return template.format(
            credentials=credentials,
            host=host,
            port=data.get("rtsp_port") or 554,
            channel=channel,
            stream=stream,
            hik_channel=f"{channel}{int(stream) + 1:02d}",
            width=data.get("width") or 1920,
            height=data.get("height") or 1080,
        )

    def _existing_password(self) -> str:
        if not self.instance.pk:
            return ""
        try:
            return urlparse(self.instance.rtsp_url).password or ""
        except Exception:
            return ""

    # --------------------------------------------------------------- warnings

    def add_warning(self, message: str) -> None:
        """Non-blocking advice, surfaced next to the form.

        Deliberately separate from errors: a camera can be saved while being
        installed, and the operator should still be told what is wrong with
        its placement.
        """
        if not hasattr(self, "warnings"):
            self.warnings = []
        self.warnings.append(message)

    def get_warnings(self) -> list[str]:
        return getattr(self, "warnings", [])

    # ------------------------------------------------------------------ save

    def save(self, commit: bool = True) -> Camera:
        camera = super().save(commit=False)
        url = self.cleaned_data.get("_resolved_url")
        if url:
            camera.rtsp_url = url
        if self.cleaned_data.get("username"):
            camera.rtsp_username = self.cleaned_data["username"]
        if self.cleaned_data.get("password"):
            camera.rtsp_password = self.cleaned_data["password"]
        if self.cleaned_data.get("host"):
            camera.ip_address = self.cleaned_data["host"]
        if commit:
            camera.save()
        return camera


class PipelineConfigForm(forms.ModelForm):
    """Matching thresholds. Developer-only, and every change is attributed."""

    change_reason = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"placeholder": "Why this change, e.g. 'too many false matches at reception'"}
        ),
        help_text="Recorded with the change so a regression can be traced.",
    )

    class Meta:
        model = PipelineConfig
        fields = [
            "match_threshold_high",
            "match_threshold_low",
            "vote_window",
            "min_face_px",
            "min_quality_score",
            "presence_gap_seconds",
            "change_reason",
        ]

    def clean(self):
        data = super().clean()
        high = data.get("match_threshold_high")
        low = data.get("match_threshold_low")

        if high is not None and low is not None and low >= high:
            raise ValidationError(
                {"match_threshold_low": "Must sit below the confirmed threshold."}
            )
        if high is not None and high < 0.3:
            raise ValidationError(
                {
                    "match_threshold_high": (
                        "Below 0.30 this will confidently match the wrong people. "
                        "Tune against a labelled set from your own cameras before "
                        "going anywhere near this."
                    )
                }
            )
        vote_window = data.get("vote_window")
        if vote_window is not None and vote_window < 3:
            raise ValidationError(
                {
                    "vote_window": (
                        "Deciding identity from fewer than 3 frames is close to "
                        "per-frame matching, which is the main cause of wrong "
                        "names."
                    )
                }
            )
        return data
