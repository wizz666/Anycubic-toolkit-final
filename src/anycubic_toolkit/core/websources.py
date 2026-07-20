"""External web sources — fetched **directly from Anycubic's own sites**.

The toolkit pulls error-code and firmware information straight from Anycubic,
never from a mirror. wizz.se is used only for the log-password database; the
data in this module comes from:

* Error codes — the Anycubic Wiki. Each code has its own server-rendered page
  at ``/en/error-codes/{code}-code`` whose content a plain ``urllib`` request
  can read (no browser engine required). The wiki *index* page is a client-side
  app and is used only as a "browse all" deep link.
* Firmware — the official Anycubic "Firmware & Software" page. It is fetched
  directly and download links are extracted best-effort; the page is always
  offered as an authoritative deep link. For the Kobra X specifically, the
  page's real download links only appear after client-side JavaScript runs
  (no browser engine here), and Rinkhals has no catalog for it either (its
  firmware isn't cracked yet), so the app also deep-links a community-shared
  folder as a clearly-labeled, unverified fallback — never downloaded or
  re-hosted by the app itself.
* Makeronline — Anycubic's model platform, opened in the browser.

Only canonical, tracking-free URLs are requested. No personal data or tracking
parameters (``_sasdk``, ``gclid``, ``utm_*``, …) are ever sent, and nothing
about the user's machine or logs leaves the computer.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

from anycubic_toolkit import (
    __anycubic_error_codes__,
    __anycubic_firmware__,
    __anycubic_wiki__,
    __app_name__,
    __kobra_x_community_firmware__,
    __makeronline__,
    __version__,
)
from anycubic_toolkit.core.config import cache_dir

_TIMEOUT = 12
_USER_AGENT = f"{__app_name__}/{__version__}"

# --------------------------------------------------------------- deep links


def error_codes_url() -> str:
    """Anycubic Wiki error-codes index (used as a 'browse all' link)."""
    return __anycubic_error_codes__


def error_code_page_url(code: str) -> str:
    """Direct, server-rendered wiki page for a specific error *code*.

    Anycubic's dominant slug pattern is ``{code}-code`` (e.g.
    ``/en/error-codes/11518-code``). Some codes use descriptive slugs; those
    fall back to the index deep link in the UI.
    """
    safe = urllib.parse.quote(str(code).strip(), safe="")
    return f"{__anycubic_wiki__.rstrip('/')}/en/error-codes/{safe}-code"


def firmware_url() -> str:
    """Official Anycubic firmware & software page."""
    return __anycubic_firmware__


def kobra_x_community_firmware_url() -> str:
    """Community-shared Kobra X firmware folder (unverified — see docstring above)."""
    return __kobra_x_community_firmware__


def makeronline_url(path: str = "") -> str:
    """Makeronline home, or a sub-path beneath it."""
    base = __makeronline__.rstrip("/")
    if not path:
        return __makeronline__
    return f"{base}/{path.lstrip('/')}"


# --------------------------------------------------------------- live fetch


class WebSourceClient:
    """Best-effort fetcher for Anycubic pages, with a small on-disk cache.

    Every method degrades gracefully: on any network/parse failure it returns
    an empty result (never raises), so the UI can fall back to the
    authoritative deep link to the official page.
    """

    def __init__(self, cache_root: Path | None = None) -> None:
        self._cache = (cache_root or cache_dir()) / "web"
        self._cache.mkdir(parents=True, exist_ok=True)

    def fetch_text(self, url: str) -> str:
        """Return page text, falling back to a cached copy when offline."""
        text = _http_get(url)
        cache_file = self._cache_path(url)
        if text:
            try:
                cache_file.write_text(text, encoding="utf-8")
            except OSError:
                pass
            return text
        try:
            return cache_file.read_text(encoding="utf-8")
        except OSError:
            return ""

    def fetch_error_code(self, code: str) -> dict[str, str] | None:
        """Fetch and parse a single error code straight from the Anycubic Wiki.

        Returns ``{"code", "description", "summary", "url"}`` or ``None`` when
        the code has no ``{code}-code`` page (unknown code or descriptive slug).
        """
        url = error_code_page_url(code)
        html = self.fetch_text(url)
        if not html:
            return None
        parsed = parse_error_code_page(html, str(code).strip())
        if parsed is None:
            return None
        parsed["url"] = url
        return parsed

    def firmware_entries(self) -> list[dict[str, str]]:
        """Best-effort firmware download links from the official Anycubic page."""
        return parse_firmware(self.fetch_text(firmware_url()))

    def _cache_path(self, url: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9]+", "_", url)[:120]
        return self._cache / f"{safe or 'page'}.html"


# --------------------------------------------------------------- parsers

_TITLE_RE = re.compile(r"<title[^>]*>(?P<v>.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](?P<v>.*?)["\']',
    re.IGNORECASE,
)
_META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+'
    r'content=["\'](?P<v>.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_SITE_SUFFIX_RE = re.compile(r"\s*[|\u2013\-]\s*Anycubic\s*Wiki\s*$", re.IGNORECASE)
# Leading "CODE", optional colon (ASCII or fullwidth), optional repeated number.
_CODE_PREFIX_RE = re.compile(r"^\s*CODE\s*[:\uFF1A]?\s*", re.IGNORECASE)


def parse_error_code_page(html: str, code: str) -> dict[str, str] | None:
    """Extract ``code``/``description``/``summary`` from a wiki code page."""
    if not html:
        return None

    raw_title = ""
    match = _OG_TITLE_RE.search(html) or _TITLE_RE.search(html)
    if match:
        raw_title = unescape(match.group("v")).strip()
    if not raw_title:
        return None

    title = _SITE_SUFFIX_RE.sub("", raw_title).strip()
    if re.match(r"(?i)^\s*(page\s+not\s+found|404|not\s+found)\s*$", title):
        return None
    description = _CODE_PREFIX_RE.sub("", title).strip()
    # Drop a leading duplicate of the code number if present.
    if description.startswith(code):
        description = description[len(code):].lstrip(" :\uFF1A-\u2013").strip()
    if not description:
        description = title

    summary = ""
    desc_match = _META_DESC_RE.search(html)
    if desc_match:
        summary = " ".join(unescape(desc_match.group("v")).split())

    # A genuine "not found" page has no meaningful, code-specific content.
    if not description and not summary:
        return None
    return {"code": code, "description": description, "summary": summary}


# Firmware links such as .../file_v1.2.3.zip, .bin, .swu, .img, .acf.
_FIRMWARE_LINK_RE = re.compile(
    r'href=["\'](?P<url>https?://[^"\']+?\.(?:zip|bin|swu|img|acf|tar\.gz))["\']',
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"v?\d+\.\d+(?:\.\d+)*")


def parse_firmware(html: str) -> list[dict[str, str]]:
    """Extract downloadable firmware links from the Anycubic page, if present."""
    if not html:
        return []
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _FIRMWARE_LINK_RE.finditer(html):
        url = match.group("url")
        if url in seen:
            continue
        seen.add(url)
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        version_match = _VERSION_RE.search(name)
        entries.append(
            {
                "name": name,
                "version": version_match.group(0) if version_match else "",
                "url": url,
            }
        )
    return entries


# --------------------------------------------------------------- internal


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return ""
