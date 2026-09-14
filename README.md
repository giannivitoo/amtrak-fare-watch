# Amtrak Fare Watch

Polls Amtrak for the trips you care about, and pushes a notification to your
Mac and phone when a fare drops. Runs on GitHub Actions, so it keeps watching
whether or not your laptop is open. Free.

It also keeps every observation, so over time you get a real record of how
Amtrak's pricing moves on the routes you actually ride.

---

## Setup, about ten minutes

### 1. Get a notification channel

Notifications go through [ntfy.sh](https://ntfy.sh), which is free and needs no
account. You pick a topic name; anyone who knows it can read your alerts, so
make it unguessable:

```bash
echo "amtrak-$(openssl rand -hex 12)"
```

Keep that string. It is the only secret here.

Then subscribe to it:

- **Mac desktop notifications:** open <https://ntfy.sh/app>, click *Subscribe to
  topic*, paste the topic, and allow notifications when Chrome asks. Leave the
  tab open (pin it) and drops arrive in macOS Notification Center.
- **Phone:** install the ntfy app (iOS or Android), *Subscribe to topic*, paste
  the same string. This is the one worth doing, since day-of drops tend to find
  you away from your desk.

### 2. Put this in a GitHub repo

With the `gh` CLI:

```bash
cd amtrak-fare-watch
git init && git add . && git commit -m "Amtrak fare watch"
gh repo create amtrak-fare-watch --public --source=. --push
```

Or entirely in the browser:

1. <https://github.com/new> - name it `amtrak-fare-watch`, pick Public, and
   leave every "Initialize with" box unchecked.
2. On the empty repo page, click **uploading an existing file**.
3. In Finder, open this project folder and press **Cmd-Shift-.** to reveal
   hidden files, so the `.github` folder is visible. Select everything inside
   the folder (Cmd-A) and drag it onto the upload page. Commit.
4. Confirm `.github/workflows/watch.yml` is listed in the repo. If the upload
   skipped it, add it by hand: **Add file -> Create new file**, set the
   filename to `.github/workflows/watch.yml` (typing the slashes creates the
   folders), paste the file's contents, and commit.

**Public or private?** Public repos get unlimited free Actions minutes. Private
repos get 2,000 minutes a month, which this can exceed when you have trips
inside the 15-minute polling tier. Public is the easy choice, and the only thing
exposed is which trips you are watching. Your ntfy topic goes in Secrets either
way and is never committed. If you would rather keep the trips private, make the
repo private and widen the cadence tiers in `watchlist.json`.

### 3. Add the secret

```bash
gh secret set NTFY_TOPIC --body "amtrak-whatever-you-generated"
```

Or: repo → Settings → Secrets and variables → Actions → New repository secret,
named `NTFY_TOPIC`.

### 4. Turn on the dashboard (optional)

Settings → Pages → Source: *Deploy from a branch*, branch `main`, folder
`/docs`. Your price history then lives at
`https://<you>.github.io/amtrak-fare-watch/`.

### 5. Try it

Actions → *Watch Amtrak fares* → *Run workflow*. The first run takes about a
minute. Watch the log: you should see the browser session warm up, then a
price for each trip.

---

## Watching trips

Everything lives in `watchlist.json`. Edit, commit, push. The next run picks it
up. Past-dated trips retire themselves.

```jsonc
{
  "watches": [
    {
      "id": "nyp-was-2026-10-05",   // any unique string; names the history file
      "from": "NYP",
      "to": "WAS",
      "date": "2026-10-05",
      "target_price": 50,           // optional: high-priority alert at or below this
      "depart_after": "06:00",      // optional: ignore the 4am departures
      "depart_before": "20:00"
    }
  ]
}
```

Common station codes: `NYP` New York Penn (Moynihan), `WAS` Washington Union,
`BOS` Boston South, `PHL` Philadelphia 30th St, `NWK` Newark Penn, `BAL`
Baltimore, `WIL` Wilmington, `TRE` Trenton, `NHV` New Haven, `PVD` Providence,
`BBY` Boston Back Bay, `ALB` Albany, `PHN` Philadelphia North.

### Settings

Any of these can go in `defaults` (applies to all trips) or on an individual
watch (overrides the default).

| Setting | Default | What it does |
|---|---|---|
| `classes` | `["Coach"]` | Which travel classes count. `[]` or `["*"]` for any. Acela has no coach, so leave this as Coach to ignore Acela, or add `"Business"` to include it. |
| `alert_drop_pct` | `15` | Alert when the price falls this many percent since the last check. |
| `alert_drop_abs` | `20` | ...or this many dollars. Either one fires. |
| `target_price` | none | Alert at high priority any time the price is at or below this. |
| `cooldown_minutes` | `180` | Do not re-alert the same price within this window. A new, lower price always breaks the cooldown. |
| `passengers` | `1` | Fares are per-person, and per-person prices change with party size. |
| `depart_after` / `depart_before` | none | Restrict to a departure window, `"HH:MM"` 24-hour. |
| `cadence` | see below | How often to check, by how close the trip is. |

### Polling cadence

The workflow wakes every 15 minutes, but each trip decides whether it is
actually due. Defaults:

| Time to departure | Checked |
|---|---|
| Under 48 hours | every 15 minutes |
| Under 14 days | hourly |
| Beyond that | every 6 hours |

This is the point of the whole thing: day-of collapses get caught within
fifteen minutes, while a trip three months out does not burn compute. Change
the tiers under `defaults.cadence`.

---

## What you get in an alert

```
$45 NYP-WAS Mon Oct 5
Coach Value on #113 at 6:32a
down $37 (45%) from $82; lowest seen yet (was $82).
Only 3 left at this price.
```

Tapping it opens Amtrak's search for that trip. The seat count comes from
Amtrak's own inventory, so "only 3 left" is real, not a marketing line.

---

## Two things worth knowing about Amtrak pricing

**Fares mostly go up, not down.** Amtrak sells seats in price buckets; when a
bucket empties the next one costs more. The day-of collapse you are hunting
happens when a train is running empty off-peak and they would rather sell a $20
seat than an empty one. It is real, and this tool catches it, but it is the
exception. On a Friday evening or near a holiday it never happens and the train
sells out instead.

**So the strategy that actually wins is book-then-rebook.** Buy early on a
**Flexible** fare, which cancels free any time before departure. You have a
guaranteed seat. If this tool then tells you the price collapsed, cancel and
rebook at the lower one. Flexible costs about 10% more than Value up front and
removes the entire downside of guessing wrong. Value fares forfeit 30% on
cancellation, so the drop has to be large before rebooking pays. Every fare also
has a 24-hour free-cancellation window from the moment you buy.

Alerts tell you which of the two you are looking at.

---

## How it works

Amtrak has no public fare API. Their booking site posts to an internal endpoint,
`/dotcom/journey-solution-option`, and that is what this calls.

The site sits behind Akamai Bot Manager. Measured against the live site: a
request carrying the cookies a real browser session has earned returns in about
300ms; the identical request without them is tarpitted and never returns. So a
plain HTTP client cannot reach this endpoint on its own.

The run therefore opens one Chromium page with Playwright, loads amtrak.com so
Akamai's sensor script runs and grants the clearance cookies, and then issues
every trip's request from inside that page. The fixed cost is paid once per run
and each additional trip is a few hundred milliseconds, so watching ten trips
costs barely more than watching one. A direct HTTP path stays in the code as a
long-shot fallback with a short timeout.

```
watcher/amtrak.py   endpoint, browser session, response parsing
watcher/store.py    price history, alert cooldown, polling cadence
watcher/notify.py   ntfy push
watcher/main.py     the run: decide, fetch, compare, alert, record
docs/               the dashboard, served by GitHub Pages
history/            one JSONL file per trip, committed after each run
```

### Tests

```bash
python -m tests.test_watcher     # parsing, filters, cadence, alert rules
python -m tests.test_endtoend    # a full simulated run, network stubbed out
```

The fixture in `tests/` is a real NYP→WAS response captured on 2026-09-14.

---

## When it breaks

**Every trip fails with "browser tier failed".** Amtrak changed something. Open
the booking page in Chrome with DevTools on the Network tab, run a search, find
the `journey-solution-option` request, and compare its payload and headers to
`_payload()` and `_headers()` in `watcher/amtrak.py`. This is the part that will
need occasional maintenance; the rest should not.

**No notifications but the log shows prices.** Check that `NTFY_TOPIC` is set as
a repository secret and that the topic in the log matches what you subscribed
to. The log prints `[notify] NTFY_TOPIC not set` when the secret is missing.

**The workflow stopped running.** GitHub disables scheduled workflows in repos
with no activity for 60 days. The watcher commits its own history, which usually
keeps it alive, but if the watchlist sits empty for two months it can go dormant.
Re-enable it from the Actions tab.

**Scheduled runs are late.** GitHub's cron is best-effort and runs queue up under
load, so a 15-minute schedule can slip to 20 or more. Nothing to fix; just do
not treat the cadence as a guarantee.

---

## Please be polite

This makes one request per watched trip per check, which is less traffic than
leaving the booking page open and refreshing. Keep it that way: do not drop the
cadence to every minute, and do not watch fifty routes because you can.
