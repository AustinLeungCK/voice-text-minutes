import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
JOBS_TABLE = os.environ["JOBS_TABLE"]


def lambda_handler(event, context):
    job_id = event.get("pathParameters", {}).get("job_id")
    if not job_id:
        return _response(400, {"error": "job_id is required"})

    table = dynamodb.Table(JOBS_TABLE)
    result = table.get_item(Key={"job_id": job_id})

    item = result.get("Item")
    if not item:
        return _response(404, {"error": "Job not found"})

    return _response(
        200,
        {
            "job_id": item["job_id"],
            "status": item["status"],
            "created_at": item.get("created_at"),
            "completed_at": item.get("completed_at"),
            "error_message": item.get("error_message"),
        },
    )


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
