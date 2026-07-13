"""Password database providers.

Anycubic's ``AC_LOG.pack`` archives are protected with passwords that are
**public information**, maintained by the community (chiefly the open-source
Rinkhals project). This application therefore **never hardcodes any Anycubic
password in its source code**. Instead the password database is retrieved
through a chain of providers, tried in priority order:

1. :class:`WizzApiPasswordProvider` — the official Toolkit API on wizz.se
   (``/wp-json/anycubic-toolkit/v1/keys``). This is the canonical, structured
   source.
2. :class:`RinkhalsPasswordProvider` — the Rinkhals project
   (``https://jbatonnet.github.io/Rinkhals/``). Rinkhals does not publish a
   stable ``passwords.json``; its per-model pack/SWU passwords live as string
   literals inside its firmware tooling, so this provider fetches the raw
   source and parses it defensively.
3. :class:`LocalCachePasswordProvider` — the on-disk copy at
   ``<cache>/passwords.json`` written by earlier successful fetches.

:class:`PasswordService` orchestrates the chain: it returns a fresh cached
database without touching the network, refreshes from the network when the
cache is missing or older than :data:`CACHE_STALE_DAYS`, always writes
successful downloads back to the local cache, and falls back to the (possibly
stale) cache when the machine is offline. It also exposes the cache age so the
UI can warn the user when the database is more than 30 days old.

Privacy guarantee: providers only ever **download** the password database.
No password and no log content is ever transmitted to any server — archive
extraction happens entirely locally (see :mod:`anycubic_toolkit.core.logpack`).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anycubic_toolkit import __app_name__, __version__
from anycubic_toolkit.core.api import ApiError, WizzApiClient
from anycubic_toolkit.core.config import cache_dir

# A database older than this many days triggers a UI notification.
CACHE_STALE_DAYS: int = 30

# File name of the persisted database inside the cache directory.
CACHE_FILE_NAME: str = "passwords.json"

# Network timeout for provider downloads, in seconds.
_TIMEOUT = 12

# Documentation URL shown to users; the human-readable Rinkhals home.
RINKHALS_HOME = "https://jbatonnet.github.io/Rinkhals/"

# Locations the Rinkhals provider attempts, in order. The first is an optional
# clean JSON (if the project ever publishes one); the rest are raw source files
# whose password literals are parsed defensively.
_RINKHALS_SOURCES: tuple[str, ...] = (
    "https://jbatonnet.github.io/Rinkhals/passwords.json",
    "https://raw.githubusercontent.com/jbatonnet/Rinkhals/master/"
    "files/3-rinkhals/opt/rinkhals/ui/common.py",
    "https://raw.githubusercontent.com/jbatonnet/Rinkhals/main/"
    "files/3-rinkhals/opt/rinkhals/ui/common.py",
)

_USER_AGENT = f"{__app_name__}/{__version__} (+{RINKHALS_HOME})"


# --------------------------------------------------------------------- model


@dataclass
class PasswordDatabase:
    """A set of candidate archive passwords with provenance metadata."""

    passwords: list[str] = field(default_factory=list)
    by_model: dict[str, list[str]] = field(default_factory=dict)
    source: str = ""                 # provider id that produced this data
    updated_at: str = ""             # ISO-8601 UTC timestamp

    def is_empty(self) -> bool:
        """True when there are no candidate passwords."""
        return not self.passwords

    def age_days(self) -> float | None:
        """Age of the data in days, or ``None`` if the timestamp is missing."""
        stamp = _parse_timestamp(self.updated_at)
        if stamp is None:
            return None
        delta = datetime.now(timezone.utc) - stamp
        return max(0.0, delta.total_seconds() / 86_400)

    def is_stale(self, max_age_days: int = CACHE_STALE_DAYS) -> bool:
        """True when the data is older than *max_age_days* (unknown age = stale)."""
        age = self.age_days()
        return age is None or age > max_age_days

    def merged_with(self, other: "PasswordDatabase") -> "PasswordDatabase":
        """Return a copy of *self* augmented with passwords from *other*.

        *self* takes precedence for ordering and metadata; *other*'s entries
        that are not already present are appended. Used so a network refresh
        never drops passwords the local cache already knew about.
        """
        passwords = list(self.passwords)
        for value in other.passwords:
            if value not in passwords:
                passwords.append(value)
        by_model: dict[str, list[str]] = {k: list(v) for k, v in self.by_model.items()}
        for model, values in other.by_model.items():
            bucket = by_model.setdefault(model, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
        return PasswordDatabase(
            passwords=passwords,
            by_model=by_model,
            source=self.source,
            updated_at=self.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the on-disk cache file."""
        return {
            "version": 1,
            "updated_at": self.updated_at,
            "source": self.source,
            "passwords": self.passwords,
            "by_model": self.by_model,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "PasswordDatabase":
        """Parse a database from arbitrary (possibly untrusted) JSON."""
        passwords: list[str] = []
        for value in data.get("passwords", []):
            if isinstance(value, str) and value not in passwords:
                passwords.append(value)

        by_model: dict[str, list[str]] = {}
        raw_models = data.get("by_model", {})
        if isinstance(raw_models, dict):
            for model, values in raw_models.items():
                if not isinstance(model, str) or not isinstance(values, list):
                    continue
                cleaned = [v for v in values if isinstance(v, str)]
                if cleaned:
                    by_model[model] = cleaned
                for value in cleaned:
                    if value not in passwords:
                        passwords.append(value)

        return PasswordDatabase(
            passwords=passwords,
            by_model=by_model,
            source=str(data.get("source", "")),
            updated_at=str(data.get("updated_at", "")),
        )


# ------------------------------------------------------------------ providers


class PasswordProvider(ABC):
    """Retrieves a :class:`PasswordDatabase` from one source."""

    #: Stable identifier written into ``source`` and used in logs/UI.
    id: str = "provider"

    @abstractmethod
    def fetch(self) -> PasswordDatabase | None:
        """Return a database, or ``None`` if this source is unavailable.

        Implementations must never raise for ordinary network/parse failures;
        they return ``None`` so the service can fall through to the next
        provider.
        """


class WizzApiPasswordProvider(PasswordProvider):
    """Primary source: the official Toolkit API on wizz.se."""

    id = "wizz-api"

    def __init__(self, api: WizzApiClient) -> None:
        self._api = api

    def fetch(self) -> PasswordDatabase | None:
        try:
            document = self._api.get_key_database()
        except ApiError:
            return None
        except Exception:  # noqa: BLE001 - never let a source break the chain
            return None

        if not isinstance(document, dict):
            return None

        passwords: list[str] = []
        for value in document.get("passwords", []):
            if isinstance(value, str) and value not in passwords:
                passwords.append(value)

        by_model: dict[str, list[str]] = {}
        for entry in document.get("models", []):
            if not isinstance(entry, dict):
                continue
            value = entry.get("log_password", entry.get("password"))
            model = entry.get("model", entry.get("printer", ""))
            if not isinstance(value, str) or not value:
                continue
            if value not in passwords:
                passwords.append(value)
            if isinstance(model, str) and model:
                bucket = by_model.setdefault(model, [])
                if value not in bucket:
                    bucket.append(value)

        if not passwords:
            return None

        updated = document.get("updated")
        return PasswordDatabase(
            passwords=passwords,
            by_model=by_model,
            source=self.id,
            updated_at=updated if isinstance(updated, str) and updated else _now_iso(),
        )


class RinkhalsPasswordProvider(PasswordProvider):
    """Community source: the Rinkhals project.

    Rinkhals does not expose a canonical ``passwords.json``; its pack/SWU
    passwords are string literals inside its firmware tooling. This provider
    tries a published JSON first (in case one appears), then falls back to
    parsing the raw source with a tolerant regex, degrading gracefully.
    """

    id = "rinkhals"

    def __init__(self, sources: tuple[str, ...] = _RINKHALS_SOURCES) -> None:
        self._sources = sources

    def fetch(self) -> PasswordDatabase | None:
        for url in self._sources:
            body = _http_get(url)
            if not body:
                continue
            database = self._parse(url, body)
            if database is not None and not database.is_empty():
                return database
        return None

    @staticmethod
    def _parse(url: str, body: str) -> PasswordDatabase | None:
        # Preferred path: a structured JSON database.
        if url.endswith(".json"):
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return None
            if isinstance(data, dict):
                database = PasswordDatabase.from_dict(data)
            elif isinstance(data, list):
                database = PasswordDatabase.from_dict({"passwords": data})
            else:
                return None
            database.source = RinkhalsPasswordProvider.id
            database.updated_at = _now_iso()
            return database

        # Fallback: parse password string literals from source code.
        passwords: list[str] = []
        for match in _RINKHALS_PASSWORD_RE.finditer(body):
            value = match.group("value")
            if value and value not in passwords:
                passwords.append(value)
        if not passwords:
            return None
        return PasswordDatabase(
            passwords=passwords,
            source=RinkhalsPasswordProvider.id,
            updated_at=_now_iso(),
        )


class LocalCachePasswordProvider(PasswordProvider):
    """Last-resort source: the on-disk ``passwords.json`` cache."""

    id = "cache"

    def __init__(self, path: Path) -> None:
        self._path = path

    def fetch(self) -> PasswordDatabase | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        database = PasswordDatabase.from_dict(data)
        return database if not database.is_empty() else None


# Matches ``password = '...'`` / ``password: "..."`` style literals in source.
_RINKHALS_PASSWORD_RE = re.compile(
    r"""password['"]?\s*[:=]\s*['"](?P<value>[^'"]{4,})['"]""",
    re.IGNORECASE,
)


# ------------------------------------------------------------------- service


class PasswordService:
    """Coordinates the provider chain, caching and staleness reporting.

    The service is created once at startup and shared. Extraction code calls
    :meth:`candidate_passwords`; the UI reads :attr:`database` and
    :meth:`cache_is_stale` to decide whether to warn about an old cache.
    """

    def __init__(
        self,
        api: WizzApiClient,
        cache_path: Path | None = None,
        stale_days: int = CACHE_STALE_DAYS,
    ) -> None:
        self._cache_path = cache_path or (cache_dir() / CACHE_FILE_NAME)
        self._stale_days = stale_days
        self._network_providers: list[PasswordProvider] = [
            WizzApiPasswordProvider(api),
            RinkhalsPasswordProvider(),
        ]
        self._cache_provider = LocalCachePasswordProvider(self._cache_path)
        self._database: PasswordDatabase | None = None

    # ------------------------------------------------------------- accessors

    @property
    def cache_path(self) -> Path:
        """Location of the persisted ``passwords.json``."""
        return self._cache_path

    @property
    def database(self) -> PasswordDatabase | None:
        """The database resolved by the last :meth:`load`, if any."""
        return self._database

    def cache_age_days(self) -> float | None:
        """Age in days of the resolved database, or ``None`` if unknown."""
        return self._database.age_days() if self._database else None

    def cache_is_stale(self) -> bool:
        """True when the resolved database is missing or older than the limit."""
        if self._database is None or self._database.is_empty():
            return True
        return self._database.is_stale(self._stale_days)

    # ------------------------------------------------------------- retrieval

    def load(self, force_refresh: bool = False) -> PasswordDatabase:
        """Resolve the password database following the priority policy.

        Without *force_refresh*, a present and fresh cache is returned with no
        network access. Otherwise the network providers (wizz → Rinkhals) are
        tried in order; the first non-empty result is merged with any existing
        cache, written back to disk and returned. If every network provider is
        unavailable, the (possibly stale) cache is used. The result is always a
        :class:`PasswordDatabase`, even if empty.
        """
        cached = self._cache_provider.fetch()

        if not force_refresh and cached is not None and not cached.is_stale(self._stale_days):
            self._database = cached
            return cached

        for provider in self._network_providers:
            fetched = provider.fetch()
            if fetched is None or fetched.is_empty():
                continue
            combined = fetched.merged_with(cached) if cached else fetched
            self._write_cache(combined)
            self._database = combined
            return combined

        # Offline (or every network source failed): use whatever we have.
        self._database = cached or PasswordDatabase()
        return self._database

    def candidate_passwords(self, force_refresh: bool = False) -> list[str]:
        """Convenience: the flat password list from :meth:`load`."""
        return list(self.load(force_refresh=force_refresh).passwords)

    def refresh(self) -> PasswordDatabase:
        """Force a network refresh (used by an explicit 'update database' action)."""
        return self.load(force_refresh=True)

    # -------------------------------------------------------------- internal

    def _write_cache(self, database: PasswordDatabase) -> None:
        """Persist *database* to the cache file (best-effort, atomic)."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(database.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._cache_path)
        except OSError:
            # Caching is best-effort; a read-only cache dir must not break use.
            pass


# ------------------------------------------------------------------- helpers


def _http_get(url: str) -> str | None:
    """Fetch *url* as text, returning ``None`` on any failure.

    Only performs an HTTP GET — nothing about the user's machine, logs or
    passwords is ever sent.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with timezone."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp
