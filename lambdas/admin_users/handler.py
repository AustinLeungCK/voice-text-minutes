import json
import os

import boto3

ADMIN_EMAIL = "austin.leung@ecloudvalley.com"
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
SES_REGION = os.environ.get("SES_REGION", "ap-southeast-1")

cognito = boto3.client("cognito-idp", region_name=os.environ.get("COGNITO_REGION", "ap-east-1"))
ses = boto3.client("ses", region_name=SES_REGION)


def lambda_handler(event, context):
    # --- Admin check ---
    authorizer = (event.get("requestContext") or {}).get("authorizer", {})
    caller_email = authorizer.get("email")
    if caller_email != ADMIN_EMAIL:
        return _response(403, {"error": "Admin access required"})

    method = event.get("httpMethod", "")
    if method == "POST":
        return _create_user(event)
    elif method == "GET":
        return _list_users()
    elif method == "DELETE":
        return _delete_user(event, caller_email)
    else:
        return _response(405, {"error": f"Method {method} not allowed"})


def _create_user(event):
    body = json.loads(event.get("body", "{}"))
    email = (body.get("email") or "").strip().lower()
    password = body.get("password", "")

    if not email:
        return _response(400, {"error": "email is required"})
    if not password or len(password) < 8:
        return _response(400, {"error": "password must be at least 8 characters"})

    try:
        # Step 1: Create user with email_verified=true
        cognito.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",  # Don't send Cognito welcome email
        )

        # Step 2: Set permanent password (skip FORCE_CHANGE_PASSWORD)
        cognito.admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            Password=password,
            Permanent=True,
        )

        # Step 3: Verify email in SES so they can receive notification emails
        ses.verify_email_identity(EmailAddress=email)

        return _response(200, {"email": email, "status": "created"})

    except cognito.exceptions.UsernameExistsException:
        return _response(409, {"error": "User already exists"})
    except cognito.exceptions.InvalidPasswordException as e:
        return _response(400, {"error": str(e)})
    except Exception as e:
        return _response(500, {"error": str(e)})


def _list_users():
    try:
        users = []
        params = {
            "UserPoolId": COGNITO_USER_POOL_ID,
            "Limit": 60,
        }

        while True:
            result = cognito.list_users(**params)
            for user in result.get("Users", []):
                email = ""
                for attr in user.get("Attributes", []):
                    if attr["Name"] == "email":
                        email = attr["Value"]
                        break
                users.append({
                    "email": email,
                    "status": user.get("UserStatus", ""),
                    "created": user.get("UserCreateDate", "").isoformat()
                    if hasattr(user.get("UserCreateDate", ""), "isoformat")
                    else str(user.get("UserCreateDate", "")),
                    "enabled": user.get("Enabled", True),
                })
            # Paginate if needed
            pagination_token = result.get("PaginationToken")
            if pagination_token:
                params["PaginationToken"] = pagination_token
            else:
                break

        return _response(200, users)

    except Exception as e:
        return _response(500, {"error": str(e)})


def _delete_user(event, caller_email):
    body = json.loads(event.get("body", "{}"))
    email = (body.get("email") or "").strip().lower()

    if not email:
        return _response(400, {"error": "email is required"})

    # Prevent self-deletion
    if email == caller_email:
        return _response(400, {"error": "Cannot delete yourself"})

    try:
        cognito.admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
        )
        return _response(200, {"email": email, "status": "deleted"})

    except cognito.exceptions.UserNotFoundException:
        return _response(404, {"error": "User not found"})
    except Exception as e:
        return _response(500, {"error": str(e)})


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }
