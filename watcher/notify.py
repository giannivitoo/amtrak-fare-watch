"""
Push notifications via ntfy.sh.

ntfy is a free pub/sub push service with no account required. You pick an
unguessable topic name, the watcher publishes to it, and you subscribe from
the macOS app, the iPhone app, or a browser tab. Subscribing in Chrome gives
you real macOS notification-centre alerts.

Set NTFY_TOPIC in the environment (a GitHub Actions secret) to switch it on.
"""

from __future__ import annotations

import os

import requests

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


def enabled() -> bool:
    return bool(os.environ.get("NTFY_TOPIC"))


def push(title: str, message: str, url: str | None = None,
         priority: str = "default", tags: str = "train", log=print) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log("  [notify] NTFY_TOPIC not set, skipping push")
        return False

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    if url:
        # Makes the notification itself clickable, straight to booking.
        headers["Click"] = url
        headers["Actions"] = f"view, Book on Amtrak, {url}"

    try:
        resp = requests.post(
            f"{NTFY_SERVER}/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        log(f"  [notify] pushed: {title}")
        return True
    except Exception as e:
        log(f"  [notify] push failed: {e}")
        return False
