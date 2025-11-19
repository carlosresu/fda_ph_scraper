#!/usr/bin/env python3
"""Helper that installs the FDA_PH_SCRAPER requirements into the active environment."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    req_file = Path(__file__).resolve().with_name("requirements.txt")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req_file)], check=True)


if __name__ == "__main__":
    main()
