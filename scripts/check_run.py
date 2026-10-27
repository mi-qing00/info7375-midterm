#!/usr/bin/env python3
"""
check_run.py - grade ONE stampede run against the exam's rules.

Reads the JSON that `artillery run --output FILE` writes and prints a scorecard: each rule, whether
the run met it, what was measured and what was expected. It is the same arithmetic for every team,
which is the point: nobody is graded by eye.

    python3 scripts/check_run.py waiting-room.json
    python3 scripts/check_run.py waiting-room.json --configured-rate 10
    python3 scripts/check_run.py direct-capped.json --expect-failures 0.3     # the failing baseline

Exit code: 0 if no rule FAILED, 1 if any did, 2 if the file could not be read, 3 if the run is INVALID
(see below). SKIP and INFO never fail a run.

INVALID RUNS
  A run is INVALID - rerun it, do not grade it - when the LOAD GENERATOR failed and not the system:
  client-side errors are above the tolerance AND bunched into a few seconds AND the server reported no
  errors of its own. The signature is a burst of ERR_SOCKET_TIMEOUT (requests the system never saw).
  On the first AWS runs this was Artillery holding ~1,400 simultaneous connections from one machine;
  the load test now caps its connection pool. The rules that merely reflect lost requests are then marked
  INVALID instead of FAIL; a genuine failure (an order without admission, server errors) still FAILS.

WHAT IT CHECKS (waiting-room run)
  - every bot got a 403                       the gate is not advisory
  - no order without an admission             orders accepted <= visitors admitted
  - everyone served, nobody gave up           >= 99.5% of visitors who joined were admitted
  - the admission rate is near the configured rate, measured over the STEADY MIDDLE of the run
    (the first three and the last window with orders are dropped: ramp-up and tail are partial)
  - no 429s, no 5xx
  - client-side errors are reported by TYPE and tolerated up to 0.5%: a DNS failure on the load
    generator's laptop (ENOTFOUND) is not a student bug

WHY A WINDOW FOR THE RATE
  Two correct reference runs measured 101% and 70% of the configured rate,
  so the default accepts 60% to 110%. The lower bound catches a team that admits so slowly that
  everyone waits forever; the upper bound has headroom for measurement noise. The HARD limit on
  speed is the cap on POST /orders: exceed it and 429s appear, which fails the "no 429s" rule.

WHAT IT CANNOT CHECK
  Whether an admission is single-use (each fan orders once in the load test, so a reused admission
  never shows up in the counts). contract_check.py replays an order live and checks that.
"""
import argparse
import json
import sys
from pathlib import Path

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"
INVALID = "INVALID"
# Rules whose failure can be nothing more than requests the load generator lost.
EFFECT_OF_CLIENT_ERRORS = {"Every bot turned away (403)", "Everyone who joined was admitted",
                           "Admission rate near configured", "Virtual users that failed", "Client-side errors"}
WINDOW_SECONDS = 10.0
BOT_SCENARIO = "Bot skips the line"


def _count(counters, key):
    return int(counters.get(key, 0))


def load(path):
    """Parse an Artillery JSON file into the handful of facts the rules need."""
    data = json.loads(Path(path).read_text())
    agg = data["aggregate"]
    c = agg.get("counters", {})
    windows = [int(p.get("counters", {}).get("http.codes.201", 0)) for p in data.get("intermediate", [])]
    error_windows = [sum(int(v) for key, v in p.get("counters", {}).items() if key.startswith("errors."))
                     for p in data.get("intermediate", [])]
    return {
        "name": Path(path).stem,
        "joins": _count(c, "http.codes.202"),
        "orders": _count(c, "http.codes.201"),
        "rejected": _count(c, "http.codes.403"),
        "throttled": _count(c, "http.codes.429"),
        "server_errors": sum(int(v) for k, v in c.items() if k.startswith("http.codes.5")),
        "requests": _count(c, "http.requests"),
        "vusers": _count(c, "vusers.created"),
        "failed": _count(c, "vusers.failed"),
        "admitted": _count(c, "waiting_room.admitted"),
        "gave_up": _count(c, "waiting_room.gave_up"),
        "bots": _count(c, "vusers.created_by_name." + BOT_SCENARIO),
        "client_errors": {k[len("errors."):]: int(v) for k, v in c.items() if k.startswith("errors.")},
        "order_windows": windows,
        "error_windows": error_windows,
    }


