"""Minimal entry point for the detached Bridge process."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, cast


def _startup_marker(stage: str) -> None:
    with suppress(OSError):
        os.write(2, f"gsv Bridge worker: {stage}\n".encode("ascii"))


def _detach_if_requested() -> None:
    if os.environ.pop("GSV_BRIDGE_DETACH_IN_CHILD", None) != "1":
        return

    import resource

    resource_api = cast(Any, resource)
    soft_limit, _ = resource_api.getrlimit(resource_api.RLIMIT_NOFILE)
    if soft_limit == resource_api.RLIM_INFINITY:
        soft_limit = 1_048_576
    os.closerange(3, int(soft_limit))
    cast(Any, os).setsid()
    _startup_marker("detached")


def main(arguments: Sequence[str] | None = None) -> int:
    _startup_marker("entered")
    _detach_if_requested()

    import argparse

    from continuity_kernel.bridge import serve_bridge
    from continuity_kernel.vault import Vault

    _startup_marker("imports ready")

    parser = argparse.ArgumentParser(prog="gsv-bridge-worker")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args(arguments)
    _startup_marker("serving")
    serve_bridge(Vault(args.vault), port=args.port, instance_id=args.instance_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
