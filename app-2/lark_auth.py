"""
Lark sign-in for the dashboard.

Opened inside Lark, this signs people in without a prompt: Lark's authorize
endpoint recognises the client and redirects straight back with a code. Opened
in a browser, it shows the normal Lark consent screen once. Either way the
dashboard learns who is looking at it, which is what makes "the China team
creates shipments" safe -- every row can carry who made it.

The flow:

    /dashboard            no session -> redirect to Lark's authorize page
    accounts.larksuite.com/open-apis/authen/v1/authorize
                          -> user approves (silent inside Lark)
    /auth/lark/callback   ?code=... -> exchange for a user_access_token
                          -> read the profile -> set a signed session cookie
    /dashboard            renders, and every API call knows the user

The session cookie is a signed blob, not a Lark token: Lark's own docs are
explicit that a user_access_token must never reach the client, so it is used
once on the server to read the profile and then dropped.

Setup in the Lark Developer Console, on the existing ShipBot app:
  1. Features > Add Features > Web app; set the desktop and mobile home URL
     to  <DASHBOARD_URL>/dashboard
  2. Security Settings > Redirect URL: add  <DASHBOARD_URL>/auth/lark/callback
  3. Permissions: contact:contact.base:readonly (name and open_id)
  4. Version Management > publish
Then set LARK_SSO=1 on Railway. With it unset, nothing here runs and the
shared-token link keeps working exactly as it does now.
"""

import os
import json
import time
import hmac
import base64
import hashlib
import logging
import secrets
import threading
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

APP_ID = os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
BASE_URL = os.environ.get("LARK_BASE_URL", "https://open.larksuite.com").rstrip("/")
ENABLED = os.environ.get("LARK_SSO", "").strip() in ("1", "true", "yes")

# The authorize page lives on accounts.*, not open.* -- a JP-region tenant
# still authorises here, so it is not derived from LARK_BASE_URL.
AUTHORIZE_URL = os.environ.get(
    "LARK_AUTHORIZE_URL",
    "https://accounts.larksuite.com/open-apis/authen/v1/authorize")

# Name and open_id. Anything more is not needed to stamp a row.
SCOPE = os.environ.get("LARK_SSO_SCOPE", "contact:contact.base:readonly base:record:read base:record:retrieve base:field:read docs:document.media:download")

# Tokens remain server-side, never in the signed (readable) browser cookie.
# Single-worker deployment: restart/expiry deliberately requires a new login.
_delegated = {}
_delegated_lock = threading.Lock()


def delegated_token(user):
    with _delegated_lock:
        entry = _delegated.get((user or {}).get('grant_id'))
        if entry and entry['expires'] > time.time() and entry['open_id'] == user.get('open_id'):
            return entry['token']
    return None

SESSION_COOKIE = "shipbot_session"
SESSION_TTL = int(os.environ.get("LARK_SESSION_TTL", str(14 * 24 * 3600)))


def configured():
    return bool(ENABLED and APP_ID and APP_SECRET)


# ---------------------------------------------------------------------------
# Signed session cookie
# ---------------------------------------------------------------------------

def _sign(payload):
    return hmac.new(APP_SECRET.encode(), payload, hashlib.sha256).digest()


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(user):
    """user -> tamper-proof cookie value."""
    body = dict(user)
    body["exp"] = int(time.time()) + SESSION_TTL
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return "%s.%s" % (_b64(raw), _b64(_sign(raw)))


def read_session(cookie):
    """Cookie value -> user dict, or None when missing, forged or expired."""
    if not cookie or "." not in cookie:
        return None
    body_b64, sig_b64 = cookie.rsplit(".", 1)
    try:
        raw = _unb64(body_b64)
        if not hmac.compare_digest(_sign(raw), _unb64(sig_b64)):
            return None
        user = json.loads(raw)
    except Exception:
        return None
    if user.get("exp", 0) < time.time():
        return None
    return user


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def login_url(redirect_uri, state=""):
    return "%s?%s" % (AUTHORIZE_URL, urlencode({
        "client_id": APP_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state or "shipbot",
    }))


def _app_access_token():
    resp = requests.post(
        "%s/open-apis/auth/v3/app_access_token/internal" % BASE_URL,
        json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise Exception("app_access_token failed: %s" % data)
    return data["app_access_token"]


def exchange_code(code, redirect_uri):
    """Authorization code -> user_access_token.

    v2 is the current endpoint; v1 is kept because tenants move at different
    speeds and a bare 400 here would otherwise be a dead end. Both log the
    response body -- Lark puts the actual reason there.
    """
    attempts = [
        ("v2", "%s/open-apis/authen/v2/oauth/token" % BASE_URL,
         {"grant_type": "authorization_code", "client_id": APP_ID,
          "client_secret": APP_SECRET, "code": code,
          "redirect_uri": redirect_uri}, None),
        ("v1", "%s/open-apis/authen/v1/oidc/access_token" % BASE_URL,
         {"grant_type": "authorization_code", "code": code}, "app"),
    ]
    last = ""
    for label, url, body, auth in attempts:
        try:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if auth == "app":
                headers["Authorization"] = "Bearer %s" % _app_access_token()
            resp = requests.post(url, json=body, headers=headers, timeout=20)
            data = resp.json()
            token = (data.get("access_token")
                     or data.get("data", {}).get("access_token"))
            if token:
                logger.info("Lark sign-in: exchanged code via %s", label)
                return token
            last = "%s -> %s" % (label, json.dumps(data)[:300])
            logger.warning("Lark token exchange %s failed: %s", label, last)
        except Exception as e:
            last = "%s -> %s" % (label, e)
            logger.warning("Lark token exchange %s errored: %s", label, e)
    raise Exception("Could not exchange the Lark code (%s)" % last)


def user_info(user_access_token):
    resp = requests.get(
        "%s/open-apis/authen/v1/user_info" % BASE_URL,
        headers={"Authorization": "Bearer %s" % user_access_token}, timeout=20)
    data = resp.json()
    if data.get("code") != 0:
        raise Exception("user_info failed: %s" % json.dumps(data)[:300])
    d = data.get("data", {})
    return {
        "name": d.get("name") or d.get("en_name") or "Unknown",
        "open_id": d.get("open_id", ""),
        "avatar": d.get("avatar_thumb", ""),
    }


def sign_in(code, redirect_uri):
    """Code from the callback -> the user dict to put in the session."""
    token = exchange_code(code, redirect_uri)
    user = user_info(token)
    if not user.get('open_id'):
        raise ValueError('Lark identity unavailable')
    grant = secrets.token_urlsafe(32)
    with _delegated_lock:
        for key in list(_delegated):
            if _delegated[key]['expires'] <= time.time():
                del _delegated[key]
        _delegated[grant] = {'token': token, 'open_id': user['open_id'], 'expires': time.time() + 1800}
    return dict(user, grant_id=grant)
