import hmac
import os
from functools import wraps

from flask import jsonify, request


def require_service_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        token_prefix = "Bearer "

        if not authorization.startswith(token_prefix):
            return unauthorized()

        token = authorization[len(token_prefix) :]
        api_key = os.environ.get("API_KEY")
        if not api_key or not hmac.compare_digest(token, api_key):
            return unauthorized()

        return view(*args, **kwargs)

    return wrapped


def unauthorized():
    return (
        jsonify(
            {
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Missing or invalid service bearer key.",
                }
            }
        ),
        401,
    )
