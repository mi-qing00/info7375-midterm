#!/usr/bin/env python3
"""
Seed inventory for the waiting-room stampede exercise, and write the
products.csv payload file the Artillery load tests read.

WHY A SEPARATE SEED SCRIPT?
  seed_inventory.py creates 50 products with SMALL stock (5-300 units).
  A 1,500-order stampede would exhaust most of them, and every stock-out
  publishes a StockOut event that fans out to the analytics queue. That
  would bury the thing this exercise measures - how many orders reach the
  backend, and how fast - under unrelated stock-out noise.

  So this script creates a few products with enormous stock. Every order
  succeeds, and the only variable left is the waiting room.

Usage:
    export AWS_DEFAULT_REGION=us-east-1
    python3 scripts/seed_stampede_inventory.py
    # -> writes load-test/products.csv

Safe to run more than once: it replaces its own products (named
"Stampede Ticket ...") rather than piling up duplicates, and leaves any
other products in the table alone.
"""
import csv
import os
import sys
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

STACK_TABLE = "week5-inventory"        # the name the stack creates
INVENTORY_TABLE = os.getenv("INVENTORY_TABLE", "week5-inventory")
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "load-test" / "products.csv"

STOCK_PER_PRODUCT = 1_000_000          # far more than any test can consume
PRODUCT_NAMES = [
    "Stampede Ticket - Floor",
    "Stampede Ticket - Lower Bowl",
    "Stampede Ticket - Upper Bowl",
    "Stampede Ticket - Balcony",
    "Stampede Ticket - VIP",
]


def main(table_name: str = INVENTORY_TABLE, csv_path: Path = DEFAULT_CSV, dynamodb=None) -> list:
    dynamodb = dynamodb or boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)
    region = dynamodb.meta.client.meta.region_name
    print(f"Seeding table {table_name!r} in region {region}.")
    override = os.getenv("INVENTORY_TABLE")
    if override and override != STACK_TABLE:
        print(f"NOTE: INVENTORY_TABLE is set in your shell to {override!r}. If that is left over from "
              "another week (Week 6 used its own table), run `unset INVENTORY_TABLE` and try again.")

    # Remove products from a previous run of THIS script (only ours).
    removed = 0
    try:
        scan = table.scan(ProjectionExpression="product_id, #n",
                          ExpressionAttributeNames={"#n": "name"})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        print(f"ERROR: table {table_name!r} does not exist in region {region}. Check, in this order:\n"
              "  1. the stack is deployed and finished: aws cloudformation describe-stacks --stack-name Week5EventDrivenStack\n"
              "  2. your region: export AWS_DEFAULT_REGION=us-east-1\n"
              "  3. a leftover INVENTORY_TABLE variable from another week: echo $INVENTORY_TABLE", file=sys.stderr)
        return []
    with table.batch_writer() as batch:
        for item in scan["Items"]:
            if str(item.get("name", "")).startswith("Stampede Ticket"):
                batch.delete_item(Key={"product_id": item["product_id"]})
                removed += 1
    if removed:
        print(f"Removed {removed} products from a previous run.")

    print(f"Seeding {len(PRODUCT_NAMES)} products into {table_name} "
          f"(stock {STOCK_PER_PRODUCT:,} each)...")
    product_ids = []
    with table.batch_writer() as batch:
        for name in PRODUCT_NAMES:
            product_id = str(uuid.uuid4())
            batch.put_item(Item={
                "product_id": product_id,
                "name": name,
                "category": "stampede",
                "price": "99.00",                  # string, matching seed_inventory.py
                "stock_count": STOCK_PER_PRODUCT,
                "updated_at": "2026-01-01T00:00:00Z",
            })
            product_ids.append(product_id)
            print(f"  ok {name:<32} {product_id}")

    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["product_id"])
        for product_id in product_ids:
            writer.writerow([product_id])
    print(f"\nWrote {csv_path} ({len(product_ids)} rows) - the Artillery payload file.")
    return product_ids


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
