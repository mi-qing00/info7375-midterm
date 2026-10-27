#!/usr/bin/env python3
"""
contract_check.py - a short live probe of the waiting-room HTTP contract.

Run it against your deployed stack BEFORE a stampede. It plays one honest visitor and a few
cheats, and checks that the system answers the way the contract says it must. A team whose
system fails this probe is not graded on performance: the load test depends on the contract.

    export API_ENDPOINT=https://abc123.execute-api.us-east-1.amazonaws.com/prod    # no trailing slash
    python3 scripts/contract_check.py

It needs a product id that exists in your inventory table. By default it reads the first one
from load-test/products.csv (written by scripts/seed_stampede_inventory.py); or pass --product-id.

WHAT IT DOES (about 15 seconds on an idle system)
   1. joins the line                          -> 202, with a visitor_id, status "waiting"
   2. asks for the status of a malformed id   -> 400
   3. polls until admitted                    -> 200 "admitted" with an admission_token
   4. orders with NO admission                -> 403   (a bot)
   5. orders with a FORGED token              -> 403
   6. orders with the real admission          -> 201   (and this really does place one order)
   7. replays the same admission              -> 403   (an admission is single use)
   8. asks for the status again               -> the token must not be shown a second time (warning only)

Exit code: 0 if nothing FAILED, 1 if something did, 2 if it could not run at all.
It uses only the Python standard library.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def call(method, url, body=None, headers=None, timeout=20):
    """One HTTP request. Returns (status, parsed_json); status is None if the server was unreachable."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return None, {"_error": str(e)}
    try:
        return status, json.loads(raw.decode() or "{}")
    except ValueError:
        return status, {"_raw": raw.decode(errors="replace")[:200]}


def first_product_id(csv_path):
    lines = Path(csv_path).read_text().strip().splitlines()
    return lines[1].strip() if len(lines) > 1 else None


def run_probe(endpoint, product_id, timeout=120.0, poll_cap=2.0):
    """Returns a list of {check, result, detail}. Stops early when later checks cannot make sense."""
    endpoint = endpoint.rstrip("/")
    results = []

    def add(check, ok, detail="", level=FAIL):
        results.append({"check": check, "result": PASS if ok else level, "detail": detail})
        return ok

    order = {"user_id": "contract-probe", "items": [{"product_id": product_id, "quantity": 1}], "total": 9.99}

    # 1. join
    status, j = call("POST", f"{endpoint}/waiting-room/join")
    visitor_id = j.get("visitor_id", "")
    try:
        valid_id = str(uuid.UUID(visitor_id)) == visitor_id.lower()
    except (ValueError, AttributeError):
        valid_id = False
    if not add("Join the line: 202 with a visitor_id and status 'waiting'",
               status == 202 and valid_id and j.get("status") == "waiting",
               f"got {status} {json.dumps(j)[:120]}"):
        hint = " (is a waiting room deployed? cdk deploy -c waiting_room=on)" if status in (403, 404) else ""
        results[-1]["detail"] += hint
        return results
    hint = j.get("poll_interval_seconds")
    pause = min(float(hint), poll_cap) if isinstance(hint, (int, float)) and hint > 0 else poll_cap

    # 2. malformed id
    status, _ = call("GET", f"{endpoint}/waiting-room/status/not-a-uuid")
    add("Status of a malformed id: 400", status == 400, f"got {status}")

    # 3. poll until admitted
    deadline, token, seen = time.monotonic() + timeout, None, []
    while time.monotonic() < deadline:
        status, s = call("GET", f"{endpoint}/waiting-room/status/{visitor_id}")
        seen.append(s.get("status"))
        if status != 200 or s.get("status") not in ("waiting", "admitted"):
            add("Status answers 200 'waiting' or 'admitted'", False, f"got {status} {json.dumps(s)[:120]}")
            return results
        if s.get("status") == "admitted":
            token = s.get("admission_token")
            break
        time.sleep(pause)
    if not add("Admitted within the time limit", token is not None or "admitted" in seen,
               f"{len(seen)} polls, last status {seen[-1] if seen else None}"):
        return results
    if not add("The admitted response carries an admission_token", bool(token), "token missing or empty"):
        return results

    # 4-5. cheats
    status, _ = call("POST", f"{endpoint}/orders", order)
    add("Order with NO admission: 403", status == 403, f"got {status}")
    status, _ = call("POST", f"{endpoint}/orders", order,
                     {"X-Visitor-Id": visitor_id, "X-Admission-Token": "forged-" + uuid.uuid4().hex})
    add("Order with a FORGED token: 403", status == 403, f"got {status}")

    # 6. the honest order
    creds = {"X-Visitor-Id": visitor_id, "X-Admission-Token": token}
    status, body = call("POST", f"{endpoint}/orders", order, creds)
    add("Order with the real admission: 201", status == 201, f"got {status} {json.dumps(body)[:120]}")

    # 7. replay
    status, _ = call("POST", f"{endpoint}/orders", order, creds)
    add("Replaying the same admission: 403 (single use)", status == 403, f"got {status}")

    # 8. the token must not be handed out again
    status, s = call("GET", f"{endpoint}/waiting-room/status/{visitor_id}")
    add("After use, status no longer shows the token", status == 200 and "admission_token" not in s,
        "the spent token is still being returned", level=WARN)
    return results


def render(results):
    w = max(len(r["check"]) for r in results)
    lines = [f"{'CHECK':<{w}}  RESULT  DETAIL", "-" * (w + 30)]
    lines += [f"{r['check']:<{w}}  {r['result']:<6}  {r['detail'] if r['result'] != PASS else ''}".rstrip() for r in results]
    bad = [r["check"] for r in results if r["result"] == FAIL]
    lines += ["", "RESULT: " + (f"FAIL - {len(bad)} check(s) failed: " + "; ".join(bad) if bad else "PASS - the contract holds")]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live probe of the waiting-room HTTP contract.")
    ap.add_argument("--endpoint", default=os.environ.get("API_ENDPOINT"), help="API base URL (default: $API_ENDPOINT)")
    ap.add_argument("--product-id", help="a product id that exists in the inventory table")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds to wait to be admitted (default 120)")
    args = ap.parse_args(argv)
    if not args.endpoint:
        print("No endpoint: set API_ENDPOINT or pass --endpoint.", file=sys.stderr)
        return 2
    pid = args.product_id
    if not pid:
        csv_path = Path(__file__).resolve().parent.parent / "load-test" / "products.csv"
        try:
            pid = first_product_id(csv_path)
        except OSError:
            pid = None
        if not pid:
            print("No product id: run scripts/seed_stampede_inventory.py first, or pass --product-id.", file=sys.stderr)
            return 2
    results = run_probe(args.endpoint, pid, timeout=args.timeout)
    print(render(results))
    return 1 if any(r["result"] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
