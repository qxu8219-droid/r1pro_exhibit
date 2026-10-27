"""Read-only, offline package integrity check. Python 3.9+, no dependencies."""
import hashlib
from pathlib import Path, PurePosixPath
import sys


def main():
    if sys.version_info < (3, 9):
        print("FAIL: Python 3.9+ is required.")
        return 1
    root = Path(__file__).resolve().parent
    print("R1 exhibition package / offline check")
    print("Python: " + sys.version.split()[0])
    try:
        import sqlite3
        import http.server
        import webbrowser
        with sqlite3.connect(":memory:") as db:
            assert db.execute("SELECT 1").fetchone() == (1,)
        manifest = (root / "SHA256SUMS.txt").read_text(encoding="utf-8")
        count = 0
        for line in manifest.splitlines():
            digest, name = line.split("  ", 1)
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                raise ValueError("Invalid manifest path")
            path = root.joinpath(*relative.parts)
            if root not in path.resolve().parents:
                raise ValueError("File is outside the package")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("File changed or incomplete: " + name)
            count += 1
        if not count:
            raise ValueError("Empty manifest")
    except (OSError, ValueError, ImportError, AssertionError) as exc:
        print("FAIL: " + str(exc))
        print("Extract the original ZIP again into a new writable folder.")
        return 1
    print("PASS: %d original files intact; standard-library runtime ready." % count)
    print("No service or robot connection was started. New local simulation data is ignored.")
    print("Next: open START_HERE.html, then run.bat (Windows) or sh run.sh (Linux/Mac).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
