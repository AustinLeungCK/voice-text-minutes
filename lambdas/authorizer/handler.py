"""
Lambda authorizer for API Gateway — validates JWT from HK Cognito.
Cognito User Pool stays in ap-east-1, API Gateway moves to ap-northeast-2.
JWKS is fetched once and cached in Lambda memory (~5ms first call, ~0ms after).
"""
import json
import os
import time
import urllib.request

from jose import jwt

COGNITO_REGION = "ap-east-1"
USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]     # ap-east-1_6BilvzaAu
CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]             # 1q6id8nmmri2olooak025gscd5
ISSUER = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

# Cache JWKS in Lambda memory (persists across warm invocations)
_jwks_cache = {"keys": None, "fetched_at": 0}
JWKS_TTL = 3600  # refresh every 1 hour


def _get_jwks():
    now = time.time()
    if _jwks_cache["keys"] and (now - _jwks_cache["fetched_at"]) < JWKS_TTL:
        return _jwks_cache["keys"]
    with urllib.request.urlopen(JWKS_URL) as resp:
        _jwks_cache["keys"] = json.loads(resp.read())["keys"]
        _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]


def lambda_handler(event, context):
    token = (event.get("authorizationToken") or "").replace("Bearer ", "")
    method_arn = event["methodArn"]

    if not token:
        raise Exception("Unauthorized")

    try:
        # Decode header to find key ID
        header = jwt.get_unverified_header(token)
        kid = header["kid"]

        # Match key from JWKS
        keys = _get_jwks()
        key = next((k for k in keys if k["kid"] == kid), None)
        if not key:
            raise Exception("Key not found")

        # Verify JWT
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=ISSUER,
        )

        # Extract email for downstream Lambdas
        email = claims.get("email", "")
        sub = claims.get("sub", "")

        # Build allow policy
        arn_parts = method_arn.split(":")
        region = arn_parts[3]
        account = arn_parts[4]
        api_gw = arn_parts[5].split("/")
        api_id = api_gw[0]
        stage = api_gw[1]

        return {
            "principalId": sub,
            "policyDocument": {
                "Version": "2012-10-17",
                "Statement": [{
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": f"arn:aws:execute-api:{region}:{account}:{api_id}/{stage}/*",
                }],
            },
            "context": {
                "email": email,
                "sub": sub,
            },
        }
    except Exception as e:
        print(f"Auth failed: {e}")
        raise Exception("Unauthorized")
