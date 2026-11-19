"""Process liveness and dependency readiness for the standalone relay role."""

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

from faststream.kafka import KafkaBroker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.settings import get_settings
from app.infrastructure.di import create_container


def record_state(path: Path, *, ready: bool) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "ready": ready}))
    temporary.replace(path)


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        os.kill(pid, 0)
        return True

    # Windows os.kill(pid, 0) terminates the process rather than probing it.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        status = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(status))) and (
            status.value == 259  # STILL_ACTIVE
        )
    finally:
        kernel.CloseHandle(handle)


def _process_state(path: Path) -> tuple[bool, bool]:
    try:
        state = json.loads(path.read_text())
        pid = state["pid"]
        if type(pid) is not int or pid <= 0:
            return False, False
        return _process_alive(pid), state["ready"] is True
    except OSError, ValueError, KeyError, TypeError:
        return False, False


async def _dependencies_ready() -> bool:
    container = create_container()
    try:
        async with asyncio.timeout(3):
            engine = await container.get(AsyncEngine)
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            broker = await container.get(KafkaBroker)
            try:
                async with broker:
                    return await broker.ping(timeout=1)
            finally:
                await broker.stop()
    except Exception:
        return False
    finally:
        await container.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--ready", action="store_true")
    args = parser.parse_args(argv)
    alive, initialized = _process_state(get_settings().outbox.OUTBOX_HEALTH_STATE_PATH)
    if args.live:
        return 0 if alive else 1
    return 0 if alive and initialized and asyncio.run(_dependencies_ready()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
