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
LARK_CHAT_ID = os.environ.get("LARK_CHAT_ID", "")

# =============================================================================
# LARK SHEETS TO SCAN
# =============================================================================
# Comma-separated list of sheet tokens
# Extract from URL: https://xxx.jp.larksuite.com/sheets/<SHEET_TOKEN>
SHEET_TOKENS = [
    t.strip()
    for t in os.environ.get("LARK_SHEET_TOKENS", "").split(",")
    if t.strip()
]

# =============================================================================
# COLUMN MAPPING (letters A-Q)
# =============================================================================
COLUMNS = {
    "shipment_id":   "A",
    "vendor":        "B",
    "recipient":     "C",
    "order_num":     "D",
    "customer":      "E",
    "product_photo": "F",
    "tracking_num":  "G",
    "carrier":       "H",
    "qty_shipped":   "I",
    "qty_expected":  "J",
    "discrepancy":   "K",
    "balance_owed":  "L",
    "status":        "M",
    "tariff_charge": "N",
    "num_boxes":     "O",
    "notes":         "P",
    "delivery_date": "Q",
}

# Header row (1-indexed) — data starts on the row after this
HEADER_ROW = 2

# =============================================================================
# CARRIER API CREDENTIALS
# FedEx, USPS, Royal Mail use free public scraping — no keys needed.
# UPS and DHL use API credentials below.
# =============================================================================

# UPS — https://developer.ups.com
UPS_CLIENT_ID = os.environ.get("UPS_CLIENT_ID", "")
UPS_CLIENT_SECRET = os.environ.get("UPS_CLIENT_SECRET", "")

# DHL — https://developer.dhl.com (free tier)
DHL_API_KEY = os.environ.get("DHL_API_KEY", "")

# =============================================================================
# BOT SETTINGS
# =============================================================================
# Sheet tabs to skip
SKIP_TABS = {"TEMPLATE"}

# Carrier name normalization — maps values in sheet column H to API client keys
CARRIER_ALIASES = {
    # FedEx
    "fedex":             "fedex",
    "fed ex":            "fedex",
    "federal express":   "fedex",
    # UPS
    "ups":               "ups",
    "united parcel":     "ups",
    # USPS
    "usps":              "usps",
    "us postal":         "usps",
    "united states postal": "usps",
    # DHL
    "dhl":               "dhl",
    "dhl express":       "dhl",
    # Royal Mail
    "royal mail":        "royalmail",
    "royalmail":         "royalmail",
    "royal":             "royalmail",
    "rm":                "royalmail",
}

# Status values the bot writes to the sheet (column M)
STATUS_MAP = {
    "delivered":       "DELIVERED",
    "in_transit":      "IN TRANSIT",
    "out_for_delivery": "OUT FOR DELIVERY",
    "exception":       "EXCEPTION",
    "pending":         "PENDING",
    "label_created":   "LABEL CREATED",
    "unknown":         "UNKNOWN",
    "not_found":       "NOT FOUND",
}
