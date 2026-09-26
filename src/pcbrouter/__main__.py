"""Allow ``python -m pcbrouter`` (and the frozen executables' entry point)."""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # Must run first: in the frozen Windows build the routing worker process is
    # this same executable, started by multiprocessing ("spawn"); freeze_support()
    # turns that invocation into the worker instead of a second GUI.
    multiprocessing.freeze_support()
    from pcbrouter.app.application import main

    sys.exit(main())
