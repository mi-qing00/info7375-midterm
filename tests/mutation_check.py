"""
mutation_check.py - break our handlers on purpose, one mutation at a time, and confirm our tests FAIL.
A test suite that cannot fail is worth nothing (README). Run from the repository root:

    python3 tests/mutation_check.py        # exit 0 = every mutation was caught

Each file is restored from its .bak copy right after its pytest run. If you interrupt it (Ctrl+C), check git status.
"""
import shutil, subprocess, sys
J="lambdas/waiting_room/join_handler.py"; T="lambdas/waiting_room/status_handler.py"
M=[
 ("no pacing: every slot is 'now'", J, 'return int(a["next_position"]), int(a["next_slot_ms"]) - INTERVAL_MS', 'return int(a["next_position"]), now_ms'),
 ("idle attempt unconditional (overwrites a busy line)", J, 'ConditionExpression="attribute_not_exists(next_slot_ms) OR next_slot_ms < :now",', ''),
 ("busy attempt unconditional", J, 'ConditionExpression="next_slot_ms >= :now",', ''),
 ("joined counted before the write", J, '        visitor_id = str(uuid.uuid4())\n', '        telemetry.joined()\n        visitor_id = str(uuid.uuid4())\n'),
 ("admit before the slot", T, 'if now_ms < admit_at_ms:', 'if False:'),
 ("admission write unconditional (second token overwrites first)", T, 'put_item(Item=record, ConditionExpression="attribute_not_exists(visitor_id)")', 'put_item(Item=record)'),
 ("no read of an existing admission", T, '    if existing:\n        return _admitted_view(existing, now_s)\n', ''),
 ("spent token shown again", T, '    if record.get("used"):', '    if False:'),
 ("no expiry check", T, '    if int(record["expires_at"]) <= now_s:', '    if False:'),
 ("no id validation", T, '    if not _valid_uuid(visitor_id):', '    if not visitor_id:'),
 ("telemetry counted on the losing poll too", T, '        winner = _admissions.get_item(Key={"visitor_id": visitor_id}, ConsistentRead=True)["Item"]\n', '        winner = _admissions.get_item(Key={"visitor_id": visitor_id}, ConsistentRead=True)["Item"]\n        telemetry.admitted()\n'),
]
missed = 0
for name, f, a, b in M:
    orig = open(f).read(); assert orig.count(a) == 1, name
    shutil.copy(f, f + ".bak"); open(f, "w").write(orig.replace(a, b))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_join.py", "tests/test_status.py", "tests/test_concurrency.py"], capture_output=True, text=True)
    shutil.move(f + ".bak", f)
    last = r.stdout.strip().splitlines()[-1]
    print(f"{'CAUGHT' if r.returncode else 'MISSED'} | {name} | {last}")
    missed += 0 if r.returncode else 1
print(f"{len(M) - missed} of {len(M)} mutations caught")
sys.exit(1 if missed else 0)
