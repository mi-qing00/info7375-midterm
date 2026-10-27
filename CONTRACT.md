# INFO7375 Midterm: The Virtual Waiting Room

Teams of two. 24 hours. AI assistants are allowed (section 8).

| | |
|---|---|
| Released | 27 October |
| Due | 28 October 1:30P |
| Hand in to | Canvas |

## 1. The scenario

Tickets for a famous tour go on sale. A few fans trickle in before the doors open. Then about 1,500 people arrive within 15 seconds, and about 5 in every 100 of them are bots that skip the line and go straight for the order button.

You have the Week 5 event-driven order system. It accepts every order the instant it arrives. A real backend cannot do that, so in this exam the order endpoint is limited to **40 requests per second** (with a burst allowance of 100).
Send the whole crowd straight at it and about four in ten fans get an error. Nobody is served fairly, and the bots do well.

Your job is to put a **virtual waiting room** in front of `POST /orders`:

- every fan joins a line and can ask where they stand;
- fans are let through at a steady rate the backend can take;
- bots without a place in line are turned away;
- the order endpoint never receives more than it can handle; and
- **every fan is served.**

## 2. Choose one option

Each team builds exactly one option. The three designs were discussed in class; the implementation is yours.

| Option | Design | Difficulty coefficient |
|---|---|---|
| **B** | An SQS admission queue, drained at a controlled rate | 1.0 |
| **A** | A DynamoDB waiting room (an atomic counter) | 1.04 |
| **C** | A Redis (ElastiCache) INCR counter | 1.08 |

Your grade is the **difficulty coefficient times your execution score** (section 6). A harder option is worth more only if it works.

## 3. What you receive and what you write

**Provided. Do not modify these files:**

- the Week 5 baseline system: the API, `OrderProducer`, the three consumers and their tables;
- the **gate**, built into `OrderProducer`: every order must present a valid admission, which the gate verifies and
  consumes;
- the load tests in `load-test/`;
- the tools in `scripts/` (table below);
- a one-page dashboard with the shared panels and a slot for your option-specific widgets;
- a skeleton that shows where your option plugs in.

**You write:**

- your option's logic and infrastructure;
- your own tests, including the cases where several things happen at the same time;
- the three standard telemetry calls (one line each, from the provided `telemetry.py`) and your option-specific
  dashboard widgets;
- a short written explanation (section 7).

The starter's `README.md` says exactly where each piece goes, and `RUBRIC.md` says what each grading component looks like at full credit.

| Tool | When you run it |
|---|---|
| `preflight.py` | first, before anything else; it must PASS |
| `artillery run -e stampede load-test/stampede-direct.yml` | Step 0: the same crowd with no waiting room, so you see the problem |
| `cost_guard.py` | before every deploy; it must say OK |
| `seed_stampede_inventory.py` | after every fresh deploy |
| `contract_check.py` | after the deploy and before any load test; it must PASS |
| `artillery run -e rehearsal ...` | a small trial of about 150 visitors; always do this first |
| `artillery run -e stampede ...` | the real test, about 1,500 visitors, with `--output yourfile.json` |
| `check_run.py` | on your stampede file; it prints the scorecard |
| `summarize_stampede.py` | on the same file; it prints what the backend saw every 10 seconds |
| `verify_kit.py` | before you hand in; it shows you did not change a provided file |

## 4. The HTTP contract

Your waiting room must answer exactly like this. The load test and the probe depend on it.

```
POST /waiting-room/join
  -> 202  {"visitor_id": "<uuid>", "status": "waiting",
           "poll_interval_seconds": <number>, "poll_path": "/waiting-room/status/<uuid>"}

GET /waiting-room/status/{visitor_id}
  -> 200  {"status": "waiting"}
  -> 200  {"status": "admitted", "admission_token": "<token>", "expires_at": <epoch seconds>}
  -> 200  {"status": "admitted", "order_placed": true}     (after the order; the token is no longer shown)
  -> 400  the visitor_id is malformed

POST /orders   with the headers   X-Visitor-Id: <uuid>    X-Admission-Token: <token>
  -> 201  accepted
  -> 403  not admitted: missing, wrong, expired or already used
  -> 503  the gate could not verify the admission (it fails closed)
```

Extra fields in a response are allowed (for example a position in line or an estimated wait). The load test ignores them.

Rules your waiting room must keep:

- The line is first come, first served.
- A visitor who has only joined cannot order.
- An admission is **single use**: one admission buys one order. Until it is used, asking again returns the same token.
- An admission expires.

**The record the gate reads.** The provided gate checks every order against one DynamoDB record per admitted visitor. Your option must write that record when it admits someone:

| Field | Type | Meaning |
|---|---|---|
| `visitor_id` | string, the table's key | the visitor |
| `admission_token` | string | the secret the visitor presents |
| `used` | boolean | `false` when written; the gate sets it to `true` |
| `expires_at` | number, epoch seconds | the gate refuses the token after this time |
| `ttl` | number, epoch seconds | when DynamoDB may delete the record |

The gate accepts an order only if the record exists, the token matches, `used` is still `false`, and `expires_at` is in the future, and it marks the record used in one atomic step. Your admissions table must be the one the starter hands to the gate.

## 5. The capacity rule and your admission rate

**See the problem first.** Before you build, deploy the starter with only `-c orders_rate_limit=40`, run
`artillery run -e stampede load-test/stampede-direct.yml --output baseline.json`, and score it with
`python3 scripts/check_run.py baseline.json --expect-failures 0.3`. Roughly four in ten requests are rejected with HTTP 429 (Too Many Requests). That is the failure your waiting room exists to remove, and `baseline.json` is your comparison for the rest of the exam. Wait about three minutes after the deploy before you run it: the cap on a new stage takes a short time to take effect.

**The cap.** Deploy with `-c waiting_room=on -c orders_rate_limit=40`, as the starter's README shows. `POST /orders` then accepts about 40 requests per second (burst 100) and answers anything beyond that with HTTP 429. **One 429 fails the scorecard.**

**The crowd.** The stampede load test sends about 1,520 visitors in 25 seconds: 10 seconds of early arrivals, then 15 seconds at 100 new visitors per second. About 95% are fans and about 5% are bots.

- A fan joins, then asks for its status about every 5 seconds (the interval varies by up to 20%) until admitted, then places one order.
- A fan gives up after 120 polls, about 10 minutes.
- A bot skips the line and goes straight to `POST /orders`.

**Your admission rate.** You choose how many visitors per second your waiting room admits. It must be:

- **low enough** that the order endpoint stays under the cap, with margin: the order requests that reach it come in bunches, not perfectly evenly; and
- **high enough** that the last fan is admitted long before giving up. For about 1,440 fans, anything under about 3 per second makes some of them give up.

The reference solutions admit 10 per second.

Whatever you choose, **declare it** when you score your run: `python3 scripts/check_run.py yourfile.json --configured-rate N`. The scorecard measures your steady admission rate over the middle of the run, and it must land between 60% and 110% of the rate you declare.

## 6. How you are graded

**Hard gate.** Your submission must include a passing `contract_check.py` output. Without it, your performance results are not graded.

**Execution score (out of 100):**

| Component | Points |
|---|---|
| **Correctness evidence:** the scorecard PASSES on your own stampede run | 30 |
| **Design and rationale:** your written explanation of what you built and why | 20 |
| **Tests:** your own tests of your option's logic, including simultaneous events | 15 |
| **Interpretation:** you explain your own numbers (rate, waits, peak load, and how they differ from your baseline) | 15 |
| **Deployment and cost:** a clean deploy, `cost_guard.py` OK, everything destroyed afterward | 10 |
| **Telemetry:** you emit the three standard metrics at the right moments, and your option-specific dashboard widgets are correct and readable | 10 |

Your final grade is the difficulty coefficient (B 1.0, A 1.04, C 1.08) times this score.

**What the scorecard checks.** Every bot is turned away. No order is accepted without an admission. At least 99.5% of the fans who joined are admitted, and nobody gives up. Your measured rate is within 60% to 110% of the rate you declare. There are no 429s and no 5xx. Failed virtual users and client-side errors stay below 0.5%.

**Exit codes.** `0` means every rule passed. `1` means a rule failed. `3` means **INVALID RUN**: the load generator lost requests that your system never saw. An invalid run is not graded. Rerun it, from a steady network, on power.

## 7. What to hand in

1. Your repository (a zip or a link) with your option's code and your tests.
2. The output of `preflight.py`, and of `cost_guard.py` from before your final deploy.
3. The output of `contract_check.py`.
4. Your raw stampede file (the `.json` that `artillery` wrote), the output of `check_run.py` run on it with your
   declared `--configured-rate`, and the output of `summarize_stampede.py`. The instructor reruns the scorecard on your
   file. Include your baseline run too: `baseline.json`, the output of
   `check_run.py baseline.json --expect-failures 0.3`, and the output of `summarize_stampede.py baseline.json`.
5. The output of `verify_kit.py`.
6. A screenshot of the one-page dashboard during your stampede, showing your option-specific widgets and the Lambda
   errors and throttles and concurrency panels.
7. A written explanation of at most two pages:
   - your design in a paragraph, and why you chose this option;
   - what limits your option: the first thing that would break if the crowd were ten times larger, and why;
   - your numbers: the rate you declared against the rate you measured, the wait times (median, 95th percentile,
     maximum), and the peak orders per second, each compared with your baseline; and an explanation of any gap between what you expected and what
     happened;
   - one thing you would change.
8. Confirmation that you destroyed your stack.

## 8. Rules of the road

- **Do not change provided files.** That includes the gate, the load tests (their timing and connection settings
  too), and the tools. `verify_kit.py` detects changes to provided files. Do not raise or remove the cap, and do not bypass the gate. The cap is
  checked in your synthesized template: `cost_guard.py` fails unless `POST /orders` is capped at exactly 40 per second
  with a burst of 100, and the instructor re-synthesizes your repository.
- **AI assistants are allowed.** You must understand and be able to explain everything you hand in. The written
  explanation and your answers about your own numbers are graded, and they cannot be borrowed. Copying another team's
  work is academic misconduct, and so is editing a run file.
- **Budget.** The whole exam should cost less than $2 of AWS usage. Sending traffic costs cents; resources that stay
  running cost money. Run `cost_guard.py` before every deploy, set an AWS Budgets alert at $2, and destroy your stack
  when you finish.
- **Before you start,** run `python3 scripts/preflight.py`. It checks your Lambda concurrency quota, which every request
  shares: AWS lets a new account start as low as 10, and with a quota that low the stampede fails for EVERY design
  (requests are refused, API Gateway returns them as HTTP 500, and one 5xx fails the scorecard). The reference runs used
  the AWS default of 1,000; below that is untested, and below 100 is expected to fail. If it warns or fails, request an
  increase at once (Service Quotas, AWS Lambda, Concurrent executions) and tell the instructor: approvals are not
  instant. You can still build, test and rehearse. Keep the `preflight.py` output, and take a dashboard screenshot during
  your stampede that shows the throttles and the account total. A run that fails ONLY because of the quota is not treated
  as a design failure: it is graded on the rules it can still satisfy, and it can earn full correctness credit. A rerun after your quota is raised is welcome but not required.
- **Test small first.** Run `-e rehearsal` before `-e stampede`, and run the stampede only after `contract_check.py`
  passes.
- **Running the stampede.** Use a steady network, keep the machine awake and on power, and do not use a VPN.
- **Plan your time.** Some designs create networking or caches, and a deploy of those can take about ten minutes per
  attempt. A failed deploy costs more.
- **One stack at a time.** Do not leave an old stack running while you test a new one.
