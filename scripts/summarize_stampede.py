#!/usr/bin/env python3
"""
summarize_stampede.py - what did the BACKEND experience during the stampede?

Reads the JSON that `artillery run --output FILE` writes and prints, for each
10-second window of the run, how many requests of each kind were made. Pass
two files to compare runs side by side (for example the direct baseline and
the waiting-room run) on a shared scale.

    python3 scripts/summarize_stampede.py waiting-room.json
    python3 scripts/summarize_stampede.py direct.json waiting-room.json

WHY THIS EXISTS
  CloudWatch's Lambda metrics have a ONE-MINUTE minimum resolution. A burst
  that begins and ends inside a minute - which is exactly what a stampede
  is - gets averaged flat on the dashboard. Artillery reports in 10-second
  windows, so the shape of the traffic survives.

HOW TO READ THE COLUMNS
  Each request type has a distinct status code, so the counts are exact:

      joins      202   POST /waiting-room/join          visitors entering the line
      polls      200   GET  /waiting-room/status/...    "is it my turn yet?"
      orders     201   POST /orders                     orders the backend ACCEPTED
      rejected   403   POST /orders without admission   turned away at the gate
      throttled  429   API Gateway said "too fast"
      errors     5xx and network errors

  The "orders" column is the load the real system (OrderProducer and the
  fan-out behind it) was asked to carry. It is the number to compare.

  Windows are Artillery's 10-second reporting periods, labelled by the
  moment they END. The first and last windows are usually partial, so their
  rates read low; the peak is almost always a full window.

The output states what was measured and nothing more. What it means is
for you to work out.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

WINDOW_SECONDS = 10.0
BAR_WIDTH = 36


def _count(counters: dict, code: int) -> int:
    return int(counters.get(f"http.codes.{code}", 0))


def _count_prefix(counters: dict, prefix: str) -> int:
    return sum(int(v) for k, v in counters.items() if k.startswith(prefix))


def load_run(path) -> dict:
    """Parse one Artillery JSON file into a plain dict of windows and totals."""
    data = json.loads(Path(path).read_text())
    windows = []
    for p in data.get("intermediate", []):
        c = p.get("counters", {})
        windows.append({
            "ends_at": int(p["period"]) / 1000.0,
            "joins": _count(c, 202),
            "polls": _count(c, 200),
            "orders": _count(c, 201),
            "rejected": _count(c, 403),
            "throttled": _count(c, 429),
            "errors": _count_prefix(c, "http.codes.5") + _count_prefix(c, "errors."),
        })

    agg = data.get("aggregate", {})
    counters = agg.get("counters", {})
    summaries = agg.get("summaries", {})
    return {
        "name": Path(path).stem,
        "windows": windows,
        "vusers": int(counters.get("vusers.created", 0)),
        "vusers_failed": int(counters.get("vusers.failed", 0)),
        "admitted": int(counters.get("waiting_room.admitted", 0)),
        "gave_up": int(counters.get("waiting_room.gave_up", 0)),
        "wait_seconds": summaries.get("waiting_room.wait_seconds"),
        "polls_per_visitor": summaries.get("waiting_room.polls_per_visitor"),
        "has_waiting_room": any(w["joins"] or w["polls"] for w in windows),
    }


def peak_orders_per_second(run: dict) -> float:
    return max((w["orders"] for w in run["windows"]), default=0) / WINDOW_SECONDS


def _bar(value: float, scale_max: float) -> str:
    if scale_max <= 0 or value <= 0:
        return ""
    return "#" * max(1, round(BAR_WIDTH * value / scale_max))


def render(run: dict, scale_max_orders: float = None) -> str:
    """Render one run. scale_max_orders lets two runs share a bar scale."""
    windows = run["windows"]
    scale = scale_max_orders if scale_max_orders is not None else max(
        (w["orders"] for w in windows), default=0)

    lines = [f"=== {run['name']} ===", ""]
    header = (f"{'window ends':<12}{'joins':>7}{'polls':>8}{'orders':>8}"
              f"{'rejected':>10}{'throttled':>11}{'errors':>8}{'orders/s':>10}   orders accepted")
    lines += [header, "-" * len(header)]
    for w in windows:
        clock = datetime.fromtimestamp(w["ends_at"]).strftime("%H:%M:%S")
        rate = w["orders"] / WINDOW_SECONDS
        lines.append(
            f"{clock:<12}{w['joins']:>7}{w['polls']:>8}{w['orders']:>8}"
            f"{w['rejected']:>10}{w['throttled']:>11}{w['errors']:>8}{rate:>10.1f}   "
            f"{_bar(w['orders'], scale)}"
        )

    total = lambda key: sum(w[key] for w in windows)
    lines += ["", f"Visitors: {run['vusers']}   failed: {run['vusers_failed']}",
              f"Orders accepted: {total('orders')}   rejected at gate: {total('rejected')}   "
              f"throttled: {total('throttled')}   errors: {total('errors')}",
              f"Peak orders accepted per second (best 10 s window): {peak_orders_per_second(run):.1f}"]

    if run["has_waiting_room"]:
        lines.append(f"Waiting room: admitted {run['admitted']}, gave up {run['gave_up']}")
        ws = run["wait_seconds"]
        if ws:
            lines.append(f"  time in line (seconds): median {ws['median']:.1f}   "
                         f"p95 {ws['p95']:.1f}   max {ws['max']:.1f}")
        pp = run["polls_per_visitor"]
        if pp:
            lines.append(f"  status polls per visitor: median {pp['median']:.0f}   max {pp['max']:.0f}")
        lines.append(f"  status polls total: {total('polls')}  "
                     f"(the waiting room's own traffic - every poll is a request)")
    return "\n".join(lines)


def render_comparison(runs: list) -> str:
    peaks = [(r["name"], peak_orders_per_second(r)) for r in runs]
    width = max(len(n) for n, _ in peaks)
    top = max(p for _, p in peaks)
    lines = ["=== side by side: peak orders accepted per second ===", ""]
    for name, peak in peaks:
        lines.append(f"{name:<{width}}  {peak:>8.1f}/s   {_bar(peak, top)}")
    return "\n".join(lines)


def main(argv=None) -> int:
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths or any(p in ("-h", "--help") for p in paths):
        print(__doc__)
        return 0 if paths else 2
    try:
        runs = [load_run(p) for p in paths]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"Could not read Artillery output: {e}", file=sys.stderr)
        return 1

    shared = max((w["orders"] for r in runs for w in r["windows"]), default=0)
    for run in runs:
        print(render(run, scale_max_orders=shared if len(runs) > 1 else None))
        print()
    if len(runs) > 1:
        print(render_comparison(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
