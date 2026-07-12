"""AC_LOG.pack handling and log analysis.

Anycubic printers export diagnostics as a password-protected ZIP archive
named ``AC_LOG.pack``. This module:

1. Obtains the community password database from
   :class:`~anycubic_toolkit.core.passwords.PasswordService` (wizz.se →
   Rinkhals → local cache). **No password is ever hardcoded here.**
2. Unlocks and extracts the archive **locally** — log contents never leave
   the machine.
3. Parses the extracted text logs for printer identity, firmware version,
   errors and warnings.
4. Computes per-component health scores and an overall score.

The parser is deliberately conservative: it uses broad, well-documented
patterns so that new firmware log formats degrade gracefully instead of
crashing the analysis.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

from anycubic_toolkit.core.api import WizzApiClient
from anycubic_toolkit.core.models import KNOWN_MODELS, MODEL_ID_TO_CODE, model_name_or_code
from anycubic_toolkit.core.passwords import PasswordService
from anycubic_toolkit.core.redaction import contains_sensitive, redact_sensitive

ProgressFn = Callable[[int, str], None]

TEXT_EXTENSIONS = {".log", ".txt", ".ini", ".cfg", ".json", ""}

# --------------------------------------------------------------------- data


@dataclass
class LogIssue:
    """A single error or warning found in the logs."""

    severity: str          # "error" | "warning"
    code: str              # numeric error code if present, else ""
    message: str
    source_file: str
    line_number: int


@dataclass
class ComponentScore:
    """Health score (0-100) for one printer subsystem."""

    component: str         # extruder, ace, bed, temperature, fans, motors
    score: int
    issues: int


@dataclass
class LogAnalysisResult:
    """Everything the Log Analyzer learned from one AC_LOG.pack."""

    source_path: str = ""
    printer_model: str = ""
    model_code: str = ""
    serial_number: str = ""
    firmware_version: str = ""
    errors: list[LogIssue] = field(default_factory=list)
    warnings: list[LogIssue] = field(default_factory=list)
    components: list[ComponentScore] = field(default_factory=list)
    overall_score: int = 100
    suggested_fixes: list[str] = field(default_factory=list)
    files_scanned: int = 0
    sensitive_found: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence in the config file."""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "LogAnalysisResult":
        """Deserialize a previously stored result."""
        result = LogAnalysisResult(
            source_path=data.get("source_path", ""),
            printer_model=data.get("printer_model", ""),
            model_code=data.get("model_code", ""),
            serial_number=data.get("serial_number", ""),
            firmware_version=data.get("firmware_version", ""),
            overall_score=int(data.get("overall_score", 100)),
            suggested_fixes=list(data.get("suggested_fixes", [])),
            files_scanned=int(data.get("files_scanned", 0)),
            sensitive_found=bool(data.get("sensitive_found", False)),
        )
        result.errors = [LogIssue(**i) for i in data.get("errors", [])]
        result.warnings = [LogIssue(**i) for i in data.get("warnings", [])]
        result.components = [ComponentScore(**c) for c in data.get("components", [])]
        return result


# ------------------------------------------------------------------ parsing

# Authoritative Anycubic model-ID map (older "gk" generation: Kobra 2 Pro,
# Kobra 3 family). The printer's exported logs include api.cfg with a
# "modelId" field; this is the reliable way to identify those machines.
_MODEL_ID_TO_CODE = MODEL_ID_TO_CODE
_MODEL_ID_MAP: dict[str, str] = {
    model_id: model_name_or_code(code) for model_id, code in MODEL_ID_TO_CODE.items()
}
# Newer "avata" generation (Rockchip RV1106) identifies itself with a short
# machine_name code in its config instead of a numeric modelId.
_MODEL_CODE_MAP: dict[str, str] = {code: name for code, name in KNOWN_MODELS}
_MODEL_ID_RE = re.compile(r'"modelId"\s*:\s*"?(\d{4,6})"?')
_MACHINE_NAME_RE = re.compile(r'"machine_name"\s*:\s*"([A-Za-z0-9]+)"')
# A bare version file ("2.4.0"), or a "version: 1.2.0.6" line in a versions file.
_VERSION_FILE_RE = re.compile(r"^v?(\d+(?:\.\d+){1,3})$")
_VERSION_LINE_RE = re.compile(
    r"(?im)^[ \t]*version[ \t]*[:=][ \t]*v?(\d+(?:\.\d+){1,3})[ \t]*$"
)

