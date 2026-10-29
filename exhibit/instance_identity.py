"""Identify local exhibit instances without exposing their filesystem paths."""
import hashlib
import json
import os
from pathlib import Path
from robot_clock import robot_clock_offset as validate_robot_clock_offset


def _path_identity(path):
    return os.path.normcase(str(Path(path).resolve()))


def instance_identity(root, db_path, fleet_size, telemetry_file=None, vla_db=None, robot_clock_offset=0.0):
    """Hash the actual local root and configuration; never read the named files."""
    robot_clock_offset = validate_robot_clock_offset(robot_clock_offset)
    value = {
        "schema": 1,
        "root": _path_identity(root),
        "db": ":memory:" if str(db_path) == ":memory:" else _path_identity(db_path),
        "fleet_size": fleet_size,
        "telemetry": None if telemetry_file is None else _path_identity(telemetry_file),
    }
    # Keep camera-only and recording instance digests backward compatible.
    if vla_db is not None:
        value["vla_db"] = _path_identity(vla_db)
    if robot_clock_offset:
        value["robot_clock_offset_s"] = robot_clock_offset
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
