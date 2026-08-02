"""Anycubic Toolkit.

An open-source desktop utility for owners of Anycubic 3D printers.

This package contains the complete application: core services (configuration,
translations, theming, networking, plugin management, log-pack handling) and
the PySide6 user interface.
"""

from __future__ import annotations

__app_name__: str = "Anycubic Toolkit"
__version__: str = "0.3.0"
__author__: str = "Wizz"
__license__: str = "MIT"
__homepage__: str = "https://github.com/wizz666/Anycubic-toolkit-final"
__api_base__: str = "https://wizz.se/wp-json/anycubic-toolkit/v1"
__sponsor_github__: str = "https://github.com/sponsors/wizz666"
__sponsor_kofi__: str = "https://ko-fi.com/wizz666"
__anycubic_wiki__: str = "https://wiki.anycubic.com"
__anycubic_error_codes__: str = "https://wiki.anycubic.com/en/error-codes"
__anycubic_firmware__: str = "https://eu.anycubic.com/pages/firmware-software"
__makeronline__: str = "https://www.makeronline.com/en/"
# Rinkhals doesn't support the Kobra X yet (RSA-signed firmware, not cracked),
# so there is no official structured catalog for it. This community-shared
# folder is the only alternative to Anycubic's own site; the app never
# downloads from it automatically and never verifies its contents.
__kobra_x_community_firmware__: str = (
    "https://drive.google.com/drive/folders/1RhOhEmEnSxnshcnQUwMOMGBashxQByF9"
)
