"""Enable `python -m mailflow ...` — the always-works equivalent of the `mailflow`
command (no PATH entry-point needed). Delegates to the same CLI."""

from __future__ import annotations

import sys

from mailflow.cli import main

if __name__ == "__main__":
    sys.exit(main())