_MODEL_RE = re.compile(
    r"(?:model|machine|printer)[\s:=]+\"?(Kobra[\w\s\-]*?|Photon[\w\s\-]*?|"
    r"Vyper|Mega[\w\s\-]*?|Chiron|M5[\w\s\-]*?|S1)\b\"?",
    re.IGNORECASE,
)
_FIRMWARE_RE = re.compile(
    r"(?:firmware|fw|version)[\s:=]+\"?v?(\d+\.\d+(?:\.\d+)*(?:[_\-.]\w+)?)\"?",
    re.IGNORECASE,
)
_SERIAL_RE = re.compile(r"(?:serial|sn)[\s:=]+\"?([A-Z0-9\-]{6,})\"?", re.IGNORECASE)
# Structured log level, as emitted by the avata stack: "... [error] ...".
_LOG_LEVEL_RE = re.compile(r"\[(error|warning|warn|critical|fatal)\]", re.IGNORECASE)
# Anycubic error codes are 5 digits in the 10xxx/11xxx range.
_ERROR_CODE_RE = re.compile(r"\b(1[01]\d{3})\b")
# Free-text fallback for logs without a structured level (e.g. kernel dmesg).
_ERROR_RE = re.compile(r"\b(?:error|err|fatal|critical)\b", re.IGNORECASE)
_WARNING_RE = re.compile(r"\b(?:warn|warning)\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\b(?:code[\s:=]*)?(\d{4,5})\b")
# Known-benign lines that must never count against printer health: missing
# optional config, held-mutex notices, kernel boot chatter, camera/streaming
# timeouts, probe-sampling retries, scheduler timers and informational TMC
# phase updates. These recover on their own and don't reflect hardware health.
_BENIGN_RE = re.compile(
    r"(?i)("
    r"_mutable\.cfg"
    r"|unable to open config file"
    r"|mutex '[^']*'\s*\(owner"
    r"|regulatory\.db"
    r"|squashfs"
    r"|doesn't have any ports"
    r"|failed to request dma"
    r"|agora_rtsa|read frame timeout"
    r"|sampled data exceeds the threshold"
    r"|slow timer"
    r"|schedule delay"
    r"|phase updated"
    r"|presignurl"
    r")"
)

COMPONENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "extruder": ("extruder", "nozzle", "hotend", "filament", "clog"),
    "ace": ("ace", "ace pro", "multicolor", "hub"),
    "bed": ("bed", "leveling", "level", "z-offset", "probe"),
    "temperature": ("temp", "thermal", "heater", "thermistor", "overheat"),
    "fans": ("fan", "cooling", "blower"),
    "motors": ("motor", "stepper", "driver", "axis", "endstop", "homing"),
}

FIX_HINTS: dict[str, str] = {
    "extruder": "Inspect the nozzle and extruder path for clogs; check filament feed.",
    "ace": "Re-seat the ACE unit connections and verify hub firmware is current.",
    "bed": "Re-run automatic bed leveling and verify the Z-offset.",
    "temperature": "Check heater cartridge and thermistor wiring; verify PID tuning.",
    "fans": "Clean fan blades and verify all fan connectors are seated.",
    "motors": "Check belt tension, stepper connectors and endstop wiring.",
}


