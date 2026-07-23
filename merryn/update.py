"""Version comparison for the optional update-notification check.

Kept free of any Discord or network dependency so the comparison logic can
be unit-tested. The network fetch and owner notification live in main.py.
"""
from __future__ import annotations

import re

_CORE = re.compile(r"\d+(?:\.\d+)*")


def version_tuple(value: str) -> tuple[int, ...] | None:
    """The leading dotted-number core of a version/tag as an int tuple.

    Tolerates a leading ``v`` and any pre-release/build suffix (only the
    numeric release is compared). Returns None if there is no number to
    read, so a malformed tag never triggers a spurious notification.
    """
    match = _CORE.match(value.strip().lstrip("vV"))
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def is_newer(latest: str, current: str) -> bool:
    """True when release tag `latest` is a newer version than `current`.

    Either argument may carry a leading ``v``. Unparseable input yields
    False — when in doubt, stay quiet rather than nag.
    """
    latest_t = version_tuple(latest)
    current_t = version_tuple(current)
    if latest_t is None or current_t is None:
        return False
    width = max(len(latest_t), len(current_t))
    latest_t += (0,) * (width - len(latest_t))
    current_t += (0,) * (width - len(current_t))
    return latest_t > current_t
