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
- Qing's Mac: `pytest -q` -> `10 passed in 8.01s` (starter wiring tests still pass with our construct). Commit 3c1cecd pushed.
- `cost_guard.py` -> `POST /orders cap: 40 per second, burst 100 (as the exam requires)`, idle ~$0.36/day (Kinesis shard), `RESULT: OK to deploy` (evidence/cost_guard_wr1.txt)
- 13:14:56 `cdk deploy -c waiting_room=on -c orders_rate_limit=40` -> `Deployment time: 69.22s` (evidence/deploy_wr1.log)
- `contract_check.py` -> all 9 checks PASS, `RESULT: PASS - the contract holds` (evidence/contract_check_wr1.txt). Hard gate met (to be rerun on the final deploy).

## 2026-10-27 rehearsal and stampede 1 (Option A, rate 10/s, poll 10 s)

- 14:26 rehearsal (150 visitors): 146 joins (202), 146 admitted, 146 orders (201), 4 bots -> 4 x 403, 0 x 429, 0 x 5xx, wait min 8.2 s / median 10.1 s / max 12.5 s, 1 poll each. `RESULT: PASS` (rate SKIP: too short). (evidence/check_rehearsal.txt)
- 14:31:13-14:34:07 stampede 1 (`mine.json`, 2 min 40 s): 12,552 requests; 1,433 joins, 9,599 polls (200), 1,433 orders (201), 87 bots -> 87 x 403, 0 x 429, 0 x 5xx, 0 failed vusers, 0 client errors.
  - `check_run.py mine.json --configured-rate 10` -> `Admission rate near configured PASS 9.97/s (100% of 10)`, every rule PASS, `RESULT: PASS - no rule failed` (evidence/check_stampede1.txt)
  - time in line: median 68.7 s, p95 125.2 s, max 134.9 s; polls per visitor median 7, max 13 (far below the 120-poll give-up)
  - peak orders accepted (best 10 s window): baseline 60.7/s -> waiting room 13.3/s (window 14:31:40); steady windows 14:32:00-14:33:50 between 8.9 and 10.9/s
  - request load per 10 s window (joins + polls + orders + bots): 14:31:40 = 951+355+133+51 = 1,490 (149/s, the peak); 14:31:50 = 1,351; 14:32:00 = 1,298. Peak polls 1,203 in 14:32:00 (120/s) = about 1,200 people waiting, each polling once per 10 s.
- Correction to the earlier estimate (DESIGN_SPEC section 7 said ~250/s total at 10 s polling): measured peak is 149/s. The estimate added joins (100/s) and polls of the full line at the same moment, but a visitor's first poll comes one interval (~10 s) after joining, so the join peak (14:31:40) and the poll peak (14:32:00) fall in different windows.
- Interpretation of the 13.3/s window: slots are spaced exactly 0.1 s, but a visitor orders at their first poll AFTER their slot, i.e. up to one poll interval late. Orders of several slot-windows can land in one 10 s window (133) and the next is light (74); the average over the steady middle is 9.97/s. Plausible mechanism, `unverified` beyond these counts.
- 14:56 Midterm dashboard screenshot of stampede 1 (18:30-18:35 UTC): 4xx 87 (= the 87 bots), 5xx 0; errors and throttles, all functions 0; account total concurrent executions peak ~28 (baseline ~87); Visitors admitted ~600 per minute at 18:32 and 18:33 (= 10/s); joined 1.43K at 18:31; People waiting now peak ~1.22K; Time in line p95 rising to ~2.2 min; API Gateway requests peak ~5.54K per minute (~92/s average over that minute).

## 2026-10-27 tests and widgets (Qing + Claude; teammate to review)

- tests/test_join.py (7), tests/test_status.py (13 incl. 5 parametrized bad ids), tests/test_concurrency.py (5): 29 tests, run 3x in Claude's container: `29 passed` each time.
  - Concurrency finding: moto does NOT apply a conditional write atomically across threads (40 threaded joins gave duplicate/misordered slots on the raw mock). Real DynamoDB applies each single-item write and its condition atomically, which is the only guarantee our design relies on. The threaded tests wrap each mocked call in a lock to restore that per-request atomicity; threads still interleave between calls. Two more tests force the worst interleaving deterministically (idle-race on join; competing admission write on status).
- tests/mutation_check.py: 11 deliberate bugs (no pacing, unconditional counter writes, admit before slot, unconditional admission write, spent token shown, no expiry, no id validation, telemetry at wrong moment, ...) -> `11 of 11 mutations caught`. The first version missed "skip the read of an existing admission" (still correct, only costs a failing write per poll); added test_polls_after_admission_only_read.
- cdk/wr_widgets.py: (1) admitted per minute vs rate x 60 annotation; (2) stage requests/s and POST /orders requests/s (1-min averages) vs 500/s and 40/s annotations; (3) counter table UpdateItem throttles, conditional-check failures, p99 latency. Synth in Claude's container: the Midterm dashboard body contains all three titles and annotations; starter wiring tests `10 passed`.
- Qing's Mac: `pytest -q` -> `39 passed in 12.32s`; `mutation_check.py` -> `11 of 11 mutations caught` (evidence/mutation_check.txt); `verify_kit.py` -> 25 OK, `RESULT: PASS` (evidence/verify_kit_wr2.txt)
- 17:49:44 full deploy with widgets -> `Deployment time: 22.16s` (evidence/deploy_final.log); `contract_check.py` -> 9/9 PASS (evidence/contract_check_final.txt)
- Widgets screenshot (data of stampede 1, 18:30-18:35 UTC; the widgets were deployed after the run, CloudWatch draws stored metrics):
  - Line pacing: admitted ~600 per minute at 18:32 and 18:33, on the configured-rate line (600)
  - Front door headroom: whole API ~90/s (1-min avg), POST /orders ~15/s (1-min avg); limits 500/s and 40/s
  - Counter row: throttled 0; conditional-check failures ~11 in the 18:31 minute (the busy/idle switches while the line forms); UpdateItem p99 latency ~13 ms (right axis)