class LogPackAnalyzer:
    """Unlocks, extracts and analyzes an ``AC_LOG.pack`` archive."""

    def __init__(
        self,
        api: WizzApiClient | None = None,
        password_service: PasswordService | None = None,
    ) -> None:
        self.api = api or WizzApiClient()
        self.passwords = password_service or PasswordService(self.api)

    # ------------------------------------------------------------- pipeline

    def analyze(self, pack_path: Path, progress: ProgressFn | None = None) -> LogAnalysisResult:
        """Full pipeline: passwords → unlock → extract → parse → score."""
        report = progress or (lambda _p, _t: None)

        report(5, "Fetching password database")
        passwords = self._fetch_passwords()

        report(20, "Unlocking archive")
        with tempfile.TemporaryDirectory(prefix="ac_log_") as tmp:
            extract_dir = Path(tmp)
            self._extract(pack_path, extract_dir, passwords)

            report(55, "Analyzing logs")
            result = self._parse_directory(extract_dir)

        result.source_path = str(pack_path)
        report(85, "Computing health score")
        self._score(result)
        report(100, "Done")
        return result

    # ------------------------------------------------------------ passwords

    def _fetch_passwords(self) -> list[str]:
        """Return candidate passwords from the provider chain.

        The passwords come exclusively from :class:`PasswordService`
        (wizz.se → Rinkhals → local cache); none are hardcoded here.
        """
        return self.passwords.candidate_passwords()

    # ----------------------------------------------------------- extraction

    @staticmethod
    def _extract(pack_path: Path, target: Path, passwords: list[str]) -> None:
        """Extract *pack_path* into *target*, trying each password in turn.

        Extraction is fully local. An unencrypted archive is handled by the
        initial ``None`` attempt (the absence of a password, not a hardcoded
        one); every other candidate comes from the provider chain.
        """
        if not zipfile.is_zipfile(pack_path):
            raise ValueError(
                f"'{pack_path.name}' is not a recognized AC_LOG.pack (ZIP) archive."
            )

        # Try no password first (unencrypted archive), then each candidate.
        attempts: list[bytes | None] = [None]
        for password in passwords:
            encoded = password.encode("utf-8") if password else None
            if encoded not in attempts:
                attempts.append(encoded)

        with zipfile.ZipFile(pack_path) as archive:
            last_error: Exception | None = None
            for pwd in attempts:
                try:
                    archive.extractall(target, pwd=pwd)
                    return
                except (RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
                    last_error = exc
                    continue
            if not passwords:
                raise ValueError(
                    "The archive is password-protected and no password database "
                    "is available. Connect to the internet so the database can be "
                    "downloaded, then try again."
                )
            raise ValueError(
                "Could not unlock the archive with any known password. "
                f"({last_error})" if last_error else "Could not unlock the archive."
            )

    # -------------------------------------------------------------- parsing

    def _parse_directory(self, directory: Path) -> LogAnalysisResult:
        result = LogAnalysisResult()
        files = [
            f
            for f in sorted(directory.rglob("*"))
            if f.is_file() and f.suffix.lower() in TEXT_EXTENSIONS
        ]

        # Pass 1 — authoritative identity from the printer's own config files.
        texts: dict[Path, str] = {}
        for file in files:
            try:
                texts[file] = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self._detect_identity(file.name, texts[file], result)
            if not result.sensitive_found and contains_sensitive(texts[file]):
                result.sensitive_found = True

        # Pass 2 — scan lines for real errors/warnings (identity only as a gap-fill).
        seen: set[tuple[str, str, str]] = set()
        for file, text in texts.items():
            result.files_scanned += 1
            self._scan_lines(file.name, text, result, seen)
        return result

    def _scan_lines(
        self,
        name: str,
        text: str,
        result: LogAnalysisResult,
        seen: set[tuple[str, str, str]],
    ) -> None:
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not result.printer_model:
                match = _MODEL_RE.search(line)
                if match:
                    result.printer_model = f"Anycubic {match.group(1).strip()}"
            if not result.firmware_version:
                match = _FIRMWARE_RE.search(line)
                if match:
                    result.firmware_version = match.group(1)
            if not result.serial_number:
                match = _SERIAL_RE.search(line)
                if match:
                    result.serial_number = match.group(1)

            severity = self._classify_line(line)
            if severity is None:
                continue
            issue = self._issue(severity, name, line_no, line)
            # Collapse recurring identical faults: normalize away the leading
            # timestamp, bracketed pid/line tags and digits so the same fault at
            # different times counts once.
            norm = re.sub(r"^\[[^\]]*\]\s*", "", line)
            norm = re.sub(r"\[\d+\]", "", norm)
            norm = re.sub(r"\d+", "#", norm).strip()[:120]
            key = (issue.severity, norm)
            if key in seen:
                continue
            seen.add(key)
            (result.errors if severity == "error" else result.warnings).append(issue)

    @staticmethod
    def _classify_line(line: str) -> str | None:
        """Classify a log line as ``"error"``, ``"warning"`` or ``None``.

        Real Anycubic logs are noisy: kernel boot messages, missing optional
        config files and held-mutex notices are all benign. Health should
        reflect genuine faults, so this:

        * ignores known-benign lines outright;
        * trusts the structured ``[error]``/``[warning]`` level when present;
        * for unstructured lines (kernel dmesg), counts them only when they
          carry an Anycubic error code (10xxx/11xxx).
        """
        if _BENIGN_RE.search(line):
            return None
        level = _LOG_LEVEL_RE.search(line)
        if level:
            tag = level.group(1).lower()
            return "error" if tag in ("error", "critical", "fatal") else "warning"
        # No error/warning level tag (info/debug or kernel dmesg): count only
        # when the line explicitly reports a fault carrying an error code.
        lowered = line.lower()
        if _ERROR_CODE_RE.search(line) and any(
            token in lowered for token in ("err", "fail", "exception", "fault")
        ):
            return "error"
        return None

    @staticmethod
    def _detect_identity(name: str, text: str, result: LogAnalysisResult) -> None:
        """Detect model/firmware/serial from known config files in the pack.

        Anycubic's log export bundles the printer's own config, so these are
        far more reliable than scanning free-form log lines. Two firmware
        generations are handled:

        * "gk" generation (Kobra 2 Pro / Kobra 3 family): ``api.cfg`` carries
          ``"modelId": "20024"`` — mapped via :data:`_MODEL_ID_MAP` — and a bare
          ``version`` file holds the firmware version.
        * "avata" generation (Rockchip RV1106): the config carries
          ``"machine_name": "K4P"`` — mapped via :data:`_MODEL_CODE_MAP` — and a
          ``versions`` file holds a ``version: 1.2.0.6`` line.

        A ``device_id`` file, when present, provides the serial/device id.
        """
        lname = name.lower()

        if not result.printer_model:
            id_match = _MODEL_ID_RE.search(text)
            if id_match:
                model_id = id_match.group(1)
                result.printer_model = _MODEL_ID_MAP.get(
                    model_id, f"Anycubic printer (model ID {model_id})"
                )
                result.model_code = _MODEL_ID_TO_CODE.get(model_id, "")
            else:
                name_match = _MACHINE_NAME_RE.search(text)
                if name_match:
                    code = name_match.group(1).upper()
                    result.printer_model = _MODEL_CODE_MAP.get(
                        code, f"Anycubic printer ({code})"
                    )
                    result.model_code = code

        if not result.firmware_version and lname in ("version", "versions"):
            line_match = _VERSION_LINE_RE.search(text)
            if line_match:
                result.firmware_version = line_match.group(1)
            else:
                first = text.strip().splitlines()[0].strip() if text.strip() else ""
                bare = _VERSION_FILE_RE.match(first)
                if bare:
                    result.firmware_version = bare.group(1)

        if not result.serial_number and lname in ("device_id", "sn", "serial"):
            first = text.strip().splitlines()[0].strip() if text.strip() else ""
            if first:
                result.serial_number = first[:64]

    @staticmethod
    def _issue(severity: str, file: str, line_no: int, line: str) -> LogIssue:
        code_match = _ERROR_CODE_RE.search(line)
        return LogIssue(
            severity=severity,
            code=code_match.group(1) if code_match else "",
            message=redact_sensitive(line.strip())[:300],
            source_file=file,
            line_number=line_no,
        )

    # -------------------------------------------------------------- scoring

    @staticmethod
    def _score(result: LogAnalysisResult) -> None:
        """Derive component and overall health scores from found issues."""
        counts: dict[str, int] = {component: 0 for component in COMPONENT_KEYWORDS}
        for issue in result.errors + result.warnings:
            weight = 3 if issue.severity == "error" else 1
            lowered = issue.message.lower()
            for component, keywords in COMPONENT_KEYWORDS.items():
                if any(keyword in lowered for keyword in keywords):
                    counts[component] += weight

        result.components = [
            ComponentScore(
                component=component,
                score=max(0, 100 - min(count, 20) * 5),
                issues=count,
            )
            for component, count in counts.items()
        ]

        # Overall health reflects the subsystems: the mean of component scores,
        # nudged down slightly when many distinct faults appear. This isolates a
        # single bad subsystem (e.g. the ACE unit) instead of collapsing to zero.
        if result.components:
            base = sum(c.score for c in result.components) / len(result.components)
        else:
            base = 100.0
        spread_penalty = min(len(result.errors), 15)
        result.overall_score = max(0, min(100, round(base - spread_penalty)))

        result.suggested_fixes = [
            FIX_HINTS[c.component]
            for c in sorted(result.components, key=lambda c: c.score)
            if c.score < 90 and c.component in FIX_HINTS
        ]
        if not result.suggested_fixes and result.overall_score < 100:
            result.suggested_fixes.append(
                "Review the listed errors and warnings; no single subsystem stands out."
            )
