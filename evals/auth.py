"""Access-code sessions for the hosted evaluation workspace."""
import hashlib
import hmac
import secrets
import time

COOKIE = "eval_session"
TTL = 12 * 60 * 60


def token(secret):
    payload = f"{int(time.time())}.{secrets.token_hex(16)}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def valid(value, secret):
    try:
        timestamp, nonce, signature = value.split(".")
        age = time.time() - int(timestamp)
        expected = hmac.new(secret.encode(), f"{timestamp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return 0 <= age <= TTL and hmac.compare_digest(signature, expected)
    except (AttributeError, TypeError, ValueError):
        return False