def steady_rate(order_windows):
    """Orders per second over the steady middle: windows that had orders, minus the first three
    (ramp-up) and the last (tail). None if that leaves too few windows to mean anything."""
    busy = [w for w in order_windows if w > 0]
    middle = busy[3:-1]
    if len(middle) < 4:
        return None
    return sum(middle) / (WINDOW_SECONDS * len(middle))


def assess_validity(f, is_waiting_room, max_error_rate):
    """(invalid?, explanation). Invalid = heavy client errors, bunched in time, with a quiet server."""
    n_err = sum(f["client_errors"].values())
    if n_err == 0:
        return False, "no client-side errors"
    rate = n_err / f["requests"] if f["requests"] else 1.0
    w = f["error_windows"]
    best_two = max((w[i] + w[i + 1] for i in range(len(w) - 1)), default=max(w, default=0))
    share = best_two / n_err
    server_quiet = f["server_errors"] == 0 and (f["throttled"] == 0 or not is_waiting_room)
    why = (f"{n_err} client-side errors ({rate*100:.1f}% of requests), {share*100:.0f}% of them within 20 seconds; "
           f"{f['server_errors']} server errors")
    return rate > max_error_rate and share >= 0.8 and server_quiet, why


def check(name, status, measured, expected=""):
    return {"check": name, "result": status, "measured": measured, "expected": expected}


def run_checks(f, configured_rate=10.0, min_ratio=0.60, max_ratio=1.10,
               max_error_rate=0.005, expect_failures=None):
    out = []
    is_waiting_room = f["joins"] > 0 or f["admitted"] > 0
    pct = lambda a, b: (a / b * 100) if b else 0.0

    if is_waiting_room:
        # 1. the gate is not advisory
        if f["bots"] == 0:
            out.append(check("Every bot turned away (403)", SKIP, "no bots in this run"))
        else:
            lost = max(0, f["bots"] - f["rejected"])
            explained = lost == 0 or lost <= sum(f["client_errors"].values())    # a shortfall must be explained by errors
            note = f" ({lost} lost to client errors)" if lost and explained else ""
            out.append(check("Every bot turned away (403)", PASS if explained else FAIL,
                             f"{f['rejected']} rejections for {f['bots']} bots{note}", "all of them"))
        # 2. nothing gets in without an admission
        out.append(check("No order without admission",
                         PASS if f["orders"] <= f["admitted"] else FAIL,
                         f"{f['orders']} orders, {f['admitted']} admitted", "orders <= admitted"))
        # 3. everyone served
        served = f["admitted"] / f["joins"] if f["joins"] else 0.0
        out.append(check("Everyone who joined was admitted", PASS if served >= 0.995 else FAIL,
                         f"{f['admitted']} of {f['joins']} ({served*100:.2f}%)", ">= 99.5%"))
        out.append(check("No visitor gave up", PASS if f["gave_up"] == 0 else FAIL, str(f["gave_up"]), "0"))
        # 4. the admission rate, over the steady middle
        rate = steady_rate(f["order_windows"])
        if rate is None:
            out.append(check("Admission rate near configured", SKIP,
                             "too few full windows", "needs a stampede-size run, not a smoke test"))
        else:
            ratio = rate / configured_rate
            out.append(check("Admission rate near configured",
                             PASS if min_ratio <= ratio <= max_ratio else FAIL,
                             f"{rate:.2f}/s ({ratio*100:.0f}% of {configured_rate:g})",
                             f"{min_ratio*100:.0f}-{max_ratio*100:.0f}% "
                             f"({configured_rate*min_ratio:.1f}-{configured_rate*max_ratio:.1f}/s)"))
    else:
        total = f["vusers"]
        share = f["throttled"] / total if total else 0.0
        out.append(check("Baseline (no waiting room)", INFO,
                         f"{f['orders']} orders accepted, {f['throttled']} rejected with 429 ({share*100:.0f}%)"))
        if expect_failures is not None:
            out.append(check("Baseline fails visibly", PASS if share >= expect_failures else FAIL,
                             f"{share*100:.0f}% of requests got a 429", f">= {expect_failures*100:.0f}%"))

    if is_waiting_room or expect_failures is None:
        out.append(check("No 429s (nothing throttled)", PASS if f["throttled"] == 0 else FAIL,
                         str(f["throttled"]), "0"))
    out.append(check("No server errors (5xx)", PASS if f["server_errors"] == 0 else FAIL,
                     str(f["server_errors"]), "0"))
    if f["vusers"]:
        failed_share = f["failed"] / f["vusers"]
        out.append(check("Virtual users that failed", PASS if failed_share <= max_error_rate else FAIL,
                         f"{f['failed']} of {f['vusers']} ({failed_share*100:.2f}%)", f"<= {max_error_rate*100:g}%"))
    n_err = sum(f["client_errors"].values())
    if n_err == 0:
        out.append(check("Client-side errors", PASS, "none"))
    else:
        types = ", ".join(f"{k} x{v}" for k, v in sorted(f["client_errors"].items()))
        rate = n_err / f["requests"] if f["requests"] else 1.0
        out.append(check("Client-side errors", INFO if rate <= max_error_rate else FAIL,
                         f"{types} ({rate*100:.3f}% of requests)", f"<= {max_error_rate*100:g}% (counted, not held against you)"))
    invalid, why = assess_validity(f, is_waiting_room, max_error_rate)
    out.append(check("Load generator health", INVALID if invalid else PASS, why,
                     "errors rare, or spread out, or the server erred too"))
    if invalid:
        for r in out:
            if r["result"] == FAIL and r["check"] in EFFECT_OF_CLIENT_ERRORS:
                r["result"] = INVALID
    return out


