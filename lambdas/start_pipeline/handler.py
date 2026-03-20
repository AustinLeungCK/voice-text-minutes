import json
import os

import boto3

sfn_client = boto3.client("stepfunctions")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


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

    sfn_client.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=f"job-{job_id}",
        input=json.dumps({"job_id": job_id}),
    )

    print(f"Started pipeline for job_id={job_id}")
    return {"statusCode": 200}
