"""Re-run the calling script under .venv when it was started without it.

The dependencies live in .venv, but `python3 rbd_import.py` and an IDE's Run
button both default to the system interpreter, where the imports fail with a
bare ModuleNotFoundError that says nothing about virtualenvs. Rather than
leave that trap, each entry point calls use_venv() before importing anything
third-party and is re-executed under the right interpreter.

It says so on stderr rather than switching silently, and does nothing at all
if there is no .venv or we are already running under it.
"""

import os
import sys
from pathlib import Path


def use_venv():
    root = Path(__file__).resolve().parent
    venv_dir = root / ".venv"
    python = venv_dir / "bin" / "python"
    if not python.exists():
        return  # no virtualenv here: let the normal ImportError speak for itself

    # Compare sys.prefix, not the interpreter path. .venv/bin/python is a
    # symlink to the system interpreter, so resolving both paths makes them
    # compare equal and the re-exec never happens.
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return

    print(f"note: re-running under {python}", file=sys.stderr)
    os.execv(str(python), [str(python), *sys.argv])
