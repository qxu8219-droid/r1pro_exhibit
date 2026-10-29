"""Validate an explicit robot-clock minus viewer-clock offset in seconds."""
import math


def robot_clock_offset(value):
    """Accept CLI numeric text or finite numbers, never boolean clock settings."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("robot clock offset must be a finite number between -300 and 300 seconds")
    try:
        offset = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("robot clock offset must be a finite number between -300 and 300 seconds") from exc
    if not math.isfinite(offset) or abs(offset) > 300:
        raise ValueError("robot clock offset must be a finite number between -300 and 300 seconds")
    return offset
