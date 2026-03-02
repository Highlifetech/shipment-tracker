"""
Lark Shipment Tracking Bot - Webhook Server

Runs as a persistent web server (via gunicorn on Railway).
When the bot is @mentioned in the HLT INBOUND DELIVERIES group chat,
Lark sends a POST to /webhook. The server:
  1. Answers the URL verification challenge (one-time setup).
  2. On @mention, runs the full shipment tracker and replies in-thread.

Deployed the same way as IronBot:
  - Procfile: web: gunicorn bot_server:app --bind 0.0.0.0:$PORT
  - Railway environment variables (same as GitHub Secrets)
"""

import os
import json
import re
import logging
import threading
import time
import requests
from flask import Flask, request, jsonify
from main import run_tracker
from lark_client import LarkClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
BOT_NAME = os.environ.get("BOT_NAME", "API Inbound Shipments Tracker")

lark = LarkClient()

# Bot's own open_id - fetched at startup so we can detect @mentions reliably
BOT_OPEN_ID = None

# Deduplication: prevent double-processing if Lark retries
processed_message_ids = {}
DEDUP_TTL = 300  # seconds


def _fetch_bot_open_id():
    """Fetch the bot's own open_id from Lark API at startup."""
    global BOT_OPEN_ID
    try:
        url = lark.base_url + "/open-apis/bot/v3/info"
        resp = requests.get(url, headers=lark._headers(), timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            BOT_OPEN_ID = data.get("bot", {}).get("open_id", "")
            logger.info("Bot open_id: %s", BOT_OPEN_ID)
        else:
            logger.warning("Could not fetch bot info: %s", data)
    except Exception as e:
        logger.warning("Error fetching bot open_id: %s", e)


def _is_already_processed(message_id):
    """Return True if we've already handled this message (dedup)."""
    now = time.time()
    expired = [mid for mid, ts in processed_message_ids.items() if now - ts > DEDUP_TTL]
    for mid in expired:
        del processed_message_ids[mid]
    if message_id in processed_message_ids:
        return True
    processed_message_ids[message_id] = now
    return False


def extract_question(msg):
    """Return the message text ONLY if the bot was @mentioned, else None."""
    try:
        content = json.loads(msg.get("content", "{}"))
        raw_text = content.get("text", "").strip()
    except Exception:
        return None

    if not raw_text:
        return None

    # Direct/P2P chat - always respond
    if msg.get("chat_type", "") == "p2p":
        return raw_text

    # Group chat - only respond if bot is in the mentions list
    mentions = msg.get("mentions", [])
    bot_mentioned = False
    for mention in mentions:
        mid = mention.get("id", {})
        mention_open_id = mid.get("open_id", "")
        mention_name = mention.get("name", "")
        if BOT_OPEN_ID and mention_open_id == BOT_OPEN_ID:
            bot_mentioned = True
            break
        if BOT_NAME and BOT_NAME.lower() in mention_name.lower():
            bot_mentioned = True
            break

    if not bot_mentioned:
        logger.info("Bot not mentioned - ignoring message")
        return None

    # Strip @mention tag from text before returning
    clean = re.sub(r'@[^\s]+', '', raw_text).strip()
    return clean if clean else raw_text


def _run_and_reply(chat_id, message_id):
    """Run the full tracker and send summary back - called in background thread."""
    try:
        logger.info("@mention trigger: chat=%s message=%s", chat_id, message_id)
        run_tracker(
            dry_run=False,
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception as e:
        logger.error("Error during @mention-triggered run: %s", e)
        try:
            lark.send_group_message(
                "Error running shipment tracker: " + str(e)[:200],
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:
            pass


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}

    # 1. URL verification challenge (one-time during Lark bot setup)
    if body.get("type") == "url_verification":
        logger.info("URL verification challenge answered")
        return jsonify({"challenge": body.get("challenge", "")})

    # 2. Message event
    event = body.get("event", {})
    msg = event.get("message", {})

    if msg.get("message_type") != "text":
        return jsonify({"code": 0})

    message_id = msg.get("message_id", "")
    if _is_already_processed(message_id):
        logger.info("Duplicate message ignored: %s", message_id)
        return jsonify({"code": 0})

    user_text = extract_question(msg)
    if not user_text:
        return jsonify({"code": 0})

    chat_id = msg.get("chat_id", "")
    if not chat_id:
        return jsonify({"code": 0})

    logger.info("@mention received in chat=%s - launching tracker", chat_id)

    # Run in background so we return 200 to Lark within 3s
    threading.Thread(
        target=_run_and_reply,
        args=(chat_id, message_id),
        daemon=True,
    ).start()

    return jsonify({"code": 0})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot_open_id": BOT_OPEN_ID})


@app.route("/list-chats", methods=["GET"])
def list_chats():
    """Helper endpoint to look up the group chat ID."""
    try:
        url = lark.base_url + "/open-apis/im/v1/chats"
        resp = requests.get(url, headers=lark._headers(),
                            params={"page_size": 100}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            return jsonify({"error": data})
        chats = data.get("data", {}).get("items", [])
        result = [{"chat_id": c.get("chat_id"), "name": c.get("name", "")} for c in chats]
        return jsonify({"chats": result, "count": len(result)})
    except Exception as e:
        return jsonify({"error": str(e)})


# -------------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------------

_fetch_bot_open_id()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting shipment tracker webhook server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
