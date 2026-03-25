import json
import os

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
JOBS_TABLE = os.environ["JOBS_TABLE"]


def lambda_handler(event, context):
    # Email 從 Cognito token claims 攞（server-side enforce）
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims", {})
    email = claims.get("email")

    if not email:
        return _response(401, {"error": "unauthorized"})

    table = dynamodb.Table(JOBS_TABLE)

    result = table.query(
        IndexName="email-created_at-index",
        KeyConditionExpression=Key("email").eq(email),
        ScanIndexForward=False,  # 最新排前面
        Limit=50,
    )

    jobs = []
    for item in result.get("Items", []):
        jobs.append({
            "job_id": item.get("job_id"),
            "email": item.get("email"),
            "status": item.get("status"),
            "created_at": item.get("created_at"),
            "file_name": item.get("file_name", ""),
            "requirements": item.get("requirements", {}),
        })

    return _response(200, {"jobs": jobs})


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "https://minutes.msphk.info",
        },
        "body": json.dumps(body),
    }
