# DESIGN_SPEC: Option A, DynamoDB waiting room

The single source of truth for both teammates (and both AI sessions). Change it only by agreement, in its own commit.
CONTRACT.md section 4 is the external contract; this file is our internal design.

## 1. Idea in one paragraph

Every visitor gets a fixed **admission time slot** at the moment they join. One DynamoDB row (the counter row) holds
`next_slot_ms`, the earliest free slot. Each join atomically takes that slot and pushes it forward by
`1000 / ADMIT_PER_SECOND` ms (100 ms at 10/s). If the line is empty (`next_slot_ms` is in the past) the visitor's slot
is "now". Status only compares `now >= admit_at_ms`: no scheduler, no background job. Because slots are spaced exactly
1/rate apart in join order, admissions are first come first served and paced at the configured rate whenever there is
a backlog. This is the professor's `queue_position` / `serving_counter` design with the serving counter computed from
time instead of advanced by a timer (EventBridge runs at most once a minute, which would admit 600 at once and break
the orders cap's burst of 100).

## 2. State (where it lives)

| Table (CDK name) | Key | Rows | Written by | Read by |
|---|---|---|---|---|
| `week5-wr-counters` | `counter_id` (S) | one row, `counter_id = "line"`: `next_slot_ms` (N), `next_position` (N) | join | (nobody else) |
| `week5-wr-visitors` | `visitor_id` (S) | one per visitor: `position` (N), `joined_at_ms` (N), `admit_at_ms` (N), `ttl` (N, epoch s, now + 1 day) | join | status |
| `week5-wr-admissions` (provided) | `visitor_id` (S) | the gate's record: `admission_token` (S), `used` (BOOL), `expires_at` (N), `ttl` (N), plus `admitted_at` (N) | status (create), gate (sets used) | status, gate |

All three: on-demand billing, `RemovalPolicy.DESTROY`. Idle cost $0.

## 3. Environment variables

| Var | Join | Status | Value in CDK | Value in tests |
|---|---|---|---|---|
| `COUNTERS_TABLE` | yes | no | `week5-wr-counters` | `wr-counters` |
| `VISITORS_TABLE` | yes | yes | `week5-wr-visitors` | `wr-visitors` |
| `ADMISSIONS_TABLE` | no | yes | provided | `wr-admissions` |
| `ADMIT_PER_SECOND` | yes | yes | `-c admit_per_second` (default 10) | `10` |
| `POLL_INTERVAL_SECONDS` | yes | yes | `10` (was 5, see section 7) | `10` |
| `ADMISSION_VALID_SECONDS` | no | yes | `600` | `600` |

## 4. Join: `POST /waiting-room/join`  (lambdas/waiting_room/join_handler.py)

1. `now_ms = int(time.time() * 1000)`, `interval_ms = max(1, round(1000 / ADMIT_PER_SECOND))`.
2. Take a slot, at most 5 attempts:
   - **busy line**: `UpdateItem(Key={"counter_id": "line"}, UpdateExpression="SET next_slot_ms = next_slot_ms + :i ADD next_position :one", ConditionExpression="next_slot_ms >= :now", ReturnValues="UPDATED_NEW")` -> `admit_at_ms = new next_slot_ms - interval_ms`.
   - on `ConditionalCheckFailedException`, **idle line**: `UpdateItem(..., UpdateExpression="SET next_slot_ms = :now_plus_i ADD next_position :one", ConditionExpression="attribute_not_exists(next_slot_ms) OR next_slot_ms < :now", ReturnValues="UPDATED_NEW")` -> `admit_at_ms = now_ms`.
   - on `ConditionalCheckFailedException` again (someone moved it in between), go back to busy.
   - `position = next_position` from the same response.
3. `visitor_id = str(uuid.uuid4())`; `PutItem` the visitor row (section 2).
4. `telemetry.joined()` (only after step 3 succeeded; exactly once per visitor).
5. Return **202**: `{"visitor_id", "status": "waiting", "poll_interval_seconds": POLL_INTERVAL_SECONDS, "poll_path": "/waiting-room/status/<id>", "position": int, "estimated_wait_seconds": number}`.
6. Any `ClientError` / `BotoCoreError`, or 5 failed attempts -> **503** `{"error": "waiting_room_unavailable"}`. Never a bare 500.

## 5. Status: `GET /waiting-room/status/{visitor_id}`  (lambdas/waiting_room/status_handler.py)

1. `visitor_id = event["pathParameters"]["visitor_id"]`. If it is not a canonical lowercase UUID (`str(uuid.UUID(v)) == v`) -> **400** `{"error": "malformed_visitor_id"}`.
2. `GetItem` visitor row. Missing -> **404** `{"error": "unknown_visitor"}`.
3. `now_ms < admit_at_ms` -> **200** `{"status": "waiting", "position", "ahead": ceil((admit_at_ms - now_ms) / interval_ms), "estimated_wait_seconds"}`.
4. Otherwise `GetItem(ConsistentRead=True)` the admission record:
   - exists and `used` is true -> **200** `{"status": "admitted", "order_placed": true}` (no token).
   - exists, unused, `expires_at > now` -> **200** `{"status": "admitted", "admission_token", "expires_at"}` (the SAME token every time).
   - exists, unused, expired -> **200** `{"status": "expired", "message": "rejoin the line"}`.
   - missing -> create it: `token = secrets.token_urlsafe(32)`, `expires_at = now_s + ADMISSION_VALID_SECONDS`, `ttl = expires_at + 3600`, `PutItem(..., ConditionExpression="attribute_not_exists(visitor_id)")`.
     - **won**: `telemetry.admitted()`, `telemetry.time_in_line((now_ms - joined_at_ms) / 1000)`, return the token.
     - `ConditionalCheckFailedException` (a simultaneous poll won): `GetItem(ConsistentRead=True)` and return ITS token. No telemetry.
5. Any other AWS error -> **503** `{"error": "waiting_room_unavailable"}`.

All responses: `{"statusCode", "headers": {"Content-Type": "application/json"}, "body": json.dumps(...)}`.
Numbers read from DynamoDB come back as `Decimal`: convert with `int()` before `json.dumps`.

## 6. CDK  (cdk/waiting_room.py)

- Add the two tables; pass the env vars of section 3; `POLL_INTERVAL_SECONDS = "10"`; memory 256 MB.
- Grants: join -> counters read/write, visitors write. Status -> visitors read, admissions read/write.
- `self.functions = [join, status]`.
- `dashboard_widgets()` returns `wr_widgets.build(self)` from `cdk/wr_widgets.py` (max 3 widgets).
- Do not touch the routes the skeleton adds, the admissions table name or its key.

## 7. Numbers we design to (verify in the stampede)

- Crowd: ~1,520 visitors, ~1,444 fans, ~76 bots; 2/s for 10 s then 100/s for 15 s (CONTRACT.md 123-126).
- Rate 10/s -> the line empties ~144 s after doors open; orders reach `/orders` at ~10/s + ~5/s bots, well under 40/s.
- Stage limit 500 req/s for ALL routes (stack.py 337-338). Status load = waiting / poll interval: ~1,350 / 5 = 270/s at 5 s,
  ~135/s at 10 s. Total peak ~385/s at 5 s vs ~250/s at 10 s. One 429 anywhere fails the scorecard: we use 10 s.
- Expected wait for the last fan: ~144 s + up to one poll interval.
- First thing to break at 10x the crowd: joins alone (1,000/s) exceed the 500/s stage limit; next, the single counter row
  (one DynamoDB item handles roughly 1,000 writes/s).

## 8. Ownership (updated 2026-10-27 15:30)

Qing built the option, the tests and the widgets (with Claude). Teammate: review everything (both must be able to
explain it), draft WRITEUP.md, and check the numbers against the run files.

| File | Status |
|---|---|
| `lambdas/waiting_room/join_handler.py`, `status_handler.py`, `cdk/waiting_room.py` | done, deployed, contract PASS, stampede 1 PASS |
| `cdk/wr_widgets.py` (3 widgets: pacing, front-door headroom, counter row) | done, not yet deployed |
| `tests/test_join.py`, `test_status.py`, `test_concurrency.py`, `wr_helpers.py`, `mutation_check.py` | done: 29 tests, 11 of 11 mutations caught |
| `WRITEUP.md` | teammate drafts, both edit |
| `MIDTERM_NOTES.md`, `evidence/`, deploys and runs (Qing's account) | Qing |
