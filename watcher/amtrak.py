"""
Talks to Amtrak's booking backend.

Amtrak has no public fare API. Their booking SPA posts to an internal JSON
endpoint, /dotcom/journey-solution-option, which is what this module calls.

The site sits behind Akamai Bot Manager. Measured against the live site on
2026-09-14: a request carrying the cookies a real browser session has picked
up returns in about 300ms, while the identical request without them is
tarpitted and never returns at all. So a plain HTTP client cannot reach this
endpoint on its own.

The approach that does work, and what this module does:

  1. Open one Chromium page with Playwright and load amtrak.com, which runs
     Akamai's sensor script and earns the clearance cookies. Costs about
     20-30 seconds, once.
  2. Issue every watched trip's request from inside that page. Each one is
     a few hundred milliseconds, so watching ten trips costs barely more
     than watching one.

A plain HTTP path is kept as a long-shot fallback for the case where the
browser cannot start at all, with a short timeout so a tarpit cannot stall
the run.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Iterable

import requests

BASE = "https://www.amtrak.com"
ENDPOINT = "/dotcom/journey-solution-option"

# Amtrak's fare family codes, as they appear in the response.
FARE_FAMILIES = {
    "SAL": "Sale",
    "VLU": "Value",
    "FLX": "Flexible",
    "COR": "Corporate",
    "PRE": "Premium",
    "NA": "Standard",
}

# Refundability, which decides whether a "book now, rebook on a drop"
# strategy is worth it for a given fare. Flexible cancels free.
REFUNDABLE = {"FLX"}


class BlockedError(RuntimeError):
    """Amtrak refused the plain HTTP request (bot challenge or rate limit)."""


def _trace_id() -> str:
    """Mimic the x-amtrak-trace-id header their SPA sends.

    The server only checks that the header is present and well formed, not
    that it came from a real session, so a fresh random one per call is fine.
    """
    return "%032x%d0" % (random.getrandbits(128), int(time.time() * 1000))


def _payload(origin: str, destination: str, date: str, passengers: int = 1) -> dict:
    """Build the journey search body. `date` is YYYY-MM-DD."""
    return {
        "journeyRequest": {
            "fare": {"pricingUnit": "DOLLARS"},
            "type": "OW",
            "journeyLegRequests": [
                {
                    "origin": {
                        "code": origin,
                        "schedule": {"departureDateTime": f"{date}T00:00:00"},
                    },
                    "destination": {"code": destination},
                    "passengers": [
                        {"id": f"P{i + 1}", "type": "F", "initialType": "adult"}
                        for i in range(passengers)
                    ],
                }
            ],
            "customer": {"tierStatus": "MEMBER"},
            "isPassRider": False,
            "isCorporateTraveller": False,
            "tripTags": True,
            "singleAdultFare": True,
            "cascadesWSDOTFilter": False,
            "xDelay": "60",
        },
        "initialJourneyLegOnly": False,
        "reservableAccomodationOptions": "ALL",
    }


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "x-amtrak-trace-id": _trace_id(),
        "Origin": BASE,
        "Referer": f"{BASE}/tickets/departure.html",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }


# --------------------------------------------------------------------------
# Parsed result types
# --------------------------------------------------------------------------


@dataclass
class Fare:
    travel_class: str       # Coach / Business / First / Private Rooms
    fare_family: str        # Sale / Value / Flexible / ...
    family_code: str        # SAL / VLU / FLX / ...
    price: float
    seats_left: int | None  # inventory remaining in this bucket
    refundable: bool


@dataclass
class TrainOption:
    train_number: str
    train_name: str          # "Northeast Regional", "Acela"
    is_acela: bool
    depart: str              # ISO local datetime
    arrive: str
    duration_seconds: int
    fares: list[Fare]

    @property
    def lowest(self) -> Fare | None:
        return min(self.fares, key=lambda f: f.price) if self.fares else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fares"] = [asdict(f) for f in self.fares]
        return d


def parse(raw: dict) -> list[TrainOption]:
    """Turn the endpoint's response into a flat list of train options."""
    data = raw.get("data") or {}
    solution = data.get("journeySolutionOption") or {}
    legs = solution.get("journeyLegs") or []
    if not legs:
        return []

    options: list[TrainOption] = []
    for opt in legs[0].get("journeyLegOptions") or []:
        if opt.get("isCancelled"):
            continue

        travel_legs = opt.get("travelLegs") or []
        if not travel_legs:
            continue
        service = (travel_legs[0].get("travelService") or {})

        fares: list[Fare] = []
        for accom in opt.get("reservableAccommodations") or []:
            amount = (
                (accom.get("accommodationFare") or {})
                .get("dollarsAmount", {})
                .get("total")
            )
            if amount in (None, "", "0.00"):
                continue

            seats = None
            for tla in accom.get("travelLegAccommodations") or []:
                product = tla.get("reservableProduct") or {}
                inv = product.get("availableInventory")
                if isinstance(inv, int):
                    seats = inv if seats is None else min(seats, inv)

            code = accom.get("fareFamily") or "NA"
            fares.append(
                Fare(
                    travel_class=accom.get("travelClass") or "Unknown",
                    fare_family=FARE_FAMILIES.get(code, code),
                    family_code=code,
                    price=float(amount),
                    seats_left=seats,
                    refundable=code in REFUNDABLE,
                )
            )

        if not fares:
            continue

        options.append(
            TrainOption(
                train_number=str(service.get("number") or "?"),
                train_name=service.get("name") or "Amtrak",
                is_acela=bool(service.get("isAcela")),
                depart=(opt.get("origin") or {}).get("schedule", {}).get(
                    "departureDateTime", ""
                ),
                arrive=(opt.get("destination") or {}).get("schedule", {}).get(
                    "arrivalDateTime", ""
                ),
                duration_seconds=int(opt.get("elapsedSeconds") or 0),
                fares=fares,
            )
        )

    options.sort(key=lambda o: o.depart)
    return options


