import json
import os

import boto3

sfn_client = boto3.client("stepfunctions")
dynamodb = boto3.resource("dynamodb")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
JOBS_TABLE = os.environ["JOBS_TABLE"]


def lambda_handler(event, context):
    # EventBridge S3 event format
    detail = event.get("detail", {})
    s3_key = detail.get("object", {}).get("key", "")

    # Extract job_id from key: jobs/{job_id}/input.mp4
    parts = s3_key.split("/")
    if len(parts) < 3 or parts[0] != "jobs" or not s3_key.endswith(".mp4"):
        print(f"Skipping non-matching key: {s3_key}")
        return {"statusCode": 200}

    job_id = parts[1]

    # Validate job exists in DynamoDB and is in 'uploaded' status
    table = dynamodb.Table(JOBS_TABLE)
    resp = table.get_item(Key={"job_id": job_id})
    item = resp.get("Item")

    if not item:
        print(f"WARNING: No DynamoDB record for job_id={job_id}. Skipping pipeline start.")
        return {"statusCode": 200}

    job_status = item.get("status")
    if job_status != "uploaded":
        print(
            f"WARNING: job_id={job_id} has status '{job_status}', expected 'uploaded'. "
            f"Skipping pipeline start."
        )
        return {"statusCode": 200}

    try:
        sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=f"job-{job_id}",
            input=json.dumps({
                "job_id": job_id,
                "s3_bucket": detail.get("bucket", {}).get("name", ""),
            }),
        )
    except sfn_client.exceptions.ExecutionAlreadyExists:
        print(
            f"WARNING: Execution already exists for job_id={job_id}. "
            f"Pipeline is already running, skipping duplicate start."
        )
        return {"statusCode": 200}

    print(f"Started pipeline for job_id={job_id}")
    return {"statusCode": 200}
