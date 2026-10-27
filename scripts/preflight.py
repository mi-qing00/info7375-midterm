#!/usr/bin/env python3
"""
preflight.py - check your AWS account before you build anything.   (PROVIDED: do not modify)

    python3 scripts/preflight.py             # credentials, region and the Lambda concurrency quota
    python3 scripts/preflight.py --min 100

WHY IT EXISTS
  Every join, status check, order and consumer runs in a Lambda function, and ALL of them share ONE account-wide
  concurrency quota. AWS lets a new account start with a very low quota (as low as 10; the default for an established
  account is 1,000). Requests beyond the quota are refused, API Gateway returns them as HTTP 500, and ONE 5xx fails the
  scorecard. That happens to EVERY design, the baseline included: it is not a bug in your code.

WHAT IT REPORTS (three levels, because only two points are known)
  PASS  1,000 or more: the level every reference run used.
  WARN  between 100 and 999: UNTESTED. 10 is known to fail every design and 1,000 is known to work; nobody has measured
        in between. It is probably fine. If your stampede shows 5xx errors and CloudWatch shows Lambda throttles, this is why.
  FAIL  below 100 (the floor is a guess, not a measurement): expect refused requests in the stampede.
  A WARN or FAIL does not stop you building, testing or rehearsing. It does mean you should request an increase now:
  Service Quotas console (region us-east-1) -> AWS Lambda -> "Concurrent executions" -> Request increase. Tell the
  instructor, and keep this output and a dashboard screenshot from your stampede (the throttles and the account total)
  for your hand-in: a run that fails ONLY because of the quota is not a design failure.

Exit code: 0 for PASS or WARN, 1 for FAIL, 2 if the account could not be checked.
"""
import argparse
import sys

DEFAULT_MIN = 100       # a guess: 10 is known to fail every design; nothing between 10 and 1,000 was measured
TESTED_LEVEL = 1000     # the quota every reference run used (the AWS default for an established account)


def read_quota(lambda_client):
    """Returns (account limit, unreserved limit): the unreserved number is what functions without reserved concurrency share."""
    limit = lambda_client.get_account_settings()["AccountLimit"]
    return int(limit["ConcurrentExecutions"]), int(limit["UnreservedConcurrentExecutions"])


def evaluate(total, unreserved, minimum):
    """Returns (ok, lines). ok means "you may go on": PASS and WARN are both ok, only FAIL is not."""
    usable = min(total, unreserved)
    lines = [f"Lambda concurrency quota:  {total:,}   (unreserved, shared by your functions: {unreserved:,})"]
    if usable < minimum:
        lines += [
            f"FAIL - {usable:,} is below {minimum:,}. (10 is known to fail every design; the {minimum:,} floor is a guess.)",
            "       Expect requests to be refused during the stampede: API Gateway returns them as HTTP 500, and the",
            "       scorecard fails for EVERY design, the baseline included. It is not a bug in your code.",
            "       Request an increase now: Service Quotas -> AWS Lambda -> Concurrent executions (region us-east-1),",
            "       and tell the instructor. Approvals are not instant. You can still build, test and rehearse.",
            "       Keep this output and a dashboard screenshot from your stampede for your hand-in.",
        ]
        return False, lines
    if usable < TESTED_LEVEL:
        lines += [
            f"WARN - {usable:,} is above the {minimum:,} floor but below the {TESTED_LEVEL:,} every reference run used.",
            "       Nobody has tested the stampede in between: 10 fails every design, 1,000 works. It is probably fine.",
            "       If your stampede shows 5xx errors and CloudWatch shows Lambda throttles, this is why. Request an",
            "       increase if you can (Service Quotas -> AWS Lambda -> Concurrent executions, region us-east-1) and tell",
            "       the instructor. Keep this output and a dashboard screenshot from your stampede for your hand-in.",
        ]
        return True, lines
    lines.append(f"PASS - {usable:,} is at least the {TESTED_LEVEL:,} every reference run used")
    return True, lines


def main(argv=None, lambda_client=None, sts_client=None):
    ap = argparse.ArgumentParser(description="Check the AWS account before building.")
    ap.add_argument("--min", type=int, default=DEFAULT_MIN, help=f"smallest acceptable quota (default {DEFAULT_MIN})")
    args = ap.parse_args(argv)
    try:
        if lambda_client is None or sts_client is None:
            import boto3
            session = boto3.session.Session()
            lambda_client = lambda_client or session.client("lambda")
            sts_client = sts_client or session.client("sts")
            region = session.region_name or "(no region set: export AWS_DEFAULT_REGION=us-east-1)"
        else:
            region = "(injected)"
        account = sts_client.get_caller_identity()["Account"]
        total, unreserved = read_quota(lambda_client)
    except Exception as e:      # no credentials, no network, no permission: say what happened, do not guess
        print(f"Could not check the account: {type(e).__name__}: {e}")
        print("Check your AWS credentials and region (export AWS_DEFAULT_REGION=us-east-1), then run this again.")
        return 2
    print(f"AWS account {account}, region {region}")
    ok, lines = evaluate(total, unreserved, args.min)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
