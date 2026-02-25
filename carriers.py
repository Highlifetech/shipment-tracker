"""
Carrier Tracking Clients
- FedEx/USPS/RoyalMail: use 17track.net free API (no carrier account needed)
- UPS: uses existing UPS Tracking API credentials
- DHL: uses existing DHL API key
"""
import logging
import time
import os
import requests
from config import (
    UPS_CLIENT_ID,
    UPS_CLIENT_SECRET,
    DHL_API_KEY,
    STATUS_MAP,
)

logger = logging.getLogger(__name__)

TRACK17_API_KEY = os.environ.get("TRACK17_API_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def normalize_result(status: str, delivery_date: str = "", location: str = "",
                     raw_status: str = "", error: str = "") -> dict:
    """Return a standardized tracking result."""
    return {
        "status": STATUS_MAP.get(status, STATUS_MAP["unknown"]),
        "status_key": status,
        "delivery_date": delivery_date,
        "location": location,
        "raw_status": raw_status,
        "error": error,
    }


def _safe_expires(data: dict, key: str = "expires_in", default: int = 3600) -> int:
    """Safely extract token expiry as an integer."""
    val = data.get(key, default)
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return default


# =============================================================================
# 17track.net API — free tier supports FedEx, USPS, Royal Mail, and 1500+ carriers
# Sign up free at: https://www.17track.net/en/apidoc
# Free plan: 100 tracking numbers/month
# =============================================================================
class Track17Tracker:
    """17track.net API tracker - handles FedEx, USPS, Royal Mail and more."""

    # Carrier codes for 17track
    # https://www.17track.net/en/apidoc#carrier-list
    CARRIER_CODES = {
        "fedex":     100003,   # FedEx
        "usps":      100001,   # USPS
        "royalmail": 190001,   # Royal Mail
        "dhl":       100002,   # DHL (fallback if DHL API fails)
        "ups":       100005,   # UPS (fallback)
    }

    REGISTER_URL = "https://api.17track.net/track/v2.2/register"
    GETTRACK_URL = "https://api.17track.net/track/v2.2/gettrackinfo"

    def track(self, tracking_number: str, carrier: str) -> dict:
        """Track via 17track.net API."""
        if not TRACK17_API_KEY:
            return normalize_result("unknown", error="17track API key not configured. Get free key at https://www.17track.net/en/apidoc")

        carrier_code = self.CARRIER_CODES.get(carrier)

        try:
            headers = {
                "17token": TRACK17_API_KEY,
                "Content-Type": "application/json",
            }

            # Step 1: Register the tracking number
            reg_payload = [{"number": tracking_number}]
            if carrier_code:
                reg_payload[0]["carrier"] = carrier_code

            reg_resp = requests.post(
                self.REGISTER_URL,
                headers=headers,
                json=reg_payload,
                timeout=30,
            )
            reg_resp.raise_for_status()

            # Step 2: Get tracking info
            get_payload = [{"number": tracking_number}]
            if carrier_code:
                get_payload[0]["carrier"] = carrier_code

            get_resp = requests.post(
                self.GETTRACK_URL,
                headers=headers,
                json=get_payload,
                timeout=30,
            )
            get_resp.raise_for_status()
            data = get_resp.json()

            # Parse response
            accepted = data.get("data", {}).get("accepted", [])
            if not accepted:
                rejected = data.get("data", {}).get("rejected", [])
                if rejected:
                    err = rejected[0].get("error", {}).get("message", "Tracking rejected")
                    return normalize_result("not_found", error=err)
                return normalize_result("not_found")

            track_info = accepted[0].get("track", {})
            if not track_info:
                return normalize_result("not_found")

            # 17track status codes:
            # 0=Not Found, 10=In Transit, 20=Expired, 30=Pick Up, 35=Undelivered, 40=Delivered, 50=Exception
            # e_status is the numeric status
            e_status = track_info.get("e", 0)
            z0 = track_info.get("z0", {})   # latest event
            z1 = track_info.get("z1", {})   # delivery event

            raw_status = (z0.get("z") or z0.get("a", "")).strip() if z0 else ""
            location = (z0.get("l") or "").strip() if z0 else ""

            status_map_17 = {
                0:  "not_found",
                10: "in_transit",
                20: "exception",    # expired
                30: "in_transit",   # pick up / accepted
                35: "exception",    # undelivered
                40: "delivered",
                50: "exception",
            }
            status = status_map_17.get(e_status, "in_transit")

            # Delivery date
            delivery_date = ""
            if status == "delivered" and z1:
                ts = z1.get("a", "")
                if ts and len(ts) >= 10:
                    delivery_date = ts[:10]
            elif status == "in_transit":
                # Check for estimated delivery in latest event
                ts = z0.get("a", "") if z0 else ""
                # 17track doesn't provide estimated delivery directly, leave blank

            return normalize_result(status, delivery_date, location, raw_status)

        except Exception as e:
            logger.error(f"17track error for {tracking_number} ({carrier}): {e}")
            return normalize_result("unknown", error=str(e))


# =============================================================================
# FedEx — routes through 17track
# =============================================================================
class FedExTracker:
    def __init__(self):
        self._17track = Track17Tracker()

    def track(self, tracking_number: str) -> dict:
        return self._17track.track(tracking_number, "fedex")


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
# USPS — routes through 17track
# =============================================================================
class USPSTracker:
    def __init__(self):
        self._17track = Track17Tracker()

    def track(self, tracking_number: str) -> dict:
        return self._17track.track(tracking_number, "usps")


# =============================================================================
# DHL Tracking API (existing - kept as-is, falls back to 17track if no key)
# =============================================================================
class DHLTracker:
    """DHL Unified Tracking API."""

    TRACK_URL = "https://api-eu.dhl.com/track/shipments"

    def __init__(self):
        self._17track = Track17Tracker()

    def track(self, tracking_number: str) -> dict:
        # Use DHL API if key configured, else fall back to 17track
        if not DHL_API_KEY:
            return self._17track.track(tracking_number, "dhl")
        try:
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
            return self._17track.track(tracking_number, "dhl")
        except Exception as e:
            logger.error(f"DHL tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))


# =============================================================================
# Royal Mail — routes through 17track
# =============================================================================
class RoyalMailTracker:
    def __init__(self):
        self._17track = Track17Tracker()

    def track(self, tracking_number: str) -> dict:
        return self._17track.track(tracking_number, "royalmail")


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
