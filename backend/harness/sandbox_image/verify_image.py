"""Check that the image has everything rebuild and document tasks need. No network is used."""
from __future__ import annotations

import importlib.metadata
import os
import pathlib
import shutil
import subprocess
import sys

PINS = pathlib.Path(__file__).with_name("requirements.txt")
IMPORT_NAMES = {"scikit-learn": "sklearn", "python-docx": "docx"}
R_PACKAGES = ("readxl", "digest", "jsonlite", "renv")
COMMANDS = ("Rscript", "python", "find", "realpath", "tee", "head", "timeout", "mkdir", "diff")
FIXED_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "2", "PYTHONHASHSEED": "0"}


def main() -> int:
    problems: list[str] = []
    for line in PINS.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        try:
            installed = importlib.metadata.version(name)
            if installed != version:
                problems.append(f"{name}: wanted {version}, found {installed}")
            __import__(IMPORT_NAMES.get(name, name))
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
    for name, value in FIXED_ENV.items():
        if os.environ.get(name) != value:
            problems.append(f"{name}: wanted {value}, found {os.environ.get(name)}")
    for command in COMMANDS:
        if shutil.which(command) is None:
            problems.append(f"missing command: {command}")
    for package in R_PACKAGES:
        check = subprocess.run(
            ["Rscript", "-e", f"library({package}); cat(as.character(packageVersion('{package}')))"],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            problems.append(f"R package {package}: {check.stderr.strip()[:200]}")
        else:
            print(f"R {package} {check.stdout.strip()}")
    print(f"python {sys.version.split()[0]}")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
