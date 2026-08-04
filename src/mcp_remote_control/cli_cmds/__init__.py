"""CLI command implementations (harness-facing; keep :mod:`cli` thin).

Shared process exit codes used by doctor, selftest, replay, and tool shells.
"""

from __future__ import annotations

# Process exit codes: harness truth via status (0 ok, 2 usage, 3 validation, 4 transport).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_TRANSPORT = 4

__all__ = [
    "EXIT_OK",
    "EXIT_TRANSPORT",
    "EXIT_USAGE",
    "EXIT_VALIDATION",
]
