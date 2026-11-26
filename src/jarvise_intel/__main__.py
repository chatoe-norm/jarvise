"""python -m jarvise_intel.app | egress entrypoints."""

from __future__ import annotations

import sys


def main() -> None:
    role = (sys.argv[1] if len(sys.argv) > 1 else "ingest").strip().lower()
    if role == "egress":
        from jarvise_intel.egress import main as egress_main

        egress_main()
        return
    from jarvise_intel.app import main as ingest_main

    ingest_main()


if __name__ == "__main__":
    main()
