"""Atomic alert checkpoints and exclusive access to a state file."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from models import Quote, as_decimal

STATE_PATH = Path(os.getenv("STATE_PATH", "data/state.json"))


def load_state(path: Path | None = None) -> dict:
    p = path or STATE_PATH
    if not p.exists():
        return {"version": 1, "alerts": {}}
    with p.open(encoding="utf-8") as fp:
        value = json.load(fp)
    if not isinstance(value, dict) or not isinstance(value.get("alerts"), dict):
        raise ValueError("Invalid state file.")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("Unsupported state file version.")
    for key, entry in value["alerts"].items():
        if (not isinstance(entry, dict)
            or not isinstance(entry.get("signature"), str)
            or not isinstance(entry.get("last_marker"), str)):
            raise ValueError(f"Invalid state entry for {key}.")
        as_decimal(entry.get("last_rate"), f"Saved rate for {key}")
    return value


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=p.parent, prefix=".state-", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(p)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


def checkpoint(state: dict, key: str, sig: str, current: Quote, path: Path | None) -> None:
    """Publish an in-memory checkpoint only after it has been persisted."""
    alerts = dict(state["alerts"])
    alerts[key] = {"signature": sig, "last_marker": current.marker, "last_rate": str(current.rate)}
    save_state({**state, "alerts": alerts}, path)
    state["alerts"] = alerts


@contextmanager
def state_lock(path: Path | None = None) -> Iterator[None]:
    """Hold a process lock across state loading and all monitoring cycles."""
    target = path or STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Another monitor is using this state file.") from None
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another monitor is using this state file.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
