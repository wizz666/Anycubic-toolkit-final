"""Module entry point.

Allows the application to be started with ``python -m anycubic_toolkit``.
"""

from __future__ import annotations

import sys

from anycubic_toolkit.app import main

if __name__ == "__main__":
    sys.exit(main())
