"""Allow ``python -m pcbrouter``."""

from __future__ import annotations

import sys

from pcbrouter.app.application import main

if __name__ == "__main__":
    sys.exit(main())
