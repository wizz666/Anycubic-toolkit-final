"""Notifications for print events.

Sends a short message when a print finishes or fails, through one of three
simple, self-hosted-friendly channels:

* **ntfy** — POST the message body to ``https://<server>/<topic>``.
* **Discord** — POST ``{"content": ...}`` to a webhook URL.
* **Webhook** — POST a small JSON payload to any URL.

All sending is best-effort and off the UI thread; failures are swallowed with a
returned ``False`` rather than raised.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from anycubic_toolkit import __app_name__, __version__

_TIMEOUT = 10
_USER_AGENT = f"{__app_name__}/{__version__}"

PROVIDER_NTFY = "ntfy"
PROVIDER_DISCORD = "discord"
PROVIDER_WEBHOOK = "webhook"


@dataclass
class NotifierConfig:
    enabled: bool = False
    provider: str = PROVIDER_NTFY
    target: str = ""            # ntfy topic/URL, Discord webhook, or webhook URL
    ntfy_server: str = "https://ntfy.sh"

    @staticmethod
    def from_config(cfg) -> "NotifierConfig":
        return NotifierConfig(
            enabled=bool(cfg.get("notify_enabled", False)),
            provider=str(cfg.get("notify_provider", PROVIDER_NTFY)),
            target=str(cfg.get("notify_target", "")),
            ntfy_server=str(cfg.get("notify_ntfy_server", "https://ntfy.sh")),
        )


class Notifier:
    """Sends notifications according to a :class:`NotifierConfig`."""

    def __init__(self, config: NotifierConfig) -> None:
        self.config = config

    def is_ready(self) -> bool:
        return bool(self.config.enabled and self.config.target.strip())

    def notify(self, title: str, message: str) -> bool:
        """Send *title*/*message* through the configured channel."""
        if not self.is_ready():
            return False
        provider = self.config.provider
        target = self.config.target.strip()
        try:
            if provider == PROVIDER_NTFY:
                return self._send_ntfy(target, title, message)
            if provider == PROVIDER_DISCORD:
                return self._post_json(target, {"content": f"**{title}**\n{message}"})
            return self._post_json(
                target, {"title": title, "message": message, "app": __app_name__}
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False

    # ------------------------------------------------------------- channels

    def _send_ntfy(self, target: str, title: str, message: str) -> bool:
        if target.startswith(("http://", "https://")):
            url = target
        else:
            url = f"{self.config.ntfy_server.rstrip('/')}/{target.lstrip('/')}"
        request = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": _USER_AGENT,
                "Title": title.encode("ascii", "replace").decode(),
                "Tags": "printer",
            },
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return 200 <= response.status < 300

    def _post_json(self, url: str, payload: dict) -> bool:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"User-Agent": _USER_AGENT, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return 200 <= response.status < 300
