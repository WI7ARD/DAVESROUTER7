"""PySide6 user interface.

The UI depends on the core packages; nothing outside ``pcbrouter.ui`` and
``pcbrouter.app`` may import Qt, so the core stays testable and usable headless
(CLI, future scripting API, future worker processes).
"""
