"""
The polling run. Executed by GitHub Actions on a schedule.

Each run:
  1. reads watchlist.json
  2. retires any trip whose departure date has passed
  3. works out which trips are due a check (more often as departure nears)
  4. fetches current fares for those
  5. compares against the last check and the all-time low
  6. pushes a notification when a fare drops enough, or hits your target
  7. appends the observation to history/ and rebuilds the dashboard data
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import amtrak, notify, store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.json"
DASHBOARD_DATA = ROOT / "docs" / "data.json"

DEFAULTS = {
    "passengers": 1,
    "classes": ["Coach"],          # [] or ["*"] means every class
    "alert_drop_pct": 15.0,        # alert on a drop of at least this %
    "alert_drop_abs": 20.0,        # ...or at least this many dollars
    "target_price": None,          # ...or any time it is at or below this
    "cooldown_minutes": 180,       # do not re-alert the same price for this long
    "depart_after": None,          # "06:00" to ignore overnight departures
    "depart_before": None,
    "cadence": [
        {"within_hours": 48, "every_minutes": 15},
        {"within_hours": 336, "every_minutes": 60},
        {"within_hours": None, "every_minutes": 360},
    ],
}

LOG: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG.append(msg)


def load_watchlist() -> tuple[dict, list[dict]]:
    if not WATCHLIST.exists():
        log(f"No watchlist at {WATCHLIST}")
        return {}, []
    raw = json.loads(WATCHLIST.read_text())
    defaults = {**DEFAULTS, **(raw.get("defaults") or {})}
    return defaults, raw.get("watches") or []


def settings_for(watch: dict, defaults: dict) -> dict:
    s = {**defaults}
    for key in DEFAULTS:
        if key in watch:
            s[key] = watch[key]
    return s


def hours_until(date_str: str) -> float:
    """Hours from now until midnight of the departure date, local-ish.

    Treating the trip as departing at end of day keeps day-of trains in
    the fastest polling tier right up until the date rolls over.
    """
    dep = datetime.fromisoformat(date_str).replace(
        hour=23, minute=59, tzinfo=timezone.utc
    )
    return (dep - datetime.now(timezone.utc)).total_seconds() / 3600


def interval_for(hours: float, cadence: list[dict]) -> int:
    for tier in cadence:
        limit = tier.get("within_hours")
        if limit is None or hours <= limit:
            return int(tier["every_minutes"])
    return int(cadence[-1]["every_minutes"])


def matches_filters(option: amtrak.TrainOption, s: dict) -> bool:
    if s.get("depart_after") or s.get("depart_before"):
        try:
            t = datetime.fromisoformat(option.depart).time()
        except ValueError:
            return True
        if s.get("depart_after"):
            hh, mm = (int(x) for x in s["depart_after"].split(":"))
            if (t.hour, t.minute) < (hh, mm):
                return False
        if s.get("depart_before"):
            hh, mm = (int(x) for x in s["depart_before"].split(":"))
            if (t.hour, t.minute) > (hh, mm):
                return False
    return True


def wanted_classes(s: dict) -> set[str] | None:
    classes = s.get("classes") or []
    if not classes or "*" in classes:
        return None
    return {c.lower() for c in classes}


def cheapest(options: list[amtrak.TrainOption], s: dict):
    """Lowest qualifying fare across every train on the date."""
    want = wanted_classes(s)
    best = None
    for opt in options:
        if not matches_filters(opt, s):
            continue
        for fare in opt.fares:
            if want and fare.travel_class.lower() not in want:
                continue
            if best is None or fare.price < best[1].price:
                best = (opt, fare)
    return best


def fmt_time(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return t.strftime("%-I:%M%p").lower().replace("pm", "p").replace("am", "a")


def run() -> int:
    defaults, watches = load_watchlist()
    if not watches:
        log("Watchlist is empty. Add trips to watchlist.json.")
        return 0

    today = datetime.now(timezone.utc).date()
    active, expired = [], []
    for w in watches:
        if datetime.fromisoformat(w["date"]).date() < today:
            expired.append(w)
        else:
            active.append(w)

    for w in expired:
        log(f"Retiring past trip {w['id']} ({w['date']})")

    due: list[tuple[dict, dict]] = []
    for w in active:
        s = settings_for(w, defaults)
        hrs = hours_until(w["date"])
        interval = interval_for(hrs, s["cadence"])
        if store.due_for_poll(w["id"], interval):
            due.append((w, s))
            log(f"Due: {w['id']} ({hrs:.0f}h out, every {interval}m)")
        else:
            log(f"Skip: {w['id']} (checked recently, every {interval}m tier)")

    if not due:
        log("Nothing due this run.")
        rebuild_dashboard(active)
        return 0

    trips = [
        (w["from"], w["to"], w["date"], int(s["passengers"])) for w, s in due
    ]
    log(f"Fetching {len(trips)} trip(s)...")
    results = amtrak.fetch_many(trips, log=log)

    polled: list[str] = []
    for (w, s), trip in zip(due, trips):
        result = results.get(trip)
        if isinstance(result, Exception) or result is None:
            log(f"  {w['id']}: no data ({result})")
            continue

        polled.append(w["id"])
        best = cheapest(result, s)

        if best is None:
            log(f"  {w['id']}: no qualifying fares (sold out or filtered out)")
            store.append(w["id"], store.Snapshot(
                checked_at=datetime.now(timezone.utc).isoformat(),
                lowest_price=None, lowest_label=None, lowest_train=None,
                lowest_seats=None,
                trains=[o.to_dict() for o in result],
            ))
            continue

        opt, fare = best
        label = (
            f"{fare.travel_class} {fare.fare_family} on "
            f"#{opt.train_number} at {fmt_time(opt.depart)}"
        )
        log(f"  {w['id']}: ${fare.price:.0f} — {label}")

        previous = store.last(w["id"])
        all_time_low = store.observed_low(w["id"])

        store.append(w["id"], store.Snapshot(
            checked_at=datetime.now(timezone.utc).isoformat(),
            lowest_price=fare.price,
            lowest_label=label,
            lowest_train=opt.train_number,
            lowest_seats=fare.seats_left,
            trains=[o.to_dict() for o in result],
        ))

        maybe_alert(w, s, opt, fare, label, previous, all_time_low)

    if polled:
        store.mark_polled(polled)
    rebuild_dashboard(active)
    return 0


def maybe_alert(w, s, opt, fare, label, previous, all_time_low) -> None:
    price = fare.price
    target = s.get("target_price")
    reasons: list[str] = []

    prev_price = (previous or {}).get("lowest_price")
    if prev_price:
        drop = prev_price - price
        pct = (drop / prev_price) * 100 if prev_price else 0
        if drop >= float(s["alert_drop_abs"]) or pct >= float(s["alert_drop_pct"]):
            reasons.append(f"down ${drop:.0f} ({pct:.0f}%) from ${prev_price:.0f}")

    if target is not None and price <= float(target):
        reasons.append(f"at or below your ${float(target):.0f} target")

    if all_time_low is not None and price < all_time_low:
        reasons.append(f"lowest seen yet (was ${all_time_low:.0f})")

    if not reasons:
        log(f"    no alert (prev ${prev_price or 0:.0f}, low ${all_time_low or 0:.0f})")
        return

    if not store.should_alert(w["id"], price, int(s["cooldown_minutes"])):
        log("    suppressed by cooldown")
        return

    url = amtrak.booking_url(w["from"], w["to"], w["date"])
    date_label = datetime.fromisoformat(w["date"]).strftime("%a %b %-d")
    seats = (
        f"\nOnly {fare.seats_left} left at this price."
        if fare.seats_left is not None and fare.seats_left <= 5
        else ""
    )
    refund = (
        "\nFlexible fare, so it cancels free if it drops again."
        if fare.refundable
        else ""
    )

    notify.push(
        title=f"${price:.0f} {w['from']}-{w['to']} {date_label}",
        message=f"{label}\n{'; '.join(reasons)}.{seats}{refund}",
        url=url,
        priority="high" if (target is not None and price <= float(target)) else "default",
        tags="bullettrain_side,chart_with_downwards_trend",
        log=log,
    )
    store.record_alert(w["id"], price)


def rebuild_dashboard(active: list[dict]) -> None:
    """Write the JSON the dashboard page reads."""
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "watches": []}
    for w in active:
        rows = store.read_all(w["id"])
        series = [
            {"t": r["checked_at"], "p": r["lowest_price"], "l": r.get("lowest_label")}
            for r in rows
            if r.get("lowest_price") is not None
        ]
        latest = rows[-1] if rows else None
        prices = [pt["p"] for pt in series]
        out["watches"].append({
            "id": w["id"],
            "from": w["from"],
            "to": w["to"],
            "date": w["date"],
            "target_price": w.get("target_price"),
            "current": latest.get("lowest_price") if latest else None,
            "current_label": latest.get("lowest_label") if latest else None,
            "checked_at": latest.get("checked_at") if latest else None,
            "low": min(prices) if prices else None,
            "high": max(prices) if prices else None,
            "checks": len(series),
            "series": series[-500:],
            "booking_url": amtrak.booking_url(w["from"], w["to"], w["date"]),
        })
    DASHBOARD_DATA.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA.write_text(json.dumps(out, indent=2))
    log(f"Dashboard data written ({len(out['watches'])} watch(es)).")


if __name__ == "__main__":
    raise SystemExit(run())
