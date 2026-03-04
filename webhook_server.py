"""
Lark Shipment Tracking Bot - Webhook Server

Runs as a persistent web server (via gunicorn on Railway).
Handles two responsibilities:
  1. Scheduled runs: 8am and 8pm EST full summary, every hour exception check
  2. @mention trigger: when the bot is @mentioned, run the full tracker and reply in-thread

Schedule (all times US Eastern):
  - 8:00 AM  -> full shipment summary to HLT INBOUND DELIVERIES
  - 8:00 PM  -> full shipment summary to HLT INBOUND DELIVERIES
  - Every hour at :00  -> exception/delay check, only alerts if something is wrong

Deployed on Railway:
  - Procfile: web: gunicorn webhook_server:app --bind 0.0.0.0:$PORT
  - Environment variables match GitHub Secrets
"""

import os
import json
import logging
import threading
import time
import requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from main import run_tracker, run_exception_check
from lark_client import LarkClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
BOT_NAME = os.environ.get("BOT_NAME", "API Inbound Shipments Tracker")
LARK_CHAT_ID = os.environ.get("LARK_CHAT_ID", "")

lark = LarkClient()

# Bot open_id - fetched at startup
BOT_OPEN_ID = None

# Deduplication for webhook messages
processed_message_ids = {}
DEDUP_TTL = 300
_dedup_lock = threading.Lock()

# Timezone for scheduling
EASTERN = pytz.timezone("America/New_York")


# -------------------------------------------------------------------------
# Scheduled jobs
# -------------------------------------------------------------------------

def scheduled_full_summary():
    """Send full shipment summary - runs at 8am and 8pm Eastern."""
    logger.info("=== SCHEDULED FULL SUMMARY ===")
    try:
        run_tracker(dry_run=False, chat_id=LARK_CHAT_ID)
        logger.info("Scheduled full summary complete")
    except Exception as e:
        logger.error("Scheduled full summary failed: %s", e)


def scheduled_exception_check():
    """Check for new shipment exceptions - runs every hour."""
    logger.info("=== SCHEDULED EXCEPTION CHECK ===")
    try:
        run_exception_check()
        logger.info("Scheduled exception check complete")
    except Exception as e:
        logger.error("Scheduled exception check failed: %s", e)


def start_scheduler():
    """Start the APScheduler with precise Eastern time schedules."""
    scheduler = BackgroundScheduler(timezone=EASTERN)

    # Full summary at exactly 8:00 AM Eastern
    scheduler.add_job(
        scheduled_full_summary,
        CronTrigger(hour=8, minute=0, timezone=EASTERN),
        id="summary_8am",
        name="8am Full Summary",
        replace_existing=True,
    )

    # Full summary at exactly 8:00 PM Eastern
    scheduler.add_job(
        scheduled_full_summary,
        CronTrigger(hour=20, minute=0, timezone=EASTERN),
        id="summary_8pm",
        name="8pm Full Summary",
        replace_existing=True,
    )

    # Exception check every hour at :00
    scheduler.add_job(
        scheduled_exception_check,
        CronTrigger(minute=0, timezone=EASTERN),
        id="exception_check_hourly",
        name="Hourly Exception Check",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started: 8am summary, 8pm summary, hourly exception check (Eastern time)")
    return scheduler


# -------------------------------------------------------------------------
# Bot helpers
# -------------------------------------------------------------------------

def _fetch_bot_open_id():
    global BOT_OPEN_ID
    try:
        url = lark.base_url + "/open-apis/bot/v3/info"
        resp = requests.get(url, headers=lark._headers(), timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            BOT_OPEN_ID = data.get("bot", {}).get("open_id", "")
            logger.info("Bot open_id fetched: %s", BOT_OPEN_ID)
        else:
            logger.warning("Could not fetch bot info: %s", data)
    except Exception as e:
        logger.warning("Error fetching bot open_id: %s", e)


def _is_already_processed(message_id):
    now = time.time()
    with _dedup_lock:
        expired = [mid for mid, ts in processed_message_ids.items() if now - ts > DEDUP_TTL]
        for mid in expired:
            del processed_message_ids[mid]
        if message_id in processed_message_ids:
            return True
        processed_message_ids[message_id] = now
        return False


def _is_bot_message(event):
    sender = event.get("sender", {})
    if sender.get("sender_type", "") == "bot":
        return True
    sender_open_id = sender.get("sender_id", {}).get("open_id", "")
    if BOT_OPEN_ID and sender_open_id == BOT_OPEN_ID:
        return True
    return False


def _bot_is_mentioned(msg):
    mentions = msg.get("mentions", [])
    for mention in mentions:
        mid = mention.get("id", {})
        if BOT_OPEN_ID and mid.get("open_id", "") == BOT_OPEN_ID:
            return True
        if BOT_NAME and BOT_NAME.lower() in mention.get("name", "").lower():
            return True
    return False


def _run_and_reply(chat_id, message_id):
    try:
        logger.info("@mention trigger: chat=%s message=%s", chat_id, message_id)
        run_tracker(dry_run=False, chat_id=chat_id, message_id=message_id)
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

    if body.get("type") == "url_verification":
        logger.info("URL verification challenge answered")
        return jsonify({"challenge": body.get("challenge", "")})

    header = body.get("header", {})
    event_type = header.get("event_type", "")
    if event_type and event_type != "im.message.receive_v1":
        return jsonify({"code": 0})

    event = body.get("event", {})
    msg = event.get("message", {})

    if msg.get("message_type") != "text":
        return jsonify({"code": 0})

    if _is_bot_message(event):
        logger.info("Ignoring bot's own message")
        return jsonify({"code": 0})

    message_id = msg.get("message_id", "")
    if not message_id:
        return jsonify({"code": 0})

    if _is_already_processed(message_id):
        logger.info("Duplicate message ignored: %s", message_id)
        return jsonify({"code": 0})

    if not _bot_is_mentioned(msg):
        logger.info("Bot not @mentioned - ignoring message")
        return jsonify({"code": 0})

    chat_id = msg.get("chat_id", "")
    if not chat_id:
        return jsonify({"code": 0})

    logger.info("@mention confirmed in chat=%s - launching tracker", chat_id)
    threading.Thread(target=_run_and_reply, args=(chat_id, message_id), daemon=True).start()
    return jsonify({"code": 0})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot_open_id": BOT_OPEN_ID})


@app.route("/list-chats", methods=["GET"])
def list_chats():
    try:
        url = lark.base_url + "/open-apis/im/v1/chats"
        resp = requests.get(url, headers=lark._headers(), params={"page_size": 100}, timeout=30)
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
start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting shipment tracker webhook server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
