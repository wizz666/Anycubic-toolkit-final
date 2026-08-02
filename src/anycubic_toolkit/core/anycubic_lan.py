"""Anycubic LAN mode: local printer connection on stock firmware.

Newer Anycubic printers (the "avata" generation, e.g. the Kobra X) expose a
**local** control API when LAN mode is enabled in the printer settings — no
cloud, no account, no custom firmware required. The protocol, documented by
the open-source community (notably the MIT-licensed ``stribor/anycubic_kobrax``
Home Assistant integration), has two stages:

1. **Provisioning** over plain HTTP on port 18910: ``GET /info`` returns a
   token and a control URL; a signed ``POST`` to that URL returns an
   AES-128-CBC encrypted bundle containing the printer's local MQTT username,
   password and client certificate. Signing uses MD5 (stdlib) and decryption
   uses :mod:`anycubic_toolkit.core.aes`.
2. **Status** over MQTT/TLS on port 9883: the app connects with the bundle's
   credentials, subscribes to the printer's report topics and publishes small
   query commands; the printer answers with JSON reports (state, temperatures,
   progress, layers, ACE/multi-color-box, light and camera state, …). The same
   session can optionally publish one-off commands (start/stop the camera,
   set the chamber light) alongside the read-only queries.

Everything stays on the local network. Credentials are cached in the app
configuration so provisioning happens once per printer.

The connection itself does not hardcode a model: the printer reports its own
``modelId``/``modelName`` during provisioning, so any Anycubic printer using
this same "avata" LAN protocol should work here even if it isn't in the
toolkit's static model catalog yet (only the Kobra X and Kobra S1 have been
tested against real hardware for this project so far).

MQTT requires the ``paho-mqtt`` package; if it's missing the client reports a
clear error instead of crashing (provisioning itself is pure stdlib).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import string
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from anycubic_toolkit import __app_name__, __version__
from anycubic_toolkit.core.aes import aes128_cbc_decrypt, pkcs7_unpad

CTRL_HTTP_PORT = 18910
MQTT_PORT = 9883
STREAM_HTTP_PORT = 18088
TOPIC_BASE = "anycubic/anycubicCloud/v1"
_HTTP_TIMEOUT = 10
_MQTT_CONNECT_TIMEOUT = 10
_REPORT_COLLECT_SECONDS = 6.0
_USER_AGENT = f"{__app_name__}/{__version__}"
_NONCE_CHARS = string.ascii_letters + string.digits
_LIVE_URL_RE = re.compile(r"https?://[^\s\"']+/live/[^\s\"']+")

# Queries published after connecting; the printer answers on its report topics.
_QUERY_SPECS: tuple[tuple[str, str, str], ...] = (
    ("web", "status", "query"),
    ("web", "info", "query"),
    ("web", "tempature", "query"),  # (sic) — the printer's own spelling
    ("web", "fan", "query"),
    ("web", "print", "query"),
)


class LanError(Exception):
    """Base error for LAN mode failures."""


class LanUnavailable(LanError):
    """The printer does not answer on the LAN control port."""


class LanProtocolError(LanError):
    """The printer answered with unexpected provisioning data."""


# ------------------------------------------------------------------ credentials


@dataclass
class LanCredentials:
    """Local MQTT credentials provisioned from the printer."""

    host: str
    type_id: str
    printer_id: str
    username: str
    password: str
    device_cert: str
    device_key: str
    model_name: str = ""
    device_name: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "host": self.host,
            "type_id": self.type_id,
            "printer_id": self.printer_id,
            "username": self.username,
            "password": self.password,
            "device_cert": self.device_cert,
            "device_key": self.device_key,
            "model_name": self.model_name,
            "device_name": self.device_name,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> LanCredentials | None:
        try:
            return LanCredentials(
                host=str(data["host"]),
                type_id=str(data["type_id"]),
                printer_id=str(data["printer_id"]),
                username=str(data["username"]),
                password=str(data["password"]),
                device_cert=str(data["device_cert"]),
                device_key=str(data["device_key"]),
                model_name=str(data.get("model_name", "")),
                device_name=str(data.get("device_name", "")),
            )
        except (KeyError, TypeError):
            return None


def probe_lan_mode(host: str, timeout: float = _HTTP_TIMEOUT) -> bool:
    """True when the printer answers on the LAN control port (LAN mode on).

    *timeout* defaults to the normal per-host timeout; network scanning
    passes a much shorter one so probing ~250 addresses stays fast.
    """
    try:
        _http_json(f"http://{_clean_host(host)}:{CTRL_HTTP_PORT}/info", timeout=timeout)
    except LanError:
        return False
    return True


def provision(host: str) -> LanCredentials:
    """Fetch and decrypt local MQTT credentials from the printer.

    Raises :class:`LanUnavailable` when the printer can't be reached (LAN mode
    off or wrong IP) and :class:`LanProtocolError` on unexpected responses.
    """
    clean = _clean_host(host)
    info = _http_json(f"http://{clean}:{CTRL_HTTP_PORT}/info")

    token = info.get("token")
    ctrl_url = info.get("ctrlInfoUrl")
    if not isinstance(token, str) or len(token) < 32 or not isinstance(ctrl_url, str):
        raise LanProtocolError("Printer returned an invalid LAN provisioning response")

    ts = str(int(time.time() * 1000))
    nonce = "".join(secrets.choice(_NONCE_CHARS) for _ in range(6))
    sign = _md5(_md5(token[:16]) + ts + nonce)
    params = {"ts": ts, "nonce": nonce, "sign": sign, "did": uuid4().hex.upper()}
    ctrl = _http_json(f"{ctrl_url}?{urllib.parse.urlencode(params)}", method="POST")

    if ctrl.get("code") != 200 or not isinstance(ctrl.get("data"), dict):
        raise LanProtocolError(
            f"Printer rejected the LAN credential request (code {ctrl.get('code')})"
        )
    data = ctrl["data"]
    encrypted = data.get("info")
    iv = data.get("token")
    if not isinstance(encrypted, str) or not isinstance(iv, str) or len(iv) != 16:
        raise LanProtocolError("Printer returned invalid LAN credential material")

    try:
        plaintext = pkcs7_unpad(
            aes128_cbc_decrypt(
                base64.b64decode(encrypted), token[16:32].encode(), iv.encode()
            )
        )
        bundle = json.loads(plaintext.decode())
    except (ValueError, json.JSONDecodeError) as exc:
        raise LanProtocolError("Could not decrypt the LAN credential bundle") from exc
    if not isinstance(bundle, dict):
        raise LanProtocolError("LAN credential bundle was not an object")

    try:
        return LanCredentials(
            host=clean,
            type_id=str(int(bundle.get("modelId") or bundle.get("modeId"))),
            printer_id=str(bundle["deviceId"]),
            username=str(bundle["username"]),
            password=str(bundle["password"]),
            device_cert=str(bundle["devicecrt"]),
            device_key=str(bundle["devicepk"]),
            model_name=str(bundle.get("modelName") or info.get("modelName") or ""),
            device_name=str(info.get("deviceName") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LanProtocolError("LAN credential bundle was incomplete") from exc


# ------------------------------------------------------------------ status


@dataclass
class AceSlot:
    """One filament slot in an ACE multi-color box (e.g. on a Kobra S1 Combo)."""

    material: str = ""
    status: str = ""
    percent: float = 0.0
    weight: float = 0.0
    sku: str = ""
    color_hex: str = ""


@dataclass
class LanPrinterStatus:
    """A snapshot of live printer state collected over local MQTT."""

    online: bool = False
    error: str = ""
    model_name: str = ""
    device_name: str = ""
    firmware_version: str = ""
    print_state: str = ""
    print_filename: str = ""
    print_progress: float = 0.0  # 0.0 – 1.0
    current_layer: int = 0
    total_layers: int = 0
    remaining_minutes: int = 0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    bed_temp: float = 0.0
    bed_target: float = 0.0
    fan_speed_pct: int = 0
    light_on: bool | None = None  # None: printer hasn't reported it (yet)
    light_brightness: int = 0  # 0-100
    camera_available: bool | None = None
    camera_url: str = ""  # only set once a stream has been (re)started
    ace_present: bool = False
    ace_temp: float = 0.0
    ace_humidity: float = 0.0
    ace_loaded_slot: int = -1  # -1: none loaded / unknown
    ace_slots: list[AceSlot] = field(default_factory=list)
    raw_topics: list[str] = field(default_factory=list)
    task_id: str = ""  # current print job id, needed for pause/resume/stop


class AnycubicLanClient:
    """Reads live status from an Anycubic printer in LAN mode."""

    def __init__(self, credentials: LanCredentials) -> None:
        self.credentials = credentials

    def fetch_status(
        self,
        collect_seconds: float = _REPORT_COLLECT_SECONDS,
        *,
        start_camera: bool = False,
        stop_camera: bool = False,
        set_light: tuple[bool, int] | None = None,
    ) -> LanPrinterStatus:
        """Connect over MQTT, query the printer and collect its reports.

        *start_camera*/*stop_camera* ask the printer to (un)publish its FLV
        stream; when starting, the resulting ``camera_url`` (if the printer
        answers in time) appears on the returned status. *set_light* is
        ``(on, brightness)`` with brightness 0-100, applied before the
        read-only queries so the printer's own report reflects the change.
        """
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415 - optional dependency
        except ImportError:
            return LanPrinterStatus(online=False, error="paho-missing")

        creds = self.credentials
        status = LanPrinterStatus(
            model_name=creds.model_name, device_name=creds.device_name
        )
        state_lock = threading.Lock()
        connected = threading.Event()
        rejected = threading.Event()

        def on_connect(client, _userdata, _flags, reason_code, _properties=None):
            if getattr(reason_code, "is_failure", False) or (
                isinstance(reason_code, int) and reason_code != 0
            ):
                rejected.set()
                connected.set()
                return
            for topic in (
                f"{TOPIC_BASE}/printer/+/{creds.type_id}/{creds.printer_id}/#",
                f"{TOPIC_BASE}/printer/public/{creds.type_id}/{creds.printer_id}/#",
                f"{TOPIC_BASE}/web/printer/{creds.type_id}/{creds.printer_id}/#",
            ):
                client.subscribe(topic)
            connected.set()

        def on_message(_client, _userdata, message):
            try:
                payload = json.loads(message.payload.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            with state_lock:
                status.raw_topics.append(message.topic)
                _apply_report(status, message.topic, payload, creds.host)

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"anycubic-toolkit-{uuid4().hex[:12]}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(creds.username, creds.password)

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        cert_path = key_path = None
        try:
            try:
                if creds.device_cert and creds.device_key:
                    cert_path = _temp_pem(creds.device_cert)
                    key_path = _temp_pem(creds.device_key)
                    context.load_cert_chain(cert_path, key_path)
                client.tls_set_context(context)
                client.tls_insecure_set(True)
            except (ssl.SSLError, OSError, ValueError) as exc:
                return LanPrinterStatus(online=False, error=f"tls: {exc}")
            client.on_connect = on_connect
            client.on_message = on_message

            try:
                client.connect(creds.host, MQTT_PORT, keepalive=30)
            except (OSError, TimeoutError) as exc:
                return LanPrinterStatus(online=False, error=f"unreachable: {exc}")
            client.loop_start()
            try:
                if not connected.wait(timeout=_MQTT_CONNECT_TIMEOUT):
                    return LanPrinterStatus(online=False, error="timeout")
                if rejected.is_set():
                    return LanPrinterStatus(online=False, error="rejected")

                base = (
                    f"{TOPIC_BASE}/web/printer/{creds.type_id}/{creds.printer_id}"
                )

                if start_camera:
                    client.publish(
                        f"{base}/video",
                        json.dumps(_action_payload("video", "startCapture")),
                        qos=0,
                    )
                if stop_camera:
                    client.publish(
                        f"{base}/video",
                        json.dumps(_action_payload("video", "stopCapture")),
                        qos=0,
                    )
                if set_light is not None:
                    on, brightness = set_light
                    effective = max(0, min(100, brightness)) if on else 0
                    client.publish(
                        f"{base}/light",
                        json.dumps(
                            _action_payload(
                                "light",
                                "control",
                                {
                                    "type": 3,
                                    "status": 1 if effective else 0,
                                    "brightness": effective,
                                },
                            )
                        ),
                        qos=0,
                    )

                for _source, query_type, action in _QUERY_SPECS:
                    client.publish(
                        f"{base}/{query_type}",
                        json.dumps(_action_payload(query_type, action)),
                        qos=0,
                    )
                time.sleep(collect_seconds)
            finally:
                client.loop_stop()
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass
        finally:
            for path in (cert_path, key_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        with state_lock:
            status.online = True
            return status

    def send_print_command(self, action: str, task_id: str = "") -> str:
        """Publish a print control command (``pause`` / ``resume`` / ``stop``).

        Opens a short-lived MQTT session, publishes to the printer's ``print``
        topic and waits briefly so the broker accepts the message before
        disconnecting. Returns an empty string on success or an error code
        (mirroring :class:`LanPrinterStatus.error` values) on failure. The
        printer identifies the job by *task_id*, captured from its own print
        reports — commands without it are still sent (some firmwares accept
        them for the active job).
        """
        if action not in ("pause", "resume", "stop"):
            return f"unsupported-action: {action}"
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415 - optional dependency
        except ImportError:
            return "paho-missing"

        creds = self.credentials
        connected = threading.Event()
        rejected = threading.Event()

        def on_connect(client, _userdata, _flags, reason_code, _properties=None):
            if getattr(reason_code, "is_failure", False) or (
                isinstance(reason_code, int) and reason_code != 0
            ):
                rejected.set()
            connected.set()

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"anycubic-toolkit-cmd-{uuid4().hex[:12]}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(creds.username, creds.password)

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        cert_path = key_path = None
        try:
            try:
                if creds.device_cert and creds.device_key:
                    cert_path = _temp_pem(creds.device_cert)
                    key_path = _temp_pem(creds.device_key)
                    context.load_cert_chain(cert_path, key_path)
                client.tls_set_context(context)
                client.tls_insecure_set(True)
            except (ssl.SSLError, OSError, ValueError) as exc:
                return f"tls: {exc}"
            client.on_connect = on_connect
            try:
                client.connect(creds.host, MQTT_PORT, keepalive=15)
            except (OSError, TimeoutError) as exc:
                return f"unreachable: {exc}"
            client.loop_start()
            try:
                if not connected.wait(timeout=_MQTT_CONNECT_TIMEOUT):
                    return "timeout"
                if rejected.is_set():
                    return "rejected"
                base = f"{TOPIC_BASE}/web/printer/{creds.type_id}/{creds.printer_id}"
                data: dict[str, Any] = {"taskid": task_id} if task_id else {}
                info = client.publish(
                    f"{base}/print",
                    json.dumps(_action_payload("print", action, data)),
                    qos=1,
                )
                info.wait_for_publish(timeout=5)
                return "" if info.is_published() else "publish-failed"
            finally:
                client.loop_stop()
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass
        finally:
            for path in (cert_path, key_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass


def _action_payload(message_type: str, action: str, data: dict[str, Any] | None = None) -> dict:
    """Build a command/query payload in the shape the printer expects."""
    return {
        "type": message_type,
        "action": action,
        "timestamp": int(time.time() * 1000),
        "msgid": str(uuid4()),
        "data": data,
    }


# ------------------------------------------------------------------ parsing


def _apply_report(status: LanPrinterStatus, topic: str, payload: dict, host: str = "") -> None:
    """Fold one printer report into *status* (tolerant of field variants)."""
    if not isinstance(payload, dict):
        return
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def pick(*keys: str) -> Any:
        for key in keys:
            for source in (data, payload):
                value = source.get(key)
                if value not in (None, ""):
                    return value
        return None

    state = pick("printState", "print_state", "printStatus", "state")
    if isinstance(state, str) and "video" not in topic and "light" not in topic:
        status.print_state = state

    filename = pick("filename", "printName", "file", "taskName")
    if isinstance(filename, str):
        status.print_filename = filename

    task_id = pick("taskid", "task_id", "taskId")
    if task_id not in (None, ""):
        status.task_id = str(task_id)

    progress = pick("progress", "printProgress")
    if progress is not None:
        value = _as_float(progress)
        status.print_progress = value / 100.0 if value > 1.0 else value

    layer = pick("curr_layer", "currentLayer", "cur_layer")
    if layer is not None:
        status.current_layer = _as_int(layer)
    total = pick("total_layer", "totalLayer", "total_layers")
    if total is not None:
        status.total_layers = _as_int(total)
    remain = pick("remain_time", "remainTime", "remaining_time")
    if remain is not None:
        status.remaining_minutes = _as_int(remain)

    nozzle = pick("curr_nozzle_temp", "nozzle_temp", "nozzleTemp", "currentNozzleTemp")
    if nozzle is not None:
        status.nozzle_temp = _as_float(nozzle)
    nozzle_target = pick("target_nozzle_temp", "targetNozzleTemp")
    if nozzle_target is not None:
        status.nozzle_target = _as_float(nozzle_target)
    bed = pick("curr_hotbed_temp", "hotbed_temp", "bedTemp", "currentHotbedTemp")
    if bed is not None:
        status.bed_temp = _as_float(bed)
    bed_target = pick("target_hotbed_temp", "targetHotbedTemp")
    if bed_target is not None:
        status.bed_target = _as_float(bed_target)

    fan = pick("fan_speed_pct", "fanSpeedPct", "fan_speed", "model_fan")
    if fan is not None:
        status.fan_speed_pct = _as_int(fan)

    firmware = pick("firmwareVersion", "firmware_version", "version")
    if isinstance(firmware, str) and firmware.count(".") >= 1:
        status.firmware_version = firmware
    model = pick("modelName", "model_name", "machineName")
    if isinstance(model, str) and not status.model_name:
        status.model_name = model

    camera_available = pick("camera_available", "cameraAvailable")
    if camera_available is not None:
        status.camera_available = bool(camera_available)
    if "light" in topic or str(payload.get("type", "")).lower() == "light":
        brightness = pick("brightness")
        light_status = pick("status")
        if brightness is not None:
            status.light_brightness = _as_int(brightness)
            status.light_on = status.light_brightness > 0
        elif light_status is not None:
            status.light_on = bool(_as_int(light_status))
    if "video" in topic or str(payload.get("type", "")).lower() == "video":
        state = pick("state", "action")
        if isinstance(state, str) and state:
            status.camera_available = True
    stream_url = _extract_stream_url(payload, host)
    if stream_url:
        status.camera_url = stream_url
        status.camera_available = True

    _apply_ace_report(status, data)


def _apply_ace_report(status: LanPrinterStatus, data: dict) -> None:
    """Fold ``multi_color_box`` (ACE unit) data into *status*, if present."""
    boxes = data.get("multi_color_box")
    if not isinstance(boxes, list) or not boxes:
        return
    box = boxes[0]
    if not isinstance(box, dict):
        return
    status.ace_present = True

    temp = box.get("temp")
    if temp is not None:
        status.ace_temp = _as_float(temp)
    humidity = box.get("humidity")
    if humidity is not None:
        status.ace_humidity = _as_float(humidity)
    loaded_slot = box.get("loaded_slot")
    if loaded_slot is not None:
        status.ace_loaded_slot = _as_int(loaded_slot)

    slots = box.get("slots")
    if not isinstance(slots, list):
        return
    parsed: list[AceSlot] = []
    for slot in slots:
        if not isinstance(slot, dict):
            parsed.append(AceSlot())
            continue
        color_hex = ""
        color = slot.get("color")
        if isinstance(color, list) and len(color) >= 3:
            try:
                color_hex = f"#{int(color[0]):02x}{int(color[1]):02x}{int(color[2]):02x}"
            except (TypeError, ValueError):
                color_hex = ""
        parsed.append(
            AceSlot(
                material=str(slot.get("type") or ""),
                status=str(slot.get("status") or ""),
                percent=_as_float(slot.get("consumables_percent")),
                weight=_as_float(slot.get("weight")),
                sku=str(slot.get("sku") or ""),
                color_hex=color_hex,
            )
        )
    status.ace_slots = parsed


def _extract_stream_url(payload: dict, host: str = "") -> str:
    """Find a live-camera URL in a report, however it was embedded."""
    text = json.dumps(payload)
    match = _LIVE_URL_RE.search(text)
    if match:
        return match.group(0)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("streamUrl", "videoUrl", "flvUrl", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    token = data.get("token")
    if isinstance(token, str) and token and host:
        return f"http://{host}:{STREAM_HTTP_PORT}/live/{token}"
    return ""


# ------------------------------------------------------------------ helpers


def _clean_host(host: str) -> str:
    host = (host or "").strip()
    if host.startswith(("http://", "https://")):
        host = host.split("://", 1)[1]
    return host.split("/", 1)[0].split(":", 1)[0]


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _http_json(url: str, method: str = "GET", timeout: float = _HTTP_TIMEOUT) -> dict:
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": _USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            payload = json.loads(response.read().decode(charset, errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LanUnavailable(str(exc)) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise LanProtocolError("Printer returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise LanProtocolError("Printer returned an unexpected response")
    return payload


def _temp_pem(content: str) -> str:
    if not content.endswith("\n"):
        content += "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".pem", delete=False
    ) as handle:
        handle.write(content)
        return handle.name


def _as_float(value: Any) -> float:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
