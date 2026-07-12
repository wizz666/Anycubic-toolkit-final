"""Redaction of sensitive data found in printer logs.

Anycubic ``AC_LOG.pack`` archives can contain private information in clear
text — most notably the Wi-Fi SSID and password, the wireless MAC address, and
cloud access tokens. This module masks those values so they never persist in a
cached analysis, appear in the UI, or leak into a shared support report.

The functions are deliberately conservative: they mask credential *values*
while keeping the surrounding key/context, and they never touch error codes,
component names or other diagnostic content.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Keys whose values are secrets. Matched in JSON ("k":"v"), ini (k=v) and
# log (k: v) styles.
_SENSITIVE_KEYS = (
    r"wifi[_-]?password|wifi[_-]?ssid|ssid|password|passwd|pwd|psk|"
    r"access[_-]?token|token|secret|api[_-]?key|apikey|auth[_-]?key|authorization"
)
_KV_RE = re.compile(
    r'(?i)(["\']?(?:' + _SENSITIVE_KEYS + r')["\']?\s*[:=]\s*)'
    r'(["\']?)([^"\',;}\s][^"\',;}\r\n]*)(\2)'
)

# Wireless MAC address.
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")

# Long signed-URL query tokens (e.g. Anycubic cloud pre-signed URLs).
_URL_TOKEN_RE = re.compile(
    r'(?i)((?:token|signature|sign|key|auth)=)([A-Za-z0-9%_\-\.]{16,})'
)


def _kv_sub(match: re.Match[str]) -> str:
    quote = match.group(2)
    return f"{match.group(1)}{quote}{REDACTED}{match.group(4)}"


def redact_sensitive(text: str) -> str:
    """Return *text* with credentials, MACs and URL tokens masked."""
    if not text:
        return text
    result = _KV_RE.sub(_kv_sub, text)
    result = _MAC_RE.sub(REDACTED, result)
    result = _URL_TOKEN_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
    return result


def contains_sensitive(text: str) -> bool:
    """True when *text* holds data that :func:`redact_sensitive` would mask."""
    if not text:
        return False
    return bool(_KV_RE.search(text) or _MAC_RE.search(text))


def mask_identifier(value: str, keep_start: int = 2, keep_end: int = 4) -> str:
    """Partially mask an identifier (serial, device id) for safe sharing.

    Keeps a few leading/trailing characters so it stays useful to support
    without revealing the whole value, e.g. ``S0``…``60G2``.
    """
    value = (value or "").strip()
    if not value:
        return value
    if len(value) <= keep_start + keep_end:
        return "\u2022" * len(value)
    middle = "\u2022" * (len(value) - keep_start - keep_end)
    return f"{value[:keep_start]}{middle}{value[-keep_end:]}"
