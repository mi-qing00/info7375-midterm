#!/usr/bin/env python3
"""
cost_guard.py - scan a synthesized CloudFormation template for resources that would blow the
exam's budget. Run it BEFORE `cdk deploy`, and include its output in your submission.

    cd cdk && cdk synth > /dev/null && cd ..          # writes cdk/cdk.out/<Stack>.template.json
    python3 scripts/cost_guard.py                      # scans every template in cdk/cdk.out
    python3 scripts/cost_guard.py path/to/Stack.template.json

WHY IT EXISTS
  The traffic in this exam costs cents. The budget is lost to IDLE infrastructure that bills by
  the hour whether or not anyone is using it: a NAT Gateway (the usual culprit), a big or
  replicated Redis node, a database. A copied production VPC can cost more per day than a whole
  stampede run costs in total.

RULES
  FAIL   NAT Gateway; RDS or Aurora; an EC2 instance; a load balancer; DAX; Lambda provisioned
         concurrency; an ElastiCache node bigger than a micro, more than one node, or a Redis
         serverless cache; an estimated idle cost above $1.00 a day.
  WARN   an Interface VPC endpoint (bills hourly); stateful resources
         that outlive `cdk destroy` (RemovalPolicy RETAIN); a Kinesis on-demand stream; a Valkey
         serverless cache; a large provisioned DynamoDB table.
  INFO   resources that bill while idle, with an estimate.

THE PRICES ARE ESTIMATES from list prices (us-east-1) and can be out of date; they exist to show
the SIZE of a cost, not to predict a bill.

Exit code: 0 if nothing FAILED, 1 if something did, 2 if no template was found.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"

# Rough list prices, in dollars per day. Estimates only.
NAT_PER_DAY = 1.08
CACHE_NODE_PER_DAY = 0.41
KINESIS_SHARD_PER_DAY = 0.36
IDLE_LIMIT_PER_DAY = 1.00
ALLOWED_CACHE_NODES = {"cache.t3.micro", "cache.t4g.micro"}

# The exam's order cap. It is ENFORCED only in the student starter (the build switches ENFORCE_ORDERS_CAP on), because
# this reference repo is also used for cap experiments at other values. The check reads the FINAL synthesized template,
# so a CDK escape hatch, a raw override or an Aspect in a student's own file cannot hide a changed cap from it.
ENFORCE_ORDERS_CAP = True
REQUIRED_ORDERS_RATE = 40
REQUIRED_ORDERS_BURST = 100
ORDERS_PATH = "/~1orders"

STATEFUL = {"AWS::DynamoDB::Table", "AWS::Kinesis::Stream", "AWS::SQS::Queue",
            "AWS::ElastiCache::CacheCluster", "AWS::ElastiCache::ReplicationGroup", "AWS::RDS::DBInstance",
            "AWS::RDS::DBCluster", "AWS::ElastiCache::ServerlessCache"}
ALWAYS_FAIL = {
    "AWS::RDS::DBInstance": "a relational database is not part of this exam and bills by the hour",
    "AWS::RDS::DBCluster": "a relational database is not part of this exam and bills by the hour",
    "AWS::EC2::Instance": "an EC2 instance bills by the hour and is not needed here",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "a load balancer bills by the hour; API Gateway is the front door here",
    "AWS::ElasticLoadBalancing::LoadBalancer": "a load balancer bills by the hour; API Gateway is the front door here",
    "AWS::DAX::Cluster": "DAX bills by the hour and is not needed here",
}


def _literal(value):
    """CloudFormation values can be intrinsic functions ({'Ref': ...}); only literals are judged."""
    return value if not isinstance(value, (dict, list)) else None


def check_orders_cap(template):
    """Judge the POST /orders throttle in every API Gateway stage of the template. Returns findings."""
    findings = []
    for rid, res in template.get("Resources", {}).items():
        if res.get("Type") != "AWS::ApiGateway::Stage":
            continue
        settings = [m for m in (res.get("Properties", {}).get("MethodSettings") or []) if isinstance(m, dict)]
        # anything that mentions the orders path counts, in whatever spelling, and must be exactly one entry
        orders = [m for m in settings if "orders" in str(m.get("ResourcePath", ""))]
        exact = [m for m in orders
                 if m.get("ResourcePath") == ORDERS_PATH and m.get("HttpMethod") == "POST"
                 and _literal(m.get("ThrottlingRateLimit")) == REQUIRED_ORDERS_RATE
                 and _literal(m.get("ThrottlingBurstLimit")) == REQUIRED_ORDERS_BURST]
        if len(orders) == 1 and len(exact) == 1:
            findings.append((INFO, rid, f"POST /orders cap: {REQUIRED_ORDERS_RATE} per second, burst {REQUIRED_ORDERS_BURST} "
                                        "(as the exam requires)"))
        elif not orders:
            findings.append((FAIL, rid, "POST /orders has no cap in this template: deploy with -c orders_rate_limit="
                                        f"{REQUIRED_ORDERS_RATE}"))
        else:
            seen = "; ".join(f"{m.get('HttpMethod', '?')} {m.get('ThrottlingRateLimit', '?')}/{m.get('ThrottlingBurstLimit', '?')}"
                             for m in orders)
            findings.append((FAIL, rid, f"POST /orders cap must be exactly {REQUIRED_ORDERS_RATE} per second with burst "
                                        f"{REQUIRED_ORDERS_BURST}, in ONE setting; this template has: {seen}"))
    return findings


def evaluate(template):
    """Returns (findings, idle_dollars_per_day). A finding is (level, resource_id, message)."""
    findings, idle = [], 0.0
    for rid, res in template.get("Resources", {}).items():
        rtype, props = res.get("Type", ""), res.get("Properties", {}) or {}

        if rtype == "AWS::EC2::NatGateway":
            idle += NAT_PER_DAY
            findings.append((FAIL, rid, f"NAT Gateway: about ${NAT_PER_DAY:.2f}/day each plus data charges. Not allowed in this exam."))
        elif rtype in ALWAYS_FAIL:
            findings.append((FAIL, rid, ALWAYS_FAIL[rtype]))

        elif rtype == "AWS::ElastiCache::CacheCluster":
            node, count = _literal(props.get("CacheNodeType")), _literal(props.get("NumCacheNodes", 1))
            if node is None:
                findings.append((WARN, rid, "ElastiCache node type cannot be read from the template (a parameter?): "
                                            "make sure it is ONE cache.t3.micro or cache.t4g.micro node"))
            elif node not in ALLOWED_CACHE_NODES or (count or 1) > 1:
                findings.append((FAIL, rid, f"ElastiCache {node} x{count}: use ONE cache.t3.micro or cache.t4g.micro node"))
            else:
                idle += CACHE_NODE_PER_DAY * (count or 1)
                findings.append((INFO, rid, f"ElastiCache {node}: about ${CACHE_NODE_PER_DAY:.2f}/day while it exists"))
        elif rtype == "AWS::ElastiCache::ReplicationGroup":
            node, count = _literal(props.get("CacheNodeType")), _literal(props.get("NumCacheClusters", 1))
            ha = props.get("AutomaticFailoverEnabled") or props.get("MultiAZEnabled")
            if node not in ALLOWED_CACHE_NODES or (count or 1) > 1 or ha:
                findings.append((FAIL, rid, f"ElastiCache replication group {node} x{count}: use ONE micro node, no failover or Multi-AZ"))
            else:
                idle += CACHE_NODE_PER_DAY
                findings.append((INFO, rid, f"ElastiCache {node}: about ${CACHE_NODE_PER_DAY:.2f}/day while it exists"))
        elif rtype == "AWS::ElastiCache::ServerlessCache":
            engine = str(_literal(props.get("Engine")) or "").lower()
            if engine == "redis":
                findings.append((FAIL, rid, "serverless Redis has a high fixed minimum charge (about $91/month by list price): use one micro node"))
            else:
                findings.append((WARN, rid, "serverless cache: has a fixed minimum charge even when idle (about $6/month for Valkey by list price)"))

        elif rtype in ("AWS::Lambda::Alias", "AWS::Lambda::Version") and (
                props.get("ProvisionedConcurrencyConfig") or props.get("ProvisionedConcurrentExecutions")):
            findings.append((FAIL, rid, "Lambda provisioned concurrency bills continuously"))

        elif rtype == "AWS::EC2::VPCEndpoint":
            if str(_literal(props.get("VpcEndpointType")) or "Gateway") == "Interface":
                findings.append((WARN, rid, "Interface VPC endpoint bills hourly per AZ"))

        elif rtype == "AWS::Kinesis::Stream":
            mode = (props.get("StreamModeDetails") or {}).get("StreamMode")
            if mode == "ON_DEMAND":
                findings.append((WARN, rid, "Kinesis on-demand stream bills per hour even when idle"))
            else:
                shards = _literal(props.get("ShardCount", 1)) or 1
                idle += KINESIS_SHARD_PER_DAY * shards
                findings.append((INFO, rid, f"Kinesis {shards} shard(s): about ${KINESIS_SHARD_PER_DAY * shards:.2f}/day while it exists"))

        elif rtype == "AWS::DynamoDB::Table":
            tp = props.get("ProvisionedThroughput") or {}
            rcu, wcu = _literal(tp.get("ReadCapacityUnits")) or 0, _literal(tp.get("WriteCapacityUnits")) or 0
            if props.get("BillingMode") != "PAY_PER_REQUEST" and max(rcu, wcu) > 25:
                findings.append((WARN, rid, f"provisioned DynamoDB capacity {rcu}/{wcu} bills while idle: prefer PAY_PER_REQUEST"))

        if rtype in STATEFUL and res.get("DeletionPolicy") in ("Retain", "Snapshot", "RetainExceptOnCreate"):
            findings.append((WARN, rid, f"{rtype.split('::')[-1]} survives `cdk destroy` ({res['DeletionPolicy']}) and may keep billing"))

    if ENFORCE_ORDERS_CAP:
        findings.extend(check_orders_cap(template))

    if idle > IDLE_LIMIT_PER_DAY:
        findings.append((FAIL, "(total)", f"estimated idle cost ${idle:.2f}/day is above the ${IDLE_LIMIT_PER_DAY:.2f}/day limit"))
    return findings, idle


def render(name, findings, idle):
    order = {FAIL: 0, WARN: 1, INFO: 2}
    lines = [f"=== {name} ===", ""]
    for level, rid, msg in sorted(findings, key=lambda f: (order[f[0]], f[1])):
        lines.append(f"{level:<5} {rid[:48]:<48}  {msg}")
    if not findings:
        lines.append("(nothing billable while idle was found)")
    bad = [f for f in findings if f[0] == FAIL]
    lines += ["", f"Estimated idle cost: about ${idle:.2f}/day (list-price estimate; exam budget is $2 in total, including test traffic).",
              "RESULT: " + (f"NOT OK TO DEPLOY - {len(bad)} problem(s)" if bad else "OK to deploy")]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan synthesized CloudFormation templates for budget-busting resources.")
    ap.add_argument("templates", nargs="*", help="template JSON files (default: cdk/cdk.out/*.template.json)")
    args = ap.parse_args(argv)
    paths = args.templates or sorted(glob.glob(str(Path(__file__).resolve().parent.parent / "cdk" / "cdk.out" / "*.template.json")))
    if not paths:
        print("No template found. Run `cd cdk && cdk synth > /dev/null` first.", file=sys.stderr)
        return 2
    failed = False
    for path in paths:
        try:
            findings, idle = evaluate(json.loads(Path(path).read_text()))
        except (OSError, ValueError) as e:
            print(f"Could not read {path}: {e}", file=sys.stderr)
            return 2
        print(render(Path(path).name, findings, idle) + "\n")
        failed = failed or any(f[0] == FAIL for f in findings)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
