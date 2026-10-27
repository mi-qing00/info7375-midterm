# INFO7375 Midterm Starter: The Virtual Waiting Room

Read `CONTRACT.md` first. It is the assignment. This file explains how to work in this repository.
`RUBRIC.md` says what each grading component looks like at full credit.

## What is in the box

| Path | What it is | Yours to change? |
|---|---|---|
| `cdk/waiting_room.py` | the skeleton of your waiting room | **yes** |
| `lambdas/waiting_room/` | your Lambda code; the two handlers return 501 until you write them | **yes**, except `telemetry.py` |
| `tests/` | your tests; `test_starter_wiring.py` checks the plumbing | **yes**, add yours |
| `cdk/stack.py`, `cdk/app.py` | the baseline stack and its wiring to your construct | no |
| `lambdas/order_producer/` | the order endpoint and the **gate** | no |
| `lambdas/*_consumer/` | the three baseline consumers | no |
| `lambdas/waiting_room/telemetry.py` | the metrics helper | no |
| `load-test/` | the load tests | no |
| `scripts/` | the tools | no |
| `kit_manifest.json` | checksums of every file marked "no" | no |

`python3 scripts/verify_kit.py` tells you whether you changed a provided file. Run it now, once, to see a PASS.

## One-time setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r cdk/requirements.txt -r requirements-dev.txt
npm install -g artillery                     # if you do not have it
export AWS_DEFAULT_REGION=us-east-1
unset INVENTORY_TABLE                        # a leftover from another week can point the seed script at the wrong table
```

Then check your account. Run this first:

```bash
python3 scripts/preflight.py
```

Every request in the stampede runs in a Lambda function, and they all share one account-wide concurrency quota. AWS lets a new account start as low as 10, and with a quota that low the stampede fails for EVERY design, the baseline included.
The reference runs used the AWS default of 1,000. A quota below 1,000 is untested, and below 100 is expected to fail. If it warns or fails, request an increase now (Service Quotas, AWS Lambda, Concurrent executions) and tell the instructor. You
can still build, test and rehearse. If your stampede then shows 5xx errors, keep the `preflight.py` output and a dashboard screenshot taken during the run (the throttles and the account total): they show the cause, and a run that fails only
because of the quota is not treated as a design failure.

## Step 0: see the problem (about 15 minutes)

Before you build anything, run the same crowd with no waiting room, so you see what you are fixing. Deploy with ONLY the cap, run the direct stampede, and score it (set `API_ENDPOINT` exactly as in step 4 below):

```bash
(cd cdk && cdk deploy -c orders_rate_limit=40)
python3 scripts/seed_stampede_inventory.py
sleep 180        # the cap on a new stage takes a short time to take effect
artillery run -e stampede load-test/stampede-direct.yml --output baseline.json
python3 scripts/check_run.py baseline.json --expect-failures 0.3
python3 scripts/summarize_stampede.py baseline.json
```

Roughly four in ten requests are rejected with HTTP 429 (Too Many Requests): the order endpoint's limit. Keep `baseline.json`. You hand it in, and you compare your waiting room's run against it. You do not need to destroy the stack before step 3: deploy again with `-c waiting_room=on` and your waiting room is added.

## The working loop

1. Write your construct (`cdk/waiting_room.py`) and your handlers (`lambdas/waiting_room/`).
2. Test without AWS: `pytest` from the repository root.
3. Check the cost, then deploy:

   ```bash
   (cd cdk && cdk synth -c waiting_room=on -c orders_rate_limit=40 > /dev/null) && python3 scripts/cost_guard.py
   (cd cdk && cdk deploy -c waiting_room=on -c orders_rate_limit=40)
   ```

   Your admission rate defaults to 10 per second; change it with `-c admit_per_second=N`.
4. Seed the inventory, probe the contract, then run the load tests (wait about three minutes after a deploy before the
   stampede: the cap on a new stage takes a short time to take effect):

   ```bash
   export API_ENDPOINT=$(aws cloudformation describe-stacks --stack-name Week5EventDrivenStack \
     --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text | sed 's:/*$::')
   python3 scripts/seed_stampede_inventory.py
   python3 scripts/contract_check.py                       # must PASS before any load test
   artillery run -e rehearsal load-test/stampede-waiting-room.yml
   artillery run -e stampede  load-test/stampede-waiting-room.yml --output mine.json
   python3 scripts/check_run.py mine.json --configured-rate 10     # declare YOUR rate
   python3 scripts/summarize_stampede.py mine.json
   ```
5. Before you hand in: `python3 scripts/verify_kit.py`
6. When you finish: `(cd cdk && cdk destroy --force)`

## Testing your handlers

Your handlers create their AWS clients when they are imported, so each test starts a mock and then imports the handler fresh. `tests/conftest.py` does the plumbing:

```python
from conftest import load_module

def test_join_answers_202(aws):                       # `aws` is a fresh mocked AWS
    join = load_module("waiting_room/join_handler.py")
    response = join.lambda_handler({}, None)
    assert response["statusCode"] == 202
```

Add anything else your option needs (a queue, an environment variable) to the `aws` fixture. A test that cannot fail is worth nothing: check that yours fail when you break the logic.

## Telemetry and the dashboard

Call the provided helper in your handlers, once per visitor:

```python
import telemetry
telemetry.joined()                  # when they get a place in line
telemetry.admitted()                # when their admission record is written
telemetry.time_in_line(seconds)     # at admission: seconds since they joined
```

The deploy creates a dashboard named **Info7375-Week5-Midterm** with twelve common panels, three of them drawn from those metrics. Add up to three widgets of your own by returning them from `WaitingRoom.dashboard_widgets()`, and add every Lambda you create to `self.functions` so the errors panel counts it. Metrics take about a minute to appear.

## If something looks wrong

| What you see | What it means |
|---|---|
| `403` with `{"message":"Missing Authentication Token"}` on `/waiting-room/join` or `/waiting-room/status/...` | That route is not deployed: you deployed without `-c waiting_room=on` (Step 0 deploys the baseline on purpose). Deploy again with `-c waiting_room=on -c orders_rate_limit=40`. This is not a problem with tokens or with the gate. |
| `501` with `not_implemented` | The route is deployed and is answering with the starter's stub: you have not written that handler yet. |
| `403` with `admission_required` or `invalid_admission` on `POST /orders` | The provided gate turned the order away: no admission, a wrong or already used token, or an expired one. The gate is doing its job. |
| `503` from `POST /orders` | The gate could not check the admission and failed closed. Check that your admissions table is the one `cdk/waiting_room.py` hands to the stack, and that your handler writes the record described in `CONTRACT.md`, section 4. |
| `AccessDeniedException` or `is not authorized to perform` in a function's log (CloudWatch Logs) | That function's role lacks a permission your design now needs. As shipped, only the status function can use the admissions table, and no function can use anything you add. Grant what is missing in `cdk/waiting_room.py` and redeploy. |
| `5xx` errors in a stampede, and CloudWatch shows Lambda throttles | Your account's Lambda concurrency quota is too low. That fails every design, the baseline included, and it is not a bug in your code. Run `python3 scripts/preflight.py`; if it fails, request a quota increase (Service Quotas, AWS Lambda, Concurrent executions) and tell the instructor. Keep a dashboard screenshot from the run (the throttles and the account total) with your hand-in. |
| The baseline gets few 429s (the "Baseline fails visibly" rule fails) | You most likely ran it right after the deploy: the cap on a new stage takes a short time to take effect. Wait three minutes and run it again. |
| `cost_guard.py` says the `POST /orders` cap must be exactly 40, or that it has no cap | The synthesized template does not carry the exam's cap. Deploy with `-c orders_rate_limit=40`, and do not change the throttle from your own files. |

## Rules in one place

Do not change a provided file, do not add files inside `scripts/`, `load-test/` or the provided `lambdas/` folders, and do not raise or remove the cap. `verify_kit.py` detects the first two. The cap lives in the synthesized template, so `cost_guard.py` reads that: it fails unless `POST /orders` is capped at exactly 40 per second with a burst of 100, however the template got that way. The instructor re-synthesizes your repository and checks all three again. Put your own files in `cdk/`, `lambdas/waiting_room/` and `tests/`.
