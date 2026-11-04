"""Send policy per lane: how much of the day goes out on each tick, and when
the volume lane's guess tier has to wait.

Kept apart from pipeline.py so the health check can ask the same questions
the send path does without importing the whole pipeline.
"""

from __future__ import annotations

import math

from .models import MAIN_LANE, VOLUME_LANE, volume_tier


def is_guess(row) -> bool:
    """A draft or follow-up row in the volume lane's guess tier."""
    return volume_tier(row["verify_status"], row["provenance"])


def guess_hold(settings, db, today: str) -> str:
    """Why the volume lane's guesses must wait right now, or "".

    Two brakes. The rolling one reads the lane's recent sends, like the
    ramp, and holds still for the day. The daily one watches today's
    notices as they land, because a run of bad guesses can do its damage
    inside one morning while the rolling figure has not moved: two or more
    bounced and over the limit, and guesses stop until tomorrow. Every
    other address the lane sends faces the main gate and is unaffected.
    """
    if getattr(db, "lane", MAIN_LANE) != VOLUME_LANE:
        return ""
    limit = getattr(settings, "guess_max_bounce_pct", 0)
    if not limit:
        return ""
    pct = db.recent_bounce_pct(before_local_date=today)
    if pct > limit:
        return f"guesses held, recent bounce rate {pct:.1f}% over {limit}%"
    sent, bounced = db.bounces_on(today)
    if len(bounced) >= 2 and 100.0 * len(bounced) / max(1, sent) > limit:
        return f"guesses held for today, {len(bounced)} of {sent} sent have bounced"
    return ""


def spread(rows_with_state: list, allowance: int) -> list:
    """What goes out on this tick, given each row's window state.

    Rows come in priority order, each paired with its emailer.WindowState.
    Every zone whose window is open gets the share of `allowance` that its
    rows make up of everything still reachable today, divided over the ticks
    left in its window and rounded up, so a morning's sends are spread from
    the first tick to the last instead of two at a time until the window
    shuts. Rounding up means a window that has fallen behind (a skipped
    tick, a late start) catches up rather than leaving its share unsent.
    Zones that open later today keep their share; closed ones forfeit it to
    the rest. Never more than `allowance` in total.
    """
    if allowance <= 0:
        return []
    reachable = 0
    open_rows: dict[str, int] = {}
    ticks: dict[str, int] = {}
    for _row, state in rows_with_state:
        if not state.today:
            continue
        reachable += 1
        if state.open:
            open_rows[state.zone] = open_rows.get(state.zone, 0) + 1
            ticks[state.zone] = state.ticks_left
    budget = {
        zone: min(n, math.ceil(allowance * n / reachable / max(1, ticks[zone])))
        for zone, n in open_rows.items()
    }
    picked = []
    for row, state in rows_with_state:
        if len(picked) >= allowance:
            break
        if state.open and budget.get(state.zone, 0) > 0:
            picked.append(row)
            budget[state.zone] -= 1
    return picked