def render(name, results):
    rows = [("CHECK", "RESULT", "MEASURED", "EXPECTED")] + [
        (r["check"], r["result"], r["measured"], r["expected"]) for r in results]
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = [f"=== {name} ===", ""]
    for i, r in enumerate(rows):
        lines.append(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]}".rstrip())
        if i == 0:
            lines.append("-" * (w[0] + w[1] + w[2] + 10 + 24))
    failed = [r["check"] for r in results if r["result"] == FAIL]
    if failed:
        verdict = f"FAIL - {len(failed)} rule(s) failed: " + "; ".join(failed)
    elif any(r["result"] == INVALID for r in results):
        verdict = ("INVALID RUN - do not grade it. The load generator lost many requests the system never saw. "
                   "Rerun (see MIDTERM_TOOLS.md: connection pool, one steady network).")
    else:
        verdict = "PASS - no rule failed"
    lines += ["", f"RESULT: {verdict}"]
    status = {r["check"]: r["result"] for r in results}
    if status.get("No server errors (5xx)") == FAIL:
        lines += ["", "NOTE: a 5xx can also mean AWS is refusing requests because your account's Lambda concurrency quota is low.",
                  "      One 5xx fails this rule for EVERY design, the baseline included. Run `python3 scripts/preflight.py`;",
                  "      if it fails, request a higher quota (see the README) before you look for a bug in your code."]
    if status.get("Baseline fails visibly") == FAIL:
        lines += ["", "NOTE: if you started this run right after a deploy, wait three minutes and run it again:",
                  "      the cap on a new stage can take a short time to take effect."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grade one stampede run against the exam's rules.")
    ap.add_argument("run", help="the JSON written by `artillery run --output`")
    ap.add_argument("--configured-rate", type=float, default=10.0, help="admissions per second you configured (default 10)")
    ap.add_argument("--min-ratio", type=float, default=0.60, help="lowest acceptable measured/configured (default 0.60)")
    ap.add_argument("--max-ratio", type=float, default=1.10, help="highest acceptable measured/configured (default 1.10)")
    ap.add_argument("--max-error-rate", type=float, default=0.005, help="tolerated client-error and failed-user share (default 0.005)")
    ap.add_argument("--expect-failures", type=float, default=None, metavar="FRACTION",
                    help="baseline only: require at least this share of requests to be rejected with 429 (e.g. 0.3)")
    ap.add_argument("--json", action="store_true", help="print the results as JSON instead of a table")
    args = ap.parse_args(argv)
    try:
        facts = load(args.run)
    except (OSError, ValueError, KeyError) as e:
        print(f"Could not read Artillery output: {e}", file=sys.stderr)
        return 2
    results = run_checks(facts, args.configured_rate, args.min_ratio, args.max_ratio,
                         args.max_error_rate, args.expect_failures)
    print(json.dumps(results, indent=2) if args.json else render(facts["name"], results))
    if any(r["result"] == FAIL for r in results):
        return 1
    return 3 if any(r["result"] == INVALID for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