# --------------------------------------------------------------------------
# Tier 1: plain HTTP
# --------------------------------------------------------------------------


def fetch_http(origin: str, destination: str, date: str, passengers: int = 1,
               timeout: int = 15) -> list[TrainOption]:
    """Long-shot direct request. Usually tarpitted; keep the timeout short."""
    resp = requests.post(
        BASE + ENDPOINT,
        headers=_headers(),
        json=_payload(origin, destination, date, passengers),
        timeout=timeout,
    )
    if resp.status_code in (403, 429) or "text/html" in resp.headers.get(
        "Content-Type", ""
    ):
        raise BlockedError(f"HTTP {resp.status_code} (bot challenge)")
    resp.raise_for_status()

    body = resp.json()
    errors = body.get("errors") or []
    if errors:
        raise BlockedError(errors[0].get("sysMessage") or "endpoint error")
    return parse(body)


# --------------------------------------------------------------------------
# Tier 2: real browser
# --------------------------------------------------------------------------


class BrowserSession:
    """A warmed-up Chromium page that has cleared Amtrak's bot check.

    Open it once per polling run and reuse it across every watched trip.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None

    # JS run inside the page. A hard timeout matters because an unauthorised
    # request to this endpoint hangs rather than erroring.
    _FETCH_JS = """
    async ({endpoint, payload, traceId, timeoutMs}) => {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), timeoutMs);
        try {
            const r = await fetch(endpoint, {
                method: 'POST',
                signal: ctl.signal,
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                    'x-amtrak-trace-id': traceId
                },
                body: JSON.stringify(payload)
            });
            return {status: r.status, body: await r.text()};
        } catch (e) {
            return {status: 0, body: '', error: String(e)};
        } finally {
            clearTimeout(timer);
        }
    }"""

    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        ctx = self._browser.new_context(
            user_agent=_headers()["User-Agent"],
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1440, "height": 900},
        )
        # Chromium sets navigator.webdriver, which bot detection looks at.
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self._page = ctx.new_page()
        self._page.set_default_timeout(60000)
        self._page.goto(
            f"{BASE}/home.html", wait_until="domcontentloaded", timeout=60000
        )
        # Let Akamai's sensor script run and post, so the clearance cookies
        # are in place before we ask for fares.
        self._page.wait_for_timeout(8000)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def fetch(self, origin: str, destination: str, date: str,
              passengers: int = 1, attempts: int = 2) -> list[TrainOption]:
        last_error = "no attempt made"
        for attempt in range(attempts):
            result = self._page.evaluate(
                self._FETCH_JS,
                {
                    "endpoint": ENDPOINT,
                    "payload": _payload(origin, destination, date, passengers),
                    "traceId": _trace_id(),
                    "timeoutMs": 25000,
                },
            )
            if result["status"] == 200:
                parsed = json.loads(result["body"])
                errors = parsed.get("errors") or []
                if errors:
                    raise BlockedError(
                        errors[0].get("sysMessage") or "endpoint error"
                    )
                return parse(parsed)

            last_error = result.get("error") or f"HTTP {result['status']}"
            if attempt + 1 < attempts:
                # Usually means the sensor had not finished. Reload and wait.
                self._page.wait_for_timeout(5000)
                try:
                    self._page.reload(wait_until="domcontentloaded")
                    self._page.wait_for_timeout(6000)
                except Exception:
                    pass

        raise BlockedError(f"browser tier failed: {last_error}")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def fetch_many(trips: Iterable[tuple[str, str, str, int]],
               log=print) -> dict[tuple, list[TrainOption] | Exception]:
    """Fetch fares for several (origin, destination, date, passengers) trips.

    One browser session is warmed up and then reused for every trip, so the
    fixed cost of clearing bot detection is paid once per run rather than
    once per trip. Anything the browser could not get is retried over plain
    HTTP as a long shot.
    """
    trips = list(trips)
    results: dict[tuple, Any] = {}
    leftover: list[tuple] = []

    if os.environ.get("AMTRAK_NO_BROWSER") == "1":
        log("  browser tier disabled by AMTRAK_NO_BROWSER")
        leftover = list(trips)
    else:
        try:
            log("  warming up browser session...")
            with BrowserSession() as session:
                log("  session ready")
                for trip in trips:
                    label = f"{trip[0]}->{trip[1]} {trip[2]}"
                    try:
                        results[trip] = session.fetch(*trip)
                        log(f"  ok      {label} ({len(results[trip])} trains)")
                    except Exception as e:
                        log(f"  failed  {label}: {e}")
                        leftover.append(trip)
                    time.sleep(1.5)  # be a polite client
        except Exception as e:
            log(f"  browser session could not start: {e}")
            leftover = [t for t in trips if t not in results]

    for trip in leftover:
        label = f"{trip[0]}->{trip[1]} {trip[2]}"
        try:
            results[trip] = fetch_http(*trip)
            log(f"  ok (http) {label}")
        except Exception as e:
            log(f"  giving up on {label}: {e}")
            results[trip] = e

    return results


def booking_url(origin: str, destination: str, date: str) -> str:
    """A link that drops you on Amtrak's search for this trip."""
    d = datetime.fromisoformat(date).strftime("%m/%d/%Y")
    return (
        f"{BASE}/tickets/departure.html"
        f"?ori={origin}&des={destination}&dt={d}&adt=1&type=OW"
    )
