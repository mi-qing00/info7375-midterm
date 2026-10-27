"""
Guard rails for the provided plumbing.

These pass on the skeleton and must STILL pass after you build your option: they check what the provided stack and
gate depend on. They do not test your logic. Write those tests yourself (tests/conftest.py shows how to mock AWS).

Each synth runs in its own Python process, so a failure here cannot be caused by leftover state from another test.
"""
import json
import subprocess
import sys

from conftest import REPO

SNIPPET = r'''
import json, os, sys, tempfile
repo, context, widgets = sys.argv[1], json.loads(sys.argv[2]), int(sys.argv[3])
os.chdir(os.path.join(repo, "cdk")); sys.path.insert(0, ".")
import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
if widgets:
    import waiting_room
    waiting_room.WaitingRoom.dashboard_widgets = lambda self: [cw.TextWidget(markdown="w%d" % i) for i in range(widgets)]
from stack import EventDrivenStack
app = cdk.App(context=context, outdir=tempfile.mkdtemp())
EventDrivenStack(app, "T", env=cdk.Environment(account="123456789012", region="us-east-1"))
print(json.dumps(app.synth().get_stack_by_name("T").template, default=str))
'''

ON = {"waiting_room": "on", "orders_rate_limit": "40"}
_cache = {}

DASHBOARD_TITLES = [
    "API Gateway - 4xx and 5xx errors", "Lambda concurrent executions", "Lambda invocations",
    "Queue depths (visible messages)", "Oldest message age (s)", "DLQ depths",
    "Lambda errors and throttles", "Lambda duration p99 (ms)", "Time in line (s): median and p95",
    "Visitors joined and admitted (per minute)", "People waiting now (running total)", "API Gateway - request count",
]


def run_synth(context, widgets=0):
    return subprocess.run([sys.executable, "-c", SNIPPET, str(REPO), json.dumps(context), str(widgets)],
                          capture_output=True, text=True, timeout=240)


def synth(context, widgets=0):
    key = (json.dumps(context, sort_keys=True), widgets)
    if key not in _cache:
        result = run_synth(context, widgets)
        assert result.returncode == 0, result.stderr[-2500:]
        _cache[key] = json.loads(result.stdout.strip().splitlines()[-1])
    return _cache[key]


def resources(template, rtype):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == rtype}


def dashboard_body(template, name="Info7375-Week5-Midterm"):
    for res in resources(template, "AWS::CloudWatch::Dashboard").values():
        if res["Properties"].get("DashboardName") == name:
            return json.dumps(res["Properties"]["DashboardBody"])
    raise AssertionError(f"no dashboard named {name}")


def test_without_the_flag_there_is_no_waiting_room():
    parts = {r["Properties"].get("PathPart") for r in resources(synth({}), "AWS::ApiGateway::Resource").values()}
    assert "waiting-room" not in parts


def test_the_flag_adds_your_two_routes():
    template = synth(ON)
    by_part = {r["Properties"]["PathPart"]: k for k, r in resources(template, "AWS::ApiGateway::Resource").items()}
    assert {"waiting-room", "join", "status", "{visitor_id}"} <= set(by_part)
    methods = [(m["Properties"]["HttpMethod"], m["Properties"]["ResourceId"].get("Ref"))
               for m in resources(template, "AWS::ApiGateway::Method").values()]
    assert ("POST", by_part["join"]) in methods
    assert ("GET", by_part["{visitor_id}"]) in methods


def test_the_gate_points_at_your_admissions_table():
    template = synth(ON)
    producer = next(r for r in resources(template, "AWS::Lambda::Function").values()
                    if r["Properties"].get("FunctionName") == "week5-order-producer")
    env = producer["Properties"]["Environment"]["Variables"]
    assert env["WAITING_ROOM_ENABLED"] == "true"
    table = resources(template, "AWS::DynamoDB::Table")[env["WAITING_ROOM_TABLE"]["Ref"]]
    assert table["Properties"]["KeySchema"] == [{"AttributeName": "visitor_id", "KeyType": "HASH"}]
    assert {"AttributeName": "visitor_id", "AttributeType": "S"} in table["Properties"]["AttributeDefinitions"]


def test_the_common_dashboard_has_all_its_panels():
    body = dashboard_body(synth(ON))
    for title in DASHBOARD_TITLES:
        assert title in body, title


def test_the_capped_baseline_also_gets_the_common_dashboard():
    assert "People waiting now (running total)" in dashboard_body(synth({"orders_rate_limit": "40"}))


def test_three_widgets_of_yours_are_accepted():
    body = dashboard_body(synth(ON, widgets=3))
    assert "Your widgets" in body and "w2" in body


def test_more_than_three_widgets_are_refused():
    result = run_synth(ON, widgets=4)
    assert result.returncode != 0
    assert "at most 3 widgets" in result.stderr


def test_any_other_flag_value_is_refused():
    result = run_synth({"waiting_room": "a"})
    assert result.returncode != 0 and "must be 'on'" in result.stderr


def test_the_orders_cap_is_intact():
    """The cap is the exam's rule: POST /orders is throttled at 40 per second with a burst of 100. If this fails,
    something changed it. cost_guard.py and the instructor check the synthesized template too."""
    stages = resources(synth(ON), "AWS::ApiGateway::Stage")
    assert len(stages) == 1
    settings = next(iter(stages.values()))["Properties"]["MethodSettings"]
    orders = [m for m in settings if "orders" in str(m.get("ResourcePath", ""))]
    assert len(orders) == 1
    assert (orders[0]["HttpMethod"], orders[0]["ThrottlingRateLimit"], orders[0]["ThrottlingBurstLimit"]) == ("POST", 40, 100)


def test_the_concurrency_panel_shows_the_account_total():
    assert "account total" in dashboard_body(synth(ON))
