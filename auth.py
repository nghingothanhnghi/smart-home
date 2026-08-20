"""
auth.py
-------
Builds authentication headers (and optionally an HMAC signature) for
every request the device makes to the FastAPI backend. Centralized so
that if the auth scheme changes (e.g. Bearer -> mTLS, or you add
per-request signing), it changes in exactly one place.
"""

import ujson
import uhashlib
import ubinascii

import config


def build_headers(extra=None):
    """Standard headers for all backend calls."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + config.DEVICE_AUTH_TOKEN,
        "X-Device-Model": config.DEVICE_MODEL,
        "X-Firmware-Version": config.FIRMWARE_VERSION,
    }
    if extra:
        headers.update(extra)
    return headers


def sign_payload(payload_dict):
    """
    Optional HMAC-style integrity signature over a JSON payload, using
    a simple SHA256(secret + body) scheme (MicroPython's uhashlib does
    not ship HMAC, so this is a lightweight equivalent). Returns None
    if no shared secret is configured (signing disabled).

    A stricter deployment can swap this for real HMAC-SHA256 via a
    'uhmac' package if the board's flash budget allows it.
    """
    if not config.DEVICE_HMAC_SECRET:
        return None

    body = ujson.dumps(payload_dict)
    h = uhashlib.sha256()
    h.update(config.DEVICE_HMAC_SECRET.encode())
    h.update(body.encode())
    digest = ubinascii.hexlify(h.digest()).decode()
    return digest


def build_signed_headers(payload_dict):
    """Headers including X-Signature when HMAC signing is enabled."""
    headers = build_headers()
    sig = sign_payload(payload_dict)
    if sig:
        headers["X-Signature"] = sig
    return headers
