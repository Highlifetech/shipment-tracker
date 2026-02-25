"""
Carrier Tracking Clients

Uses the official FedEx Track API (requires credentials).
Uses free public tracking endpoints for USPS/Royal Mail.
UPS and DHL use their respective APIs (existing credentials).
Each carrier returns a normalized status dict.
"""

import logging
import time
import re
import json
import requests

from config import (
    FEDEX_API_KEY,
    FEDEX_SECRET_KEY,
    UPS_CLIENT_ID,
    UPS_CLIENT_SECRET,
    DHL_API_KEY,
    STATUS_MAP,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_result(status: str, delivery_date: str = "",
                     location: str = "", raw_status: str = "",
                     error: str = "") -> dict:
    """Return a standardized tracking result."""
    return {
        "status": STATUS_MAP.get(status, STATUS_MAP["unknown"]),
        "status_key": status,
        "delivery_date": delivery_date,
        "location": location,
        "raw_status": raw_status,
        "error": error,
    }


def _safe_expires(data: dict, key: str = "expires_in",
                  default: int = 3600) -> int:
    """Safely extract token expiry as an integer."""
    val = data.get(key, default)
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return default


# =============================================================================
# FedEx Track API v1
# https://developer.fedex.com/api/en-us/catalog/track/v1/docs.html
# =============================================================================

class FedExTracker:
    """FedEx Track API v1 (official credentials)."""

    TOKEN_URL = "https://apis.fedex.com/oauth/token"
    TRACK_URL = "https://apis.fedex.com/track/v1/trackingnumbers"

    def __init__(self):
        self.token = None
        self.token_expires = 0

    def _authenticate(self):
        if self.token and time.time() < self.token_expires:
            return self.token
        if not FEDEX_API_KEY or not FEDEX_SECRET_KEY:
            raise Exception("FedEx API credentials not configured")
        resp = requests.post(self.TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": FEDEX_API_KEY,
            "client_secret": FEDEX_SECRET_KEY,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.token_expires = time.time() + _safe_expires(data) - 300
        return self.token

    def track(self, tracking_number: str) -> dict:
        try:
            token = self._authenticate()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            body = {
                "trackingInfo": [
                    {"trackingNumberInfo": {"trackingNumber": tracking_number}}
                ],
                "includeDetailedScans": False,
            }
            resp = requests.post(self.TRACK_URL, headers=headers,
                                 json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            results = (data.get("output", {})
                       .get("completeTrackResults", [{}])[0]
                       .get("trackResults", [{}])[0])

            if results.get("error"):
                return normalize_result(
                    "not_found",
                    error=results["error"].get("message", ""))

            latest = results.get("latestStatusDetail", {})
            status_code = latest.get("code", "").upper()
            raw_status = latest.get("description", "")

            location_info = latest.get("scanLocation", {})
            location = ", ".join(filter(None, [
                location_info.get("city"),
                location_info.get("stateOrProvinceCode"),
                location_info.get("countryCode"),
            ]))

            status_map = {
                "DL": "delivered",
                "IT": "in_transit",
                "OD": "out_for_delivery",
                "DE": "exception",
                "PU": "in_transit",
                "PL": "label_created",
            }
            status = status_map.get(status_code, "in_transit")

            delivery_date = ""
            for d in results.get("dateAndTimes", []):
                if d.get("type") in ("ACTUAL_DELIVERY",
                                     "ESTIMATED_DELIVERY"):
                    delivery_date = d.get("dateTime", "")[:10]
                    break

            return normalize_result(status, delivery_date,
                                    location, raw_status)

        except Exception as e:
            logger.error(
                f"FedEx tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))

# =============================================================================
# UPS Tracking API (existing credentials - kept as-is since it works)
# =============================================================================

class UPSTracker:
    """UPS Tracking API v1."""

    TOKEN_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
    TRACK_URL = "https://onlinetools.ups.com/api/track/v1/details"

    def __init__(self):
        self.token = None
        self.token_expires = 0

    def _authenticate(self):
        if self.token and time.time() < self.token_expires:
            return self.token
        if not UPS_CLIENT_ID or not UPS_CLIENT_SECRET:
            raise Exception("UPS API credentials not configured")
        resp = requests.post(
            self.TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.token_expires = time.time() + _safe_expires(data, "expires_in", 14400) - 300
        return self.token

    def track(self, tracking_number: str) -> dict:
        try:
            token = self._authenticate()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "transId": f"track-{tracking_number[:20]}",
                "transactionSrc": "lark-tracking-bot",
            }
            url = f"{self.TRACK_URL}/{tracking_number}"
            resp = requests.get(
                url, headers=headers,
                params={"locale": "en_US", "returnSignature": "false"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            track_resp = data.get("trackResponse", {})
            shipment = track_resp.get("shipment", [{}])[0]
            package = shipment.get("package", [{}])[0]
            activity = package.get("activity", [])

            if not activity:
                return normalize_result("not_found")

            latest = activity[0]
            status_obj = latest.get("status", {})
            status_type = status_obj.get("type", "").upper()
            raw_status = status_obj.get("description", "")

            location_obj = latest.get("location", {}).get("address", {})
            location = ", ".join(filter(None, [
                location_obj.get("city"),
                location_obj.get("stateProvince"),
                location_obj.get("country"),
            ]))

            status_map = {
                "D": "delivered",
                "I": "in_transit",
                "P": "in_transit",
                "M": "label_created",
                "X": "exception",
                "O": "out_for_delivery",
            }
            status = status_map.get(status_type, "in_transit")

            delivery_date = ""
            del_date = package.get("deliveryDate", [])
            if del_date:
                d = del_date[0] if isinstance(del_date, list) else del_date
                date_str = str(d.get("date", ""))
                if date_str and len(date_str) == 8:
                    delivery_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            if status == "delivered" and not delivery_date:
                date_str = str(latest.get("date", ""))
                if date_str and len(date_str) == 8:
                    delivery_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            return normalize_result(status, delivery_date, location, raw_status)

        except Exception as e:
            logger.error(f"UPS tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))

# =============================================================================
# USPS - Scrapes the public USPS tracking page (no API key needed)
# =============================================================================

class USPSTracker:
    """Scrapes the USPS public tracking page."""

    TRACK_URL = "https://tools.usps.com/go/TrackConfirmAction"

    def track(self, tracking_number: str) -> dict:
        try:
            headers = {**HEADERS, "Referer": "https://tools.usps.com/go/TrackConfirmAction"}
            resp = requests.get(
                self.TRACK_URL,
                params={"tLabels": tracking_number},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            html = resp.text

            raw_status = ""
            status = "in_transit"
            delivery_date = ""

            if re.search(r'Delivered', html, re.IGNORECASE):
                status = "delivered"
                raw_status = "Delivered"
                date_match = re.search(
                    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
                    html, re.IGNORECASE
                )
                if date_match:
                    try:
                        from datetime import datetime
                        delivery_date = datetime.strptime(
                            date_match.group(0), "%B %d, %Y"
                        ).strftime("%Y-%m-%d")
                    except Exception:
                        pass
            elif re.search(r'Out for Delivery', html, re.IGNORECASE):
                status = "out_for_delivery"
                raw_status = "Out for Delivery"
            elif re.search(r'In Transit', html, re.IGNORECASE):
                status = "in_transit"
                raw_status = "In Transit"
            elif re.search(r'Alert', html, re.IGNORECASE):
                status = "exception"
                raw_status = "Alert"
            elif re.search(r'Pre-Shipment|Label Created', html, re.IGNORECASE):
                status = "label_created"
                raw_status = "Pre-Shipment Info Sent"
            else:
                if "not found" in html.lower() or "not available" in html.lower():
                    return normalize_result("not_found")
                raw_status = "In Transit"

            return normalize_result(status, delivery_date, "", raw_status)

        except Exception as e:
            logger.error(f"USPS tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))

# =============================================================================
# DHL Tracking API (existing - kept as-is)
# =============================================================================

class DHLTracker:
    """DHL Unified Tracking API."""

    TRACK_URL = "https://api-eu.dhl.com/track/shipments"

    def track(self, tracking_number: str) -> dict:
        try:
            if not DHL_API_KEY:
                raise Exception("DHL API key not configured")
            resp = requests.get(
                self.TRACK_URL,
                headers={"DHL-API-Key": DHL_API_KEY},
                params={"trackingNumber": tracking_number},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            shipments = data.get("shipments", [])
            if not shipments:
                return normalize_result("not_found")

            shipment = shipments[0]
            status_obj = shipment.get("status", {})
            status_code = status_obj.get("statusCode", "").lower()
            raw_status = status_obj.get("description", "")

            location = (status_obj.get("location", {})
                        .get("address", {})
                        .get("addressLocality", ""))

            status_map = {
                "delivered": "delivered",
                "transit": "in_transit",
                "failure": "exception",
                "pre-transit": "label_created",
                "unknown": "unknown",
            }
            status = status_map.get(status_code, "in_transit")

            delivery_date = ""
            if status == "delivered":
                ts = status_obj.get("timestamp", "")
                if ts:
                    delivery_date = ts[:10]
            elif shipment.get("estimatedTimeOfDelivery"):
                etd = shipment["estimatedTimeOfDelivery"]
                delivery_date = etd[:10] if isinstance(etd, str) else ""

            return normalize_result(status, delivery_date, location, raw_status)

        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 404:
                return normalize_result("not_found")
            logger.error(f"DHL tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))
        except Exception as e:
            logger.error(f"DHL tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))

# =============================================================================
# Royal Mail - Uses public Royal Mail tracking API (no API key needed)
# =============================================================================

class RoyalMailTracker:
    """Uses the Royal Mail public tracking endpoint."""

    def track(self, tracking_number: str) -> dict:
        try:
            url = f"https://api.royalmail.com/mailpieces/v2/{tracking_number}/events"
            headers = {
                **HEADERS,
                "Accept": "application/json",
                "Referer": f"https://www.royalmail.com/track-your-item#/tracking-results/{tracking_number}",
            }
            resp = requests.get(url, headers=headers, timeout=30)

            if resp.status_code == 404:
                return normalize_result("not_found")

            if resp.status_code == 200:
                data = resp.json()
                mail_pieces = data.get("mailPieces", [])
                if not mail_pieces:
                    return normalize_result("not_found")

                piece = mail_pieces[0]
                events = piece.get("events", [])
                summary = piece.get("summary", {})
                status_desc = summary.get("statusDescription", "").lower()
                raw_status = summary.get("statusDescription", "")

                if "delivered" in status_desc:
                    status = "delivered"
                elif "out for delivery" in status_desc or "with delivery" in status_desc:
                    status = "out_for_delivery"
                elif "exception" in status_desc or "returned" in status_desc or "failed" in status_desc:
                    status = "exception"
                elif "posted" in status_desc or "dispatched" in status_desc or "collected" in status_desc:
                    status = "label_created"
                elif status_desc:
                    status = "in_transit"
                else:
                    status = "in_transit"

                delivery_date = ""
                if status == "delivered" and events:
                    ts = events[0].get("eventDateTime", "")
                    if ts:
                        delivery_date = ts[:10]

                estimated = summary.get("estimatedDeliveryDate", {})
                if estimated and not delivery_date:
                    start = estimated.get("startOfEstimatedWindow", "")
                    if start:
                        delivery_date = start[:10]

                location = ""
                if events:
                    location = events[0].get("locationName", "")

                return normalize_result(status, delivery_date, location, raw_status)

            # Fallback: scrape the tracking page
            resp2 = requests.get(
                "https://www.royalmail.com/track-your-item",
                params={"trackNumber": tracking_number},
                headers=HEADERS,
                timeout=30,
            )
            html = resp2.text
            if "delivered" in html.lower():
                return normalize_result("delivered", "", "", "Delivered")
            elif "out for delivery" in html.lower():
                return normalize_result("out_for_delivery", "", "", "Out for Delivery")
            elif "exception" in html.lower() or "returned" in html.lower():
                return normalize_result("exception", "", "", "Exception")
            elif "not found" in html.lower():
                return normalize_result("not_found")
            else:
                return normalize_result("in_transit", "", "", "In Transit")

        except Exception as e:
            logger.error(f"Royal Mail tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))


# =============================================================================
# Unified Tracker
# =============================================================================

class CarrierTracker:
    """Routes tracking requests to the correct carrier."""

    def __init__(self):
        self.fedex = FedExTracker()
        self.ups = UPSTracker()
        self.usps = USPSTracker()
        self.dhl = DHLTracker()
        self.royalmail = RoyalMailTracker()
        self._clients = {
            "fedex": self.fedex,
            "ups": self.ups,
            "usps": self.usps,
            "dhl": self.dhl,
            "royalmail": self.royalmail,
        }

    def track(self, tracking_number: str, carrier: str) -> dict:
        """Track a shipment using the appropriate carrier."""
        client = self._clients.get(carrier)
        if not client:
            logger.warning(f"Unknown carrier '{carrier}' for tracking {tracking_number}")
            return normalize_result("unknown", error=f"Unsupported carrier: {carrier}")
        logger.info(f"Tracking {tracking_number} via {carrier.upper()}")
        return client.track(tracking_number)
