"""Read-only exhibition evidence. No robot/network clients or third-party imports."""
import base64
import binascii
import json
import math
import re
import stat
import time
from pathlib import Path


MAX_REPLAY_BYTES = 32 * 1024 * 1024
MAX_TELEMETRY_BYTES = 8 * 1024 * 1024
MAX_IMAGE_CHARS = 2 * 1024 * 1024
FRESH_SECONDS = 5.0
_DATA_START = re.compile(r"\bconst\s+DATA\s*=\s*")


class EvidenceError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _check_finite(value):
    # JSONDecoder rejects literal NaN/Infinity separately; 1e999 also needs checking.
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError("Non-finite JSON number")
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _read_bounded(path, limit):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError("INVALID_FILE", 400)
    if info.st_size > limit:
        raise EvidenceError("TOO_LARGE", 413)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise EvidenceError("TOO_LARGE", 413)
    return raw.decode("utf-8")


def read_recording(replay_dir, filename):
    """Decode the generated DATA JSON without evaluating any HTML or JavaScript."""
    if (not isinstance(filename, str) or not filename or len(filename) > 255
            or filename in (".", "..") or not filename.endswith(".html")
            or any(c in filename for c in ("/", "\\", ":", "\x00"))):
        raise EvidenceError("INVALID_RECORDING", 400)
    root = Path(replay_dir).resolve()
    try:
        asset = (root / filename).resolve()
        if asset.parent != root:
            raise EvidenceError("INVALID_RECORDING", 400)
        source = _read_bounded(asset, MAX_REPLAY_BYTES)
    except EvidenceError:
        raise
    except FileNotFoundError:
        raise EvidenceError("RECORDING_NOT_FOUND", 404)
    except (OSError, UnicodeError, RuntimeError):
        raise EvidenceError("RECORDING_UNAVAILABLE", 503)
    match = _DATA_START.search(source)
    if not match:
        raise EvidenceError("INVALID_RECORDING_DATA", 422)
    try:
        data, end = json.JSONDecoder(parse_constant=_reject_constant).raw_decode(source, match.end())
        if not isinstance(data, dict) or not source[end:].lstrip().startswith(";"):
            raise ValueError("Expected DATA object followed by semicolon")
        _check_finite(data)
    except (ValueError, RecursionError):
        raise EvidenceError("INVALID_RECORDING_DATA", 422)
    return {"source": "recording", "file": filename, "data": data}


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Invalid text field")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("Control character in text")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Invalid number")
    return value


def _image(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE_CHARS:
        raise ValueError("Invalid image")
    prefixes = {"data:image/png;base64,": b"\x89PNG\r\n\x1a\n", "data:image/jpeg;base64,": b"\xff\xd8\xff"}
    for prefix, signature in prefixes.items():
        if value.startswith(prefix):
            try:
                raw = base64.b64decode(value[len(prefix):], validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid base64 image")
            if not raw.startswith(signature):
                raise ValueError("Image signature mismatch")
            return value
    raise ValueError("Only inline PNG/JPEG images are accepted")


def _telemetry_state(data):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported schema")
    state = {"schema_version": 1}
    for key, limit in (("robot_id", 128), ("run_id", 128), ("task", 1000), ("model_id", 256), ("stage", 256)):
        state[key] = _text(data.get(key), limit)
    state["observed_at"] = _number(data.get("observed_at"))
    if state["observed_at"] <= 0:
        raise ValueError("Invalid timestamp")
    images = data.get("images", {})
    if not isinstance(images, dict):
        raise ValueError("Invalid images")
    state["images"] = {key: _image(images[key]) for key in ("head", "wrist_left", "wrist_right") if key in images}
    if "request_id" in data:
        request_id = data["request_id"]
        if type(request_id) is int and 0 <= request_id <= 9007199254740991:
            state["request_id"] = request_id
        else:
            state["request_id"] = _text(request_id, 128)
    if "rtt_ms" in data:
        rtt = _number(data["rtt_ms"])
        if not 0 <= rtt <= 3600000:
            raise ValueError("Invalid RTT")
        state["rtt_ms"] = rtt
    return state


def read_telemetry(telemetry_file=None, now=None):
    """Read a local producer's JSON snapshot, never mutate it or initiate a connection.

    Missing/invalid data produces a neutral state. Stale/future snapshots retain
    their identity and observation time but are explicitly disconnected.
    """
    result = {"source": "live", "configured": telemetry_file is not None,
              "connected": False, "status": "unconfigured", "age_s": None, "state": None}
    if telemetry_file is None:
        return result
    try:
        raw = _read_bounded(Path(telemetry_file), MAX_TELEMETRY_BYTES)
        data = json.loads(raw, parse_constant=_reject_constant)
        _check_finite(data)
        state = _telemetry_state(data)
        age = _number(time.time() if now is None else now) - state["observed_at"]
        result.update(state=state, age_s=round(age, 3),
                      connected=0 <= age <= FRESH_SECONDS,
                      status="connected" if 0 <= age <= FRESH_SECONDS else "stale")
    except FileNotFoundError:
        result["status"] = "missing"
    except EvidenceError as exc:
        result["status"] = "too_large" if exc.code == "TOO_LARGE" else "invalid"
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        result["status"] = "invalid"
    return result
