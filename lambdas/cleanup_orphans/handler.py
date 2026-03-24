"""Cleanup orphan uploads — delete S3 objects and DynamoDB records for jobs
that are still in status=uploaded after 24 hours (i.e. the file was never
actually uploaded or the pipeline never started)."""

import os
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

JOBS_TABLE = os.environ["JOBS_TABLE"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
ORPHAN_THRESHOLD_HOURS = int(os.environ.get("ORPHAN_THRESHOLD_HOURS", "24"))


def lambda_handler(event, context):
    table = dynamodb.Table(JOBS_TABLE)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ORPHAN_THRESHOLD_HOURS)).isoformat()

    # Scan for items with status=uploaded and created_at older than cutoff.
    # DynamoDB Scan is acceptable here because:
    #   - This runs once daily on a small table (low thousands of items max)
    #   - There is no GSI on status, and adding one just for this cleanup
    #     would cost more than the occasional scan
    orphans = []
    scan_kwargs = {
        "FilterExpression": Attr("status").eq("uploaded") & Attr("created_at").lt(cutoff),
        "ProjectionExpression": "job_id",
    }

    while True:
        response = table.scan(**scan_kwargs)
        orphans.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    deleted_count = 0
    errors = []

    for item in orphans:
        job_id = item["job_id"]
        try:
            _delete_s3_prefix(f"jobs/{job_id}/")
            table.delete_item(Key={"job_id": job_id})
            deleted_count += 1
        except Exception as e:
            errors.append({"job_id": job_id, "error": str(e)})

    result = {
        "scanned_orphans": len(orphans),
        "deleted": deleted_count,
        "errors": errors,
    }
    print(f"Cleanup result: {result}")
    return result


def _delete_s3_prefix(prefix):
    """Delete all objects under a given S3 prefix."""
    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix)

    for page in pages:
        contents = page.get("Contents", [])
        if not contents:
            continue
        objects = [{"Key": obj["Key"]} for obj in contents]
        s3_client.delete_objects(
            Bucket=DATA_BUCKET,
            Delete={"Objects": objects, "Quiet": True},
        )
