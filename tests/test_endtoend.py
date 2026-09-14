"""
End-to-end dry run with the network stubbed out.

Simulates three consecutive polling runs where the fare drops, and asserts
that history accumulates, the dashboard data is written, and exactly one
notification fires at the right moment.

Run with:  python -m tests.test_endtoend
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watcher import amtrak, main, notify, store  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixture_nyp_was.json").read_text())

sent: list[dict] = []
PASS, FAIL = 0, 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got:  {got!r}\n       want: {want!r}")


def fixture_at(coach_price: float):
    """A copy of the fixture where train #113 is the cheapest coach fare.

    Every other coach fare is pushed well above it, so the run has a single
    unambiguous winner and the assertions below can name it.
    """
    data = copy.deepcopy(FIXTURE)
    opts = data["data"]["journeySolutionOption"]["journeyLegs"][0]["journeyLegOptions"]
    for o in opts:
        is_target = o["travelLegs"][0]["travelService"]["number"] == "113"
        for accom in o["reservableAccommodations"]:
            if accom["travelClass"] != "Coach":
                continue
            amount = coach_price if is_target else 500.0
            accom["accommodationFare"]["dollarsAmount"]["total"] = f"{amount:.2f}"
    return data


def main_run_with(price: float):
    """Run one polling cycle where the cheapest coach fare is `price`."""
    def fake_fetch_many(trips, log=print):
        return {t: amtrak.parse(fixture_at(price)) for t in trips}

    real = amtrak.fetch_many
    amtrak.fetch_many = fake_fetch_many
    try:
        os.environ["AMTRAK_FORCE_POLL"] = "1"
        return main.run()
    finally:
        amtrak.fetch_many = real


def fake_push(title, message, url=None, priority="default", tags="", log=print):
    sent.append({"title": title, "message": message, "url": url,
                 "priority": priority})
    log(f"  [notify] (captured) {title}")
    return True


def run():
    tmp = Path(tempfile.mkdtemp())
    watchlist_backup = main.WATCHLIST.read_text()
    dash_backup = main.DASHBOARD_DATA.read_text() if main.DASHBOARD_DATA.exists() else None

    store.HISTORY_DIR = tmp
    store.STATE_FILE = tmp / "_alert_state.json"
    store.LAST_POLL_FILE = tmp / "_last_poll.json"
    notify.push = fake_push
    main.notify.push = fake_push

    # A single watch, far enough out that it lands in a slow cadence tier.
    main.WATCHLIST.write_text(json.dumps({
        "defaults": {"classes": ["Coach"], "alert_drop_pct": 15,
                     "alert_drop_abs": 20, "cooldown_minutes": 180},
        "watches": [{"id": "e2e-nyp-was", "from": "NYP", "to": "WAS",
                     "date": "2026-10-05", "target_price": 25}],
    }))

    try:
        print("\nrun 1: first observation at $82")
        main_run_with(82.0)
        check("history has one row", len(store.read_all("e2e-nyp-was")), 1)
        check("no alert on a first sighting", len(sent), 0)

        print("\nrun 2: fare drops to $45")
        main_run_with(45.0)
        check("history has two rows", len(store.read_all("e2e-nyp-was")), 2)
        check("one alert fired", len(sent), 1)
        check("title carries the price and route",
              sent[0]["title"], "$45 NYP-WAS Mon Oct 5")
        check("message names the train",
              "#113" in sent[0]["message"], True)
        check("message explains why",
              "down $37 (45%) from $82" in sent[0]["message"], True)
        check("message flags thin inventory",
              "Only 3 left at this price." in sent[0]["message"], True)
        check("link goes to Amtrak booking",
              sent[0]["url"].startswith("https://www.amtrak.com/tickets/departure.html"),
              True)
        check("normal priority (target not hit)", sent[0]["priority"], "default")

        print("\nrun 3: fare ticks up to $60")
        main_run_with(60.0)
        check("history has three rows", len(store.read_all("e2e-nyp-was")), 3)
        check("a rise fires no alert", len(sent), 1)

        print("\nrun 4: fare collapses to $20, under the $25 target")
        main_run_with(20.0)
        check("second alert fired", len(sent), 2)
        check("high priority when the target is met",
              sent[1]["priority"], "high")
        check("target reason present",
              "at or below your $25 target" in sent[1]["message"], True)
        check("new-low reason present",
              "lowest seen yet (was $45)" in sent[1]["message"], True)

        print("\nrun 5: same $20 again")
        main_run_with(20.0)
        check("no duplicate alert at the same price", len(sent), 2)

        print("\ndashboard data")
        data = json.loads(main.DASHBOARD_DATA.read_text())
        w = data["watches"][0]
        check("one watch", len(data["watches"]), 1)
        check("current price", w["current"], 20.0)
        check("all-time low", w["low"], 20.0)
        check("all-time high", w["high"], 82.0)
        check("series length", len(w["series"]), 5)
        check("target carried through", w["target_price"], 25)

        print("\nexpired trips")
        main.WATCHLIST.write_text(json.dumps({
            "watches": [{"id": "old", "from": "NYP", "to": "WAS",
                         "date": "2020-01-01"}],
        }))
        main_run_with(50.0)
        check("past-dated trip is dropped, not polled",
              len(store.read_all("old")), 0)

    finally:
        main.WATCHLIST.write_text(watchlist_backup)
        if dash_backup is not None:
            main.DASHBOARD_DATA.write_text(dash_backup)
        elif main.DASHBOARD_DATA.exists():
            main.DASHBOARD_DATA.unlink()
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("AMTRAK_FORCE_POLL", None)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
