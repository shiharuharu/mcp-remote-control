"""Atomic-write temp-name helper shared by FS backends.

Atomic write is same-directory temp + replace. The three backends share
only the sibling basename formula; permission copy, promote, and cleanup
stay at each write path.
"""

from __future__ import annotations

import os
import threading


def mrc_tmp_name(basename: str) -> str:
    """Return a same-directory sibling temp name for atomic write/replace.

    Formula: ``.{basename}.mrc-tmp-{pid}-{tid}``. Callers supply the
    destination basename only (empty-name fallbacks stay at the call site).
    """
    return f".{basename}.mrc-tmp-{os.getpid()}-{threading.get_ident()}"
