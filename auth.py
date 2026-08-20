"""
auth.py
-------
Logs in against the FastAPI backend's username/password endpoint and
keeps config.HEADERS populated with a fresh Bearer token.

Replaces the old static-token / HMAC-signing scheme. The backend now
issues short-lived JWTs via POST /auth/login (config.LOGIN_URL), so
this module logs in once at boot and re-logs-in transparently
whenever a request comes back 401 (DB wipe -> user recreated, or the
token simply expired).
"""

import urequests

import config

# Characters that don't need percent-encoding in a form body.
_UNRESERVED = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
               "abcdefghijklmnopqrstuvwxyz"
               "0123456789-_.~")


def _url_escape(s):
    out = ""
    for ch in s:
        if ch in _UNRESERVED:
            out += ch
        elif ch == " ":
            out += "+"
        else:
            out += "%%%02X" % ord(ch)
    return out


def _form_encode(fields):
    return "&".join(
        _url_escape(k) + "=" + _url_escape(str(v)) for k, v in fields.items()
    )


def login():
    """
    Logs in with config.AUTH_USERNAME / config.AUTH_PASSWORD and
    stores the returned access token in config.HEADERS so every
    subsequent request is authenticated. Returns True on success.

    FastAPI's standard /auth/login (OAuth2PasswordRequestForm) expects
    a form-urlencoded body with 'username'/'password' fields, NOT
    JSON - sending JSON there gets rejected with a 422 before
    credentials are even checked, which is what a JSON body would
    produce here.
    """
    body = _form_encode({
        "username": config.AUTH_USERNAME,
        "password": config.AUTH_PASSWORD,
    })

    try:
        resp = urequests.post(
            config.LOGIN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = resp.status_code
        data = resp.json() if status == 200 else None
        resp.close()

        if status != 200 or not data or "access_token" not in data:
            print("[auth] login failed, status", status)
            config.HEADERS.pop("Authorization", None)
            return False

        config.HEADERS["Authorization"] = "Bearer " + data["access_token"]
        print("[auth] login OK")
        return True

    except Exception as e:
        print("[auth] login error:", e)
        config.HEADERS.pop("Authorization", None)
        return False


def build_headers(extra=None):
    """Current auth headers (Content-Type + Authorization once logged in)."""
    headers = dict(config.HEADERS)
    if extra:
        headers.update(extra)
    return headers


def is_authenticated():
    return "Authorization" in config.HEADERS
