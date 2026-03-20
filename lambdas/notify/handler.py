import json
import os

import boto3

s3_client = boto3.client("s3")
ses_client = boto3.client("ses", region_name=os.environ.get("SES_REGION", "ap-southeast-1"))
dynamodb = boto3.resource("dynamodb")

JOBS_TABLE = os.environ["JOBS_TABLE"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
SES_FROM_EMAIL = os.environ["SES_FROM_EMAIL"]


def lambda_handler(event, context):
    job_id = event["job_id"]
    email = event["email"]
    status = event.get("status", "completed")
    error = event.get("error")

    if status == "completed":
        _send_success_email(job_id, email)
    else:
        _send_failure_email(job_id, email, error)

    # Update DynamoDB
    table = dynamodb.Table(JOBS_TABLE)
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": status},
    )

    return {"status": "notified", "email": email}


def _send_success_email(job_id, email):
    # Read meeting minutes
    key = f"jobs/{job_id}/meeting_minutes.md"
    resp = s3_client.get_object(Bucket=DATA_BUCKET, Key=key)
    minutes_content = resp["Body"].read().decode("utf-8")

    # Generate presigned download URL (7 days)
    download_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": DATA_BUCKET, "Key": key},
        ExpiresIn=604800,
    )

    short_id = job_id[:8]

    ses_client.send_email(
        Source=SES_FROM_EMAIL,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {
                "Data": f"你嘅會議紀錄已準備好 — {short_id}",
                "Charset": "UTF-8",
            },
            "Body": {
                "Html": {
                    "Data": _build_success_html(minutes_content, download_url, short_id),
                    "Charset": "UTF-8",
                },
                "Text": {
                    "Data": f"會議紀錄 ({short_id}):\n\n{minutes_content}\n\n"
                    f"下載連結（7日有效）：{download_url}",
                    "Charset": "UTF-8",
                },
            },
        },
    )


def _send_failure_email(job_id, email, error):
    short_id = job_id[:8]
    error_msg = json.dumps(error, ensure_ascii=False) if error else "Unknown error"

    ses_client.send_email(
        Source=SES_FROM_EMAIL,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {
                "Data": f"會議紀錄處理失敗 — {short_id}",
                "Charset": "UTF-8",
            },
            "Body": {
                "Text": {
                    "Data": f"Job {short_id} 處理失敗。\n\n"
                    f"錯誤信息：{error_msg}\n\n"
                    "請嘗試重新上傳錄影。",
                    "Charset": "UTF-8",
                },
            },
        },
    )


def _build_success_html(minutes, download_url, short_id):
    # Convert markdown-ish content to basic HTML
    html_content = minutes.replace("\n", "<br>")

    return f"""
    <html>
    <body style="font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
        <h2>會議紀錄已準備好 — {short_id}</h2>
        <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
            {html_content}
        </div>
        <p>
            <a href="{download_url}"
               style="background: #0066cc; color: white; padding: 10px 20px;
                      text-decoration: none; border-radius: 4px;">
                下載 Markdown 檔案
            </a>
            <br><small>（連結 7 日內有效）</small>
        </p>
    </body>
    </html>
    """
