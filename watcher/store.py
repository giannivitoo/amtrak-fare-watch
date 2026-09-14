"""
Price history, kept as one JSONL file per watched trip.

Append-only and committed back to the repo, so the history survives between
runs on a stateless CI runner and you end up with a real dataset of how
Amtrak's buckets move on the routes you actually ride.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HISTORY_DIR = Path(__file__).resolve().parent.parent / "history"


def _slug(watch_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in watch_id)


def path_for(watch_id: str) -> Path:
    return HISTORY_DIR / f"{_slug(watch_id)}.jsonl"


@dataclass
class Snapshot:
    """One observation of a watched trip."""
    checked_at: str
    lowest_price: float | None
    lowest_label: str | None      # "Coach Value on #111 at 4:50a"
    lowest_train: str | None
    lowest_seats: int | None
    trains: list[dict]            # full per-train detail

    def to_json(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))


def append(watch_id: str, snap: Snapshot) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with path_for(watch_id).open("a") as fh:
        fh.write(snap.to_json() + "\n")


def read_all(watch_id: str) -> list[dict]:
    p = path_for(watch_id)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def last(watch_id: str) -> dict | None:
    rows = read_all(watch_id)
    return rows[-1] if rows else None


def observed_low(watch_id: str) -> float | None:
    """Cheapest price ever seen for this trip."""
    prices = [
        r["lowest_price"]
        for r in read_all(watch_id)
        if r.get("lowest_price") is not None
    ]
    return min(prices) if prices else None


def prune(watch_id: str) -> None:
    """Delete history for a trip that is no longer watched."""
    p = path_for(watch_id)
    if p.exists():
        p.unlink()


# --- alert cooldown -------------------------------------------------------

STATE_FILE = HISTORY_DIR / "_alert_state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_state(state: dict) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def should_alert(watch_id: str, price: float, cooldown_minutes: int) -> bool:
    """True unless we already alerted at this price or better, recently."""
    state = _load_state()
    entry = state.get(watch_id)
    if not entry:
        return True
    if price < entry.get("price", float("inf")) - 0.001:
        return True  # a new, lower price always earns an alert
    last_at = datetime.fromisoformat(entry["at"])
    age_min = (datetime.now(timezone.utc) - last_at).total_seconds() / 60
    return age_min >= cooldown_minutes


def record_alert(watch_id: str, price: float) -> None:
    state = _load_state()
    state[watch_id] = {
        "price": price,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)


# --- polling cadence ------------------------------------------------------

LAST_POLL_FILE = HISTORY_DIR / "_last_poll.json"


def due_for_poll(watch_id: str, interval_minutes: int) -> bool:
    if os.environ.get("AMTRAK_FORCE_POLL") == "1":
        return True
    if not LAST_POLL_FILE.exists():
        return True
    try:
        state = json.loads(LAST_POLL_FILE.read_text())
    except json.JSONDecodeError:
        return True
    stamp = state.get(watch_id)
    if not stamp:
        return True
    age_min = (
        datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    ).total_seconds() / 60
    return age_min >= interval_minutes


def mark_polled(watch_ids: list[str]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    state = {}
    if LAST_POLL_FILE.exists():
        try:
            state = json.loads(LAST_POLL_FILE.read_text())
        except json.JSONDecodeError:
            pass
    now = datetime.now(timezone.utc).isoformat()
    for wid in watch_ids:
        state[wid] = now
    LAST_POLL_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
