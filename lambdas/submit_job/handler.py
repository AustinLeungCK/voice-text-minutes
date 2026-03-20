import json
import os
import uuid
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

JOBS_TABLE = os.environ["JOBS_TABLE"]
DATA_BUCKET = os.environ["DATA_BUCKET"]


def lambda_handler(event, context):
    body = json.loads(event.get("body", "{}"))

    email = body.get("email")
    if not email:
        return _response(400, {"error": "email is required"})

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    requirements = {
        "output_language": body.get("output_language", "繁體中文"),
        "summary_length": body.get("summary_length", "medium"),
        "output_format": body.get("output_format", "minutes"),
        "custom_instructions": body.get("custom_instructions", ""),
    }

    table = dynamodb.Table(JOBS_TABLE)
    table.put_item(
        Item={
            "job_id": job_id,
            "email": email,
            "requirements": requirements,
            "status": "uploaded",
            "created_at": now,
        }
    )

    s3_key = f"jobs/{job_id}/input.mp4"
    presigned_url = s3_client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": DATA_BUCKET,
            "Key": s3_key,
            "ContentType": "video/mp4",
        },
        ExpiresIn=3600,
    )

    return _response(
        200,
        {
            "job_id": job_id,
            "upload_url": presigned_url,
            "message": "Upload your recording using the presigned URL. "
            "You will receive an email when processing is complete.",
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
