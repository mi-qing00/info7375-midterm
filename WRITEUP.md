# INFO 7375 Midterm: Virtual Waiting Room, Option A (DynamoDB)

Team: Qing Mi, Ing-Ruei Huang · Lambda concurrency quota: 1,000 (`preflight.py` PASS) · Declared admission rate: 10 per second

## 1. Design and why we chose it

**How the line works.** Each visitor gets a fixed **admission slot** when they join. One DynamoDB row holds
`next_slot_ms` (the earliest free slot) and `next_position`. A join takes the slot and moves it 1000 / rate ms later
(100 ms at 10/s) in **one conditional** `UpdateItem` (`join_handler.py` 42-75). If the line is empty (`next_slot_ms`
is in the past), a second conditional update resets it and the slot is "now", so a quiet period never banks slots that
would later be released as a burst. Status compares the clock with the visitor's slot (`status_handler.py` 89): before
it, `waiting` with position and estimated wait; after it, the first poll writes the admission record the provided gate
reads. Slots are exactly 1/rate apart in join order: **first come, first served and paced, with no scheduler.**

**Where state lives.** Counter row (`week5-wr-counters`), one row per visitor with position, join time and slot
(`week5-wr-visitors`), and the gate's admission record (`week5-wr-admissions`). On-demand, no VPC, $0 while idle.

**Requests that arrive together.** DynamoDB applies the counter update atomically, so two simultaneous joins get
different slots 100 ms apart; our code never reads the counter and writes it back. If a join finds the line idle but
another join makes it busy first, the reset's condition fails and the join retries as busy. Two polls admitting the same
visitor both try `PutItem` with `attribute_not_exists(visitor_id)`: one wins, the other returns the winner's token, and
only the winner emits `Admitted`. The gate spends a token in one conditional update: one admission, one order.

**Failures.** Any DynamoDB error returns a clean **503** (never a bare 500) and emits no metric. A join that fails after
taking a slot wastes 0.1 s of capacity; a failed poll is retried on the next poll; the gate fails closed.

**Why this design.** Option A gives atomic conditional writes without the VPC and idle cost of Option C. One of us also
built the notebook's reference design: a drainer Lambda started every minute by EventBridge, advancing a serving counter
10 per second and querying a secondary index for admitted positions. It passed too (9.75/s, peak 10.8 orders/s). We chose
time slots because the drainer needs a timer, reserved concurrency and an **eventually consistent** index (a visitor whose
row reaches the index after the counter passes their position is never admitted), and it runs every minute of the day.

**Trade-offs.** (1) Lazy admission: the record is written on the first poll after the slot, so slow pollers order later.
(2) A 10 s poll interval instead of 5 s: polls share the API stage's 500 requests/s limit with joins and orders, so we
halved that load at the cost of more waiting and more bunching of orders. (3) One hot row: the simplest ordering, with a
ceiling of roughly 1,000 writes per second.

## 2. What limits the design, and what breaks first at 10x

The rate sets the wait: about N / rate for the last of N fans. At 10x (about 1,000 joins per second for 15 s), **the API
stage limit of 500 requests per second breaks first**: joins alone are twice the limit, and about 14,000 waiting
visitors polling every 10 s add about 1,400 per second, so API Gateway answers 429. With that limit raised, the **counter
row** is next: every join writes the same item, which handles roughly 1,000 writes per second, so joins are throttled
(503). Even then, 14,600 fans at 10 per second take about 24 minutes, longer than the 120 polls (about 20 minutes) a fan
waits, and the 40/s orders cap leaves little room to raise the rate.

## 3. Our numbers against the baseline

| | Baseline (no waiting room) | Final run (`final.json`) | Run 1 (`mine.json`) |
|---|---|---|---|
| Fans served / rejected | 1,019 accepted, **501 rejected with 429 (33%)** | **1,463 of 1,463 admitted** | 1,433 of 1,433 |
| Bots turned away (403) | none (ordered like fans) | 57 of 57 | 87 of 87 |
| 429 / 5xx | 501 / 0 | 0 / 0 | 0 / 0 |
| Rate, declared vs measured | n/a | 10 vs **9.78/s (98%)** | 10 vs 9.97/s (100%) |
| Wait, median / p95 / max | none (answered in ~0.1 s) | **70.1 / 127.8 / 139.2 s** | 68.7 / 125.2 / 134.9 s |
| Peak orders accepted (best 10 s) | **60.7/s** | **12.7/s** | 13.3/s |
| Requests sent / peak load | 1,520 / n/a | 13,024 / 161.6 per s | 12,552 / 149 per s |

The fan/bot mix differs between runs because Artillery picks each scenario at random by weight.

**Rate.** Slots are exactly 10 per second, but `check_run.py` counts orders, and a fan orders on the first poll after
their slot, up to one interval later. The first windows bunch (127 and 103 orders), pulling orders forward; the scorer
drops those ramp windows, and the twelve steady windows average 1,174 / 120 s = 9.78/s.

**Waits.** About 19 early fans are admitted at once; slots for the rest start at doors open (t0). The median fan joins at
about t0 + 7.5 s at position about 740, slot about t0 + 72 s: 65 s plus an average poll delay of about 5 s predicts 70 s;
we measured 70.1 s. The last fan joins at t0 + 15 s at position 1,463, slot t0 + 144 s: 129 s plus up to one interval
(8 to 12 s) predicts at most about 141 s; we measured 139.2 s. The baseline had no wait because it had no line: a third
of the crowd got an error instead.

**Peak orders.** The baseline's 60.7/s exceeds even a strict 40/s cap with a burst of 100 (at most 50/s over 10 s), so
the cap leaks; we kept orders near a quarter of it. Our 12.7/s peak is the poll bunching above.

**Expected versus measured.** We predicted a peak of about 250 requests per second and measured 161.6: our estimate added
the join peak and the poll peak, but a fan's first poll comes one interval after joining, so joins peaked at 18:40:10
and polls at 18:40:20.

## 4. One thing we would change

**An adaptive poll interval, chosen at join from the estimated wait** (about a quarter of it, between 2 and 15 s). Fans
near the front would order close to their slot, removing the 12.7/s bunching; fans far back would poll rarely, cutting
the 10,041 status requests. The load test reads `poll_interval_seconds` once from the join response, so it fits the
contract.

*Evidence in* `evidence/` *and the run files. Tests: 29 (5 concurrency cases);* `tests/mutation_check.py` *catches 11 of 11
deliberate bugs.*
