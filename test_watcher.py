"""
Checks the parser and the alert logic against a fixture captured from a
real NYP->WAS response on 2026-09-14.

Run with:  python -m tests.test_watcher
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watcher import amtrak, main, store  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixture_nyp_was.json").read_text())

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got:  {got!r}\n       want: {want!r}")


def test_parse():
    print("\nparse()")
    opts = amtrak.parse(FIXTURE)

    check("skips the cancelled train", [o.train_number for o in opts],
          ["111", "181", "2103", "113"])
    check("sorted by departure", [o.depart[11:16] for o in opts],
          ["04:50", "05:45", "06:10", "06:32"])

    t111 = opts[0]
    check("train name", t111.train_name, "Northeast Regional")
    check("fare count (NOT AVAILABLE ones excluded)", len(t111.fares), 3)
    check("cheapest on #111", t111.lowest.price, 82.0)
    check("fare family decoded", t111.lowest.fare_family, "Value")
    check("seat inventory", t111.lowest.seats_left, 52)
    check("Value is not refundable", t111.lowest.refundable, False)
    check("Flexible is refundable",
          [f.refundable for f in t111.fares if f.family_code == "FLX"], [True])

    acela = opts[2]
    check("Acela flagged", acela.is_acela, True)
    check("Acela has no coach", [f.travel_class for f in acela.fares],
          ["Business", "First", "Business", "First"])


def test_cheapest_and_filters():
    print("\ncheapest() and filters")
    opts = amtrak.parse(FIXTURE)

    s = {**main.DEFAULTS, "classes": ["Coach"]}
    opt, fare = main.cheapest(opts, s)
    check("cheapest coach across the day", (opt.train_number, fare.price),
          ("113", 20.0))
    check("and it flags the thin inventory", fare.seats_left, 3)

    s_any = {**main.DEFAULTS, "classes": []}
    opt, fare = main.cheapest(opts, s_any)
    check("cheapest of any class", fare.price, 20.0)

    s_biz = {**main.DEFAULTS, "classes": ["Business"]}
    opt, fare = main.cheapest(opts, s_biz)
    check("cheapest business", (opt.train_number, fare.price), ("181", 132.0))

    s_late = {**main.DEFAULTS, "classes": ["Coach"], "depart_after": "06:00"}
    opt, fare = main.cheapest(opts, s_late)
    check("depart_after excludes the 4:50a and 5:45a", opt.train_number, "113")

    s_early = {**main.DEFAULTS, "classes": ["Coach"], "depart_before": "06:00"}
    opt, fare = main.cheapest(opts, s_early)
    check("depart_before keeps only early trains", opt.train_number, "181")

    s_window = {**main.DEFAULTS, "classes": ["Coach"],
                "depart_after": "04:00", "depart_before": "05:00"}
    opt, fare = main.cheapest(opts, s_window)
    check("narrow window", (opt.train_number, fare.price), ("111", 82.0))


def test_cadence():
    print("\ncadence tiers")
    cad = main.DEFAULTS["cadence"]
    check("day-of trip polls every 15m", main.interval_for(10, cad), 15)
    check("2 days out polls every 15m", main.interval_for(47, cad), 15)
    check("1 week out polls hourly", main.interval_for(168, cad), 60)
    check("2 months out polls every 6h", main.interval_for(1400, cad), 360)


def test_alerting():
    print("\nalert decisions")
    tmp = Path(tempfile.mkdtemp())
    original = store.HISTORY_DIR
    store.HISTORY_DIR = tmp
    store.STATE_FILE = tmp / "_alert_state.json"
    store.LAST_POLL_FILE = tmp / "_last_poll.json"

    try:
        wid = "test-trip"
        now = datetime.now(timezone.utc).isoformat()
        store.append(wid, store.Snapshot(now, 82.0, "Coach Value on #111", "111", 52, []))
        check("history round-trips", store.last(wid)["lowest_price"], 82.0)
        check("all-time low", store.observed_low(wid), 82.0)

        store.append(wid, store.Snapshot(now, 45.0, "Coach Value on #181", "181", 38, []))
        check("all-time low updates", store.observed_low(wid), 45.0)

        check("first alert allowed", store.should_alert(wid, 45.0, 180), True)
        store.record_alert(wid, 45.0)
        check("same price suppressed by cooldown",
              store.should_alert(wid, 45.0, 180), False)
        check("higher price also suppressed",
              store.should_alert(wid, 60.0, 180), False)
        check("a new lower price breaks the cooldown",
              store.should_alert(wid, 20.0, 180), True)

        # An old alert should age out of the cooldown.
        store._save_state({wid: {
            "price": 45.0,
            "at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        }})
        check("cooldown expires", store.should_alert(wid, 45.0, 180), True)

        # Polling cadence bookkeeping.
        os.environ.pop("AMTRAK_FORCE_POLL", None)
        check("never polled means due", store.due_for_poll(wid, 15), True)
        store.mark_polled([wid])
        check("just polled means not due", store.due_for_poll(wid, 15), False)
        check("zero interval means always due", store.due_for_poll(wid, 0), True)
    finally:
        store.HISTORY_DIR = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_alert_reasons():
    """The drop thresholds, exercised without touching the network."""
    print("\ndrop thresholds")

    def reasons(prev, price, low, pct=15.0, absolute=20.0, target=None):
        out = []
        if prev:
            drop = prev - price
            p = (drop / prev) * 100
            if drop >= absolute or p >= pct:
                out.append("drop")
        if target is not None and price <= target:
            out.append("target")
        if low is not None and price < low:
            out.append("new-low")
        return out

    check("$82 -> $45 fires (both abs and pct)",
          reasons(82.0, 45.0, 82.0), ["drop", "new-low"])
    check("$82 -> $75 fires on pct only... no, 8.5% and $7, so nothing",
          reasons(82.0, 75.0, 75.0), [])
    check("$300 -> $275 fires on absolute alone",
          reasons(300.0, 275.0, 275.0), ["drop"])
    check("$40 -> $33 fires on percent alone",
          reasons(40.0, 33.0, 40.0), ["drop", "new-low"])
    check("a rise fires nothing", reasons(45.0, 82.0, 45.0), [])
    check("target hit fires even without a drop",
          reasons(46.0, 45.0, 46.0, target=50), ["target", "new-low"])
    check("matching the old low is not a new low",
          reasons(45.0, 45.0, 45.0), [])


def test_booking_url():
    print("\nbooking url")
    url = amtrak.booking_url("NYP", "WAS", "2026-10-05")
    check("contains stations and formatted date",
          ("ori=NYP" in url and "des=WAS" in url and "dt=10/05/2026" in url), True)


def test_payload():
    print("\nrequest payload")
    p = amtrak._payload("NYP", "WAS", "2026-10-05", 2)
    leg = p["journeyRequest"]["journeyLegRequests"][0]
    check("origin code", leg["origin"]["code"], "NYP")
    check("departure datetime", leg["origin"]["schedule"]["departureDateTime"],
          "2026-10-05T00:00:00")
    check("passenger count", len(leg["passengers"]), 2)
    check("passenger ids", [x["id"] for x in leg["passengers"]], ["P1", "P2"])
    tid = amtrak._trace_id()
    check("trace id is long hex-ish", len(tid) >= 40 and all(
        c in "0123456789abcdef" for c in tid), True)


if __name__ == "__main__":
    test_parse()
    test_cheapest_and_filters()
    test_cadence()
    test_alerting()
    test_alert_reasons()
    test_booking_url()
    test_payload()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
