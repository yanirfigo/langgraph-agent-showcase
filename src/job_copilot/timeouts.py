"""Run a call with a hard timeout in a daemon thread.

Unlike a ThreadPoolExecutor, a daemon thread that is still stuck when the caller gives up does not
keep the process alive at exit, so a hung request can't block the next scheduled run.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


def run_with_timeout[T](fn: Callable[[], T], timeout: float) -> T:
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"gave up after {timeout:.0f} s")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]  # type: ignore[return-value]
