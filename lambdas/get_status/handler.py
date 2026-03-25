import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

JOBS_TABLE = os.environ["JOBS_TABLE"]
DATA_BUCKET = os.environ["DATA_BUCKET"]


def lambda_handler(event, context):
    job_id = (event.get("pathParameters") or {}).get("job_id")
    if not job_id:
        return _response(400, {"error": "job_id is required"})

    table = dynamodb.Table(JOBS_TABLE)
    result = table.get_item(Key={"job_id": job_id})

    item = result.get("Item")
    if not item:
        return _response(404, {"error": "Job not found"})

    # IDOR protection: verify the Cognito caller owns this job
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims", {})
    )
    caller_email = claims.get("email")
    if not caller_email or caller_email != item.get("email"):
        return _response(404, {"error": "Job not found"})

    response_body = {
        "job_id": item["job_id"],
        "status": item["status"],
        "created_at": item.get("created_at"),
        "completed_at": item.get("completed_at"),
        "error_message": item.get("error_message"),
    }

    # Include meeting minutes content for completed/refined jobs
    if item.get("status") in ("completed", "refined"):
        try:
            resp = s3_client.get_object(
                Bucket=DATA_BUCKET,
                Key=f"jobs/{job_id}/meeting_minutes.md",
            )
            response_body["minutes"] = resp["Body"].read().decode("utf-8")
        except s3_client.exceptions.NoSuchKey:
            response_body["minutes"] = ""

    return _response(200, response_body)


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "https://minutes.msphk.info",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }
