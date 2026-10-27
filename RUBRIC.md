# Rubric

Your execution score is out of 100. Your final grade is the difficulty coefficient (B 1.0, A 1.04, C 1.08) times it.
The hard gate comes first: without a passing `contract_check.py` output in your submission, your performance results are not graded.

| Component | Points | Full credit looks like | Partial credit looks like | Little or none |
|---|---|---|---|---|
| **Correctness evidence** | 30 | A valid run on your own stack where every scorecard rule passes, scored with the rate you declared | A valid run where one rule fails, or a pass whose declared rate does not match what you configured | Only the rehearsal passes, several rules fail, or there is no run file |
| **Design and rationale** | 20 | Explains how the line works, how admission is paced, where state lives and why, what happens when requests arrive together, what happens when something fails, and names the trade-offs honestly | Describes what the code does but not why | Restates the contract |
| **Tests** | 15 | Cover joining, status, admission and the single-use rule, plus at least two cases where things happen at the same time, and fail when the logic is broken | Happy path only | Tests that cannot fail |
| **Interpretation** | 15 | Uses your own numbers, including your baseline run, to explain declared against measured rate, the waits, and the peak load, and names what actually limits your design | Reports the numbers with little explanation | Numbers that do not match your run file |
| **Deployment and cost** | 10 | Deploys cleanly from nothing, `preflight.py` and `cost_guard.py` are OK, `verify_kit.py` passes, and the stack is destroyed | Warnings ignored, or something left running | A `cost_guard.py` failure ignored |
| **Telemetry** | 10 | The three standard metrics emitted once per visitor at the right moments, at least one widget of your own that shows something that matters for your design, a clear title, and a screenshot taken during the run | Metrics emitted at the wrong moments, or no widget of your own | No telemetry |

## How your numbers are checked

The instructor reruns `check_run.py` on your raw run file, runs `verify_kit.py` against the master manifest, and compares what you wrote with what the file says. A run that says INVALID RUN (exit code 3) is not graded: rerun it. A run that fails only because your account's Lambda quota is too low (see `preflight.py`) is not treated as a design failure: it is graded on the rules it can still satisfy.

## Integrity

AI assistants are allowed. You must be able to explain everything you hand in. Copying another team's work, editing a provided file, or editing a run file is academic misconduct.
