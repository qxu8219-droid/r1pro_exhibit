"""Identify local exhibit instances without exposing their filesystem paths."""
import hashlib
import json
import os
from pathlib import Path


def _path_identity(path):
    return os.path.normcase(str(Path(path).resolve()))


def instance_identity(root, db_path, fleet_size, telemetry_file=None):
    """Hash the actual local root and configuration; never read the named files."""
    value = {
        "schema": 1,
        "root": _path_identity(root),
        "db": ":memory:" if str(db_path) == ":memory:" else _path_identity(db_path),
        "fleet_size": fleet_size,
        "telemetry": None if telemetry_file is None else _path_identity(telemetry_file),
    }
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
