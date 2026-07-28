#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    path = args.path.resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    destination = path.with_name(path.name + ".sha256")
    destination.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
