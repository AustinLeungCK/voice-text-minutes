import json
import os
import time

import boto3

dynamodb = boto3.resource("dynamodb")
sfn_client = boto3.client("stepfunctions")

JOBS_TABLE = os.environ["JOBS_TABLE"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def lambda_handler(event, context):
    # --- Extract job_id from path parameters ---
    job_id = (event.get("pathParameters") or {}).get("job_id")
    if not job_id:
        return _response(400, {"error": "job_id is required"})

    # --- Extract caller email from Cognito claims ---
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims", {})
    )
    caller_email = claims.get("email")
    if not caller_email:
        return _response(401, {"error": "unauthorized"})

    # --- Look up job and verify ownership (IDOR protection) ---
    table = dynamodb.Table(JOBS_TABLE)
    result = table.get_item(Key={"job_id": job_id})
    item = result.get("Item")
    if not item or item.get("email") != caller_email:
        return _response(404, {"error": "Job not found"})

    # --- Verify job is in failed status ---
    if item.get("status") != "failed":
        return _response(400, {"error": "Only failed jobs can be retried"})

    # --- Reset status to processing and remove error_message ---
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s REMOVE error_message",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "processing"},
    )

    # --- Start a new Step Functions execution ---
    timestamp = int(time.time())
    execution_name = f"job-{job_id}-retry-{timestamp}"

    try:
        sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=json.dumps({
                "job_id": job_id,
                "s3_bucket": DATA_BUCKET,
                "s3_key": f"jobs/{job_id}/input.mp4",
            }),
        )
    except Exception as e:
        print(f"Step Functions error for job {job_id}: {e}")
        # Roll back status so the user can try again
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "failed"},
        )
        return _response(500, {"error": "Failed to start retry pipeline"})

    print(f"Started retry pipeline for job_id={job_id}, execution={execution_name}")
    return _response(200, {"status": "retrying"})


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "https://minutes.msphk.info",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }
