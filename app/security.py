import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional


def encode_token(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def decode_token(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_password(candidate: str, configured_password: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), configured_password.encode("utf-8"))


def create_session(secret: bytes, lifetime_seconds: int = 60 * 60 * 24 * 30) -> str:
    issued_at = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    payload = f"{issued_at}.{nonce}"
    signature = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{encode_token(payload)}.{signature}"


def validate_session(token: str, secret: bytes, lifetime_seconds: int = 60 * 60 * 24 * 30) -> bool:
    try:
        encoded_payload, signature = token.split(".", 1)
        payload = decode_token(encoded_payload)
        issued_text, nonce = payload.split(".", 1)
        if not nonce:
            return False
        expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        age = time.time() - int(issued_text)
        return 0 <= age <= lifetime_seconds
    except (ValueError, TypeError, UnicodeError):
        return False


def secure_token_matches(left: Optional[str], right: Optional[str]) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)
