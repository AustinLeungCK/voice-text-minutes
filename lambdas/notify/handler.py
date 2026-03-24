import json
import os

import boto3

s3_client = boto3.client("s3")
ses_client = boto3.client("sesv2", region_name=os.environ.get("SES_REGION", "ap-southeast-1"))
dynamodb = boto3.resource("dynamodb")

JOBS_TABLE = os.environ["JOBS_TABLE"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
SES_FROM_EMAIL = os.environ["SES_FROM_EMAIL"]
SES_IDENTITY_ARN = os.environ.get("SES_IDENTITY_ARN", "")


def lambda_handler(event, context):
    job_id = event["job_id"]
    email = event["email"]
    status = event.get("status", "completed")
    error = event.get("error")

    # 從 DynamoDB 攞 file_name，用喺 email subject/body
    table = dynamodb.Table(JOBS_TABLE)
    job_record = table.get_item(Key={"job_id": job_id}).get("Item", {})
    file_name = job_record.get("file_name", "recording")

    if status == "completed":
        _send_success_email(job_id, email, file_name)
    else:
        _send_failure_email(job_id, email, error, file_name)

    # Update DynamoDB
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": status},
    )

    return {"status": "notified", "email": email}


def _send_success_email(job_id, email, file_name):
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

    send_params = {
        "FromEmailAddress": SES_FROM_EMAIL,
        "Destination": {"ToAddresses": [email]},
        "Content": {
            "Simple": {
                "Subject": {"Data": f"你嘅會議紀錄已準備好 — {file_name}", "Charset": "UTF-8"},
                "Body": {
                    "Html": {
                        "Data": _build_success_html(minutes_content, download_url, file_name),
                        "Charset": "UTF-8",
                    },
                    "Text": {
                        "Data": f"會議紀錄 ({file_name}):\n\n{minutes_content}\n\n"
                        f"下載連結（7日有效）：{download_url}",
                        "Charset": "UTF-8",
                    },
                },
            },
        },
    }
    if SES_IDENTITY_ARN:
        send_params["FromEmailAddressIdentityArn"] = SES_IDENTITY_ARN
    ses_client.send_email(**send_params)


def _extract_error_reason(error):
    """從 Step Functions error object 提取人睇得明嘅 error message"""
    if not error:
        return "未知錯誤"
    try:
        # Step Functions Catch 格式: {"Error": "...", "Cause": "..."}
        cause = error.get("Cause", "")
        if isinstance(cause, str):
            try:
                cause_obj = json.loads(cause)
            except (json.JSONDecodeError, TypeError):
                cause_obj = {}
        else:
            cause_obj = cause

        # Batch job: 從 Container.Reason 提取
        reason = ""
        container = cause_obj.get("Container", {})
        if container.get("Reason"):
            reason = container["Reason"]
        # 或者從 StatusReason
        elif cause_obj.get("StatusReason"):
            reason = cause_obj["StatusReason"]
        # Lambda error
        elif cause_obj.get("errorMessage"):
            reason = cause_obj["errorMessage"]

        if reason:
            # 截短太長嘅 error（例如 IAM policy denied 會好長）
            if len(reason) > 300:
                reason = reason[:300] + "..."
            return reason

        # Fallback: 用 Error type
        error_type = error.get("Error", "")
        if error_type:
            return f"錯誤類型：{error_type}"

        return "未知錯誤"
    except Exception:
        return "處理錯誤信息時發生異常"


def _send_failure_email(job_id, email, error, file_name):
    error_msg = _extract_error_reason(error)

    send_params = {
        "FromEmailAddress": SES_FROM_EMAIL,
        "Destination": {"ToAddresses": [email]},
        "Content": {
            "Simple": {
                "Subject": {"Data": f"會議紀錄處理失敗 — {file_name}", "Charset": "UTF-8"},
                "Body": {
                    "Text": {
                        "Data": f"「{file_name}」處理失敗。\n\n"
                        f"原因：{error_msg}\n\n"
                        "請嘗試重新上傳錄影。如果問題持續，請聯絡管理員。",
                        "Charset": "UTF-8",
                    },
                },
            },
        },
    }
    if SES_IDENTITY_ARN:
        send_params["FromEmailAddressIdentityArn"] = SES_IDENTITY_ARN
    ses_client.send_email(**send_params)


def _build_success_html(minutes, download_url, file_name):
    # Convert markdown-ish content to basic HTML
    html_content = minutes.replace("\n", "<br>")

    return f"""
    <html>
    <body style="font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
        <h2>會議紀錄已準備好 — {file_name}</h2>
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
