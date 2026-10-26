from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from anycubic_toolkit.core.logpack import LogPackAnalyzer


class _EmptyPasswords:
    def candidate_passwords(self) -> list[str]:
        return []


def _analyzer() -> LogPackAnalyzer:
    return LogPackAnalyzer(api=object(), password_service=_EmptyPasswords())


def test_extract_to_unencrypted_pack(tmp_path: Path) -> None:
    pack = tmp_path / "AC_LOG.pack"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr("kernel.log", "boot ok\n")
        archive.writestr("api.cfg", '{"modelId": "20024"}\n')

    dest = tmp_path / "out"
    result = _analyzer().extract_to(pack, dest)

    assert result == dest
    assert (dest / "kernel.log").read_text(encoding="utf-8") == "boot ok\n"
    assert '"modelId"' in (dest / "api.cfg").read_text(encoding="utf-8")


def test_extract_to_rejects_non_zip(tmp_path: Path) -> None:
    pack = tmp_path / "AC_LOG.pack"
    pack.write_bytes(b"not a zip")

    with pytest.raises(ValueError, match="not a recognized AC_LOG.pack"):
        _analyzer().extract_to(pack, tmp_path / "out")
