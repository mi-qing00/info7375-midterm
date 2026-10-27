# MIDTERM_NOTES (evidence log, Option A)

Append-only. Every number in the write-up must come from a line here or a file in `evidence/`.
Format: time | what was run | raw excerpt | interpretation.

## 2026-10-27 setup

- 12:30 `verify_kit.py --expect-digest a6dc14...` -> `checked 25 provided files ... RESULT: PASS` (evidence/verify_kit_start.txt)
- 12:30 `preflight.py` -> `Lambda concurrency quota: 1,000 (unreserved ... 1,000) PASS` (evidence/preflight.txt)
- 12:33:01 `cdk deploy -c orders_rate_limit=40` -> `Deployment time: 104.85s`, ApiEndpoint `https://hpt5p3c64i.execute-api.us-east-1.amazonaws.com/prod/` (evidence/deploy_baseline.log)
- 12:33 seed -> 5 products, stock 1,000,000 each (evidence/seed_baseline.txt)

## 2026-10-27 baseline (no waiting room, cap 40/s burst 100)

- 12:36:27-12:36:53 `artillery run -e stampede load-test/stampede-direct.yml --output baseline.json` (started ~3.4 min after deploy)
  - `http.requests 1520`, `http.codes.201 1019`, `http.codes.429 501`, `vusers.failed 0`
  - response time p50 68.7 ms, p95 165.7 ms, p99 788.5 ms, max 1009 ms
- `check_run.py baseline.json --expect-failures 0.3` -> `1019 orders accepted, 501 rejected with 429 (33%)`, `Baseline fails visibly PASS`, `RESULT: PASS` (evidence/check_baseline.txt)
- `summarize_stampede.py baseline.json` (evidence/summary_baseline.txt):

```text
window ends   orders  throttled  orders/s
12:36:20          6          0       0.6
12:36:30        292          3      29.2
12:36:40        607        395      60.7
12:36:50        114        103      11.4
Peak orders accepted per second (best 10 s window): 60.7
```

- Interpretation:
  - 33% rejected (contract says "about four in ten"). The excess is shed with 429 and no memory of the visitor: no line, no fairness, bots compete equally.
  - Peak window accepted 607 orders in 10 s = 60.7/s. A strict token bucket (40/s refill, 100 burst) allows at most 40 x 10 + 100 = 500 per 10 s. Measured is above that: API Gateway throttling is approximate, not exact. `unverified` mechanism (AWS documents throttling as best effort). Design consequence: keep our order rate far below 40/s, not near it.
- 12:40 dashboard screenshot of the baseline run. NOTE: this is the OLD page `Info7375-Week5-EventDriven`, not `Info7375-Week5-Midterm` (the hand-in screenshot must be the Midterm page). Values read from it (1-minute points, UTC 16:36-16:37):
  - API Gateway 4xx = 501, 5xx = 0 (matches Artillery's 501 x 429)
  - API Gateway request count = 1.52K (matches 1520 requests)
  - Lambda throttles = 0 for all four functions: the 429s came from the API Gateway method cap, not from Lambda concurrency
  - Lambda concurrent executions max: notification ~53, producer ~35, inventory ~23, analytics ~5 (read off the chart, approximate)
  - Kinesis records written 975 in the first minute; WriteProvisionedThroughputExceeded 0
  - Analytics oldest message age rises to ~33 s (30 s batching window, stack.py 297), DLQs 0
- 12:48 Midterm dashboard (`Info7375-Week5-Midterm`, 16:34-16:39 UTC) screenshot of the baseline: 4xx 501, 5xx 0; errors and throttles, all functions 0; account total concurrent executions peak ~87; Time in line / Joined and admitted / People waiting: "No data available" (no waiting room deployed, as expected).

## 2026-10-27 build (Option A)

- 12:55 wrote lambdas/waiting_room/join_handler.py (slot taking, DESIGN_SPEC section 4), lambdas/waiting_room/status_handler.py (section 5), cdk/waiting_room.py (counters + visitors tables, env, grants, poll 10 s, memory 256, widgets hook).
- 12:58 smoke test in Claude's scratch container (moto, same conftest fixture): 3 passed. Covered: positions 1,2,3 with slots exactly 100 ms apart; idle line gives slot = now (no catch-up burst after 60 s idle); 400 malformed, 404 unknown; same token on repeat poll; gate consumes once then 403; status after use = order_placed with no token; telemetry Joined x3, Admitted x2, TimeInLine x2; 20 threaded joins get positions 1-20 and 20 distinct slots >= 100 ms apart. This is NOT the graded test suite (teammate writes that).
