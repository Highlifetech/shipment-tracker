"""
Configuration for Lark Tracking Bot
All settings are loaded from environment variables (GitHub Secrets)
"""
import os

# =============================================================================
# LARK APP CREDENTIALS (from Lark Developer Console)
# =============================================================================
LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

# JP-region endpoint for Lark Suite
LARK_BASE_URL = os.environ.get("LARK_BASE_URL", "https://open.larksuite.com")

# =============================================================================
# LARK GROUP CHAT for notifications
# =============================================================================
# The chat_id of the Lark group where the bot sends daily summaries
LARK_CHAT_ID = os.environ.get("LARK_CHAT_ID", "")

# =============================================================================
# LARK SHEETS TO SCAN
# =============================================================================
# Comma-separated list of sheet tokens
# Extract from URL: https://xxx.jp.larksuite.com/sheets/<SHEET_TOKEN>
# Example: "OJlkscQ9AhrmWZtTAmEjw8japgV,AnotherSheetToken123"
SHEET_TOKENS = [
    t.strip()
    for t in os.environ.get("LARK_SHEET_TOKENS", "").split(",")
    if t.strip()
]

# =============================================================================
# COLUMN MAPPING (0-indexed from A)
# Adjust if your sheet layout differs
# =============================================================================
COLUMNS = {
    "shipment_id": "A",
    "vendor": "B",
    "recipient": "C",
    "order_num": "D",
    "customer": "E",
    "product_photo": "F",
    "tracking_num": "G",
    "carrier": "H",
    "qty_shipped": "I",
    "qty_expected": "J",
    "discrepancy": "K",
    "balance_owed": "L",
    "status": "M",
    "tariff_charge": "N",
    "num_boxes": "O",
    "notes": "P",
    # New column added by bot:
    "delivery_date": "Q",
}

# Header row (1-indexed) — data starts on the row after this
HEADER_ROW = 2

# =============================================================================
# CARRIER API CREDENTIALS (all free tier)
# =============================================================================

# FedEx — https://developer.fedex.com
FEDEX_API_KEY = os.environ.get("FEDEX_API_KEY", "")
FEDEX_SECRET_KEY = os.environ.get("FEDEX_SECRET_KEY", "")

# UPS — https://developer.ups.com
UPS_CLIENT_ID = os.environ.get("UPS_CLIENT_ID", "")
UPS_CLIENT_SECRET = os.environ.get("UPS_CLIENT_SECRET", "")

# USPS — https://developer.usps.com
USPS_CLIENT_ID = os.environ.get("USPS_CLIENT_ID", "")
USPS_CLIENT_SECRET = os.environ.get("USPS_CLIENT_SECRET", "")

# DHL — https://developer.dhl.com
DHL_API_KEY = os.environ.get("DHL_API_KEY", "")

# =============================================================================
# BOT SETTINGS
# =============================================================================

# Sheets to skip (e.g. "TEMPLATE")
SKIP_TABS = {"TEMPLATE"}

# Carrier name normalization (maps what's in the sheet to our API client keys)
CARRIER_ALIASES = {
    "ups": "ups",
    "fedex": "fedex",
    "fed ex": "fedex",
    "federal express": "fedex",
    "usps": "usps",
    "us postal": "usps",
    "united states postal": "usps",
    "dhl": "dhl",
    "dhl express": "dhl",
}

# Status values the bot writes
STATUS_MAP = {
    "delivered": "DELIVERED",
    "in_transit": "IN TRANSIT",
    "out_for_delivery": "OUT FOR DELIVERY",
    "exception": "EXCEPTION",
    "pending": "PENDING",
    "label_created": "LABEL CREATED",
    "unknown": "UNKNOWN",
    "not_found": "NOT FOUND",
}
