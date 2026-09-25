"""Probe for a usable GPU device (the gate every GPU job runs first).

    python tools/gpu_probe.py [--github-output PATH]

Always exits 0: no GPU means the GPU job is SKIPPED, not failed.
"""

from pcbrouter.compute.probe import main

if __name__ == "__main__":
    raise SystemExit(main())
