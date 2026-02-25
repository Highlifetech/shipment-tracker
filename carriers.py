"""
Carrier Tracking API Clients

Direct integration with FedEx, UPS, USPS, DHL, and Royal Mail APIs.
Each carrier returns a normalized status dict.
"""

import logging
import time

import requests
from config import (
    FEDEX_API_KEY,
    FEDEX_SECRET_KEY,
    UPS_CLIENT_ID,
    UPS_CLIENT_SECRET,
    USPS_CLIENT_ID,
    USPS_CLIENT_SECRET,
    DHL_API_KEY,
    ROYALMAIL_API_KEY,
    STATUS_MAP,
)

logger = logging.getLogger(__name__)


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
# FedEx Tracking API
# https://developer.fedex.com/api/en-us/catalog/track/v1/docs.html
# =============================================================================
class FedExTracker:
        """FedEx Track API v1."""

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
                                resp = requests.post(self.TRACK_URL, headers=headers, json=body, timeout=30)
                                resp.raise_for_status()
                                data = resp.json()
                                results = (data.get("output", {})
                                           .get("completeTrackResults", [{}])[0]
                                           .get("trackResults", [{}])[0])

            if results.get("error"):
                                return normalize_result("not_found",
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
                                if d.get("type") in ("ACTUAL_DELIVERY", "ESTIMATED_DELIVERY"):
                                                        delivery_date = d.get("dateTime", "")[:10]
                                                        break

                            return normalize_result(status, delivery_date, location, raw_status)
except Exception as e:
            logger.error(f"FedEx tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))


# =============================================================================
# UPS Tracking API
# https://developer.ups.com/api/reference/tracking
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
                                resp = requests.get(url, headers=headers,
                                                    params={"locale": "en_US", "returnSignature": "false"},
                                                    timeout=30)
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
# USPS Tracking API
# https://developer.usps.com/api/81
# =============================================================================
class USPSTracker:
        """USPS Tracking API v3."""

    TOKEN_URL = "https://api.usps.com/oauth2/v3/token"
    TRACK_URL = "https://api.usps.com/tracking/v3/tracking"

    def __init__(self):
                self.token = None
        self.token_expires = 0

    def _authenticate(self):
                if self.token and time.time() < self.token_expires:
                                return self.token
                            if not USPS_CLIENT_ID or not USPS_CLIENT_SECRET:
                                            raise Exception("USPS API credentials not configured")
                                        resp = requests.post(self.TOKEN_URL, data={
                                                        "grant_type": "client_credentials",
                                                        "client_id": USPS_CLIENT_ID,
                                                        "client_secret": USPS_CLIENT_SECRET,
                                        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.token_expires = time.time() + _safe_expires(data) - 300
        return self.token

    def track(self, tracking_number: str) -> dict:
                try:
                                token = self._authenticate()
                                url = f"{self.TRACK_URL}/{tracking_number}"
                                resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                                                    timeout=30)
                                resp.raise_for_status()
                                data = resp.json()

            status_category = data.get("statusCategory", "").upper()
            raw_status = data.get("status", "")

            location_parts = []
            if data.get("destinationCity"):
                                location_parts.append(data["destinationCity"])
                            if data.get("destinationState"):
                                                location_parts.append(data["destinationState"])
                                            location = ", ".join(location_parts)

            status_map = {
                                "DELIVERED": "delivered",
                                "IN_TRANSIT": "in_transit",
                                "OUT_FOR_DELIVERY": "out_for_delivery",
                                "ALERT": "exception",
                                "PRE_TRANSIT": "label_created",
                                "ACCEPTED": "in_transit",
            }
            status = status_map.get(status_category, "in_transit")

            delivery_date = ""
            if data.get("actualDeliveryDate"):
                                delivery_date = data["actualDeliveryDate"][:10]
elif data.get("expectedDeliveryDate"):
                delivery_date = data["expectedDeliveryDate"][:10]

            return normalize_result(status, delivery_date, location, raw_status)
except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 404:
                                return normalize_result("not_found")
                            logger.error(f"USPS tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))
except Exception as e:
            logger.error(f"USPS tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))


# =============================================================================
# DHL Tracking API
# https://developer.dhl.com/api-reference/shipment-tracking
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
# Royal Mail Tracking API
# https://developer.royalmail.net/api/tracking
# Uses the Royal Mail Tracking API v2 (free tier available)
# =============================================================================
class RoyalMailTracker:
        """Royal Mail Tracking API v2."""

    TRACK_URL = "https://api.royalmail.net/tracking/v2/events"

    def track(self, tracking_number: str) -> dict:
                try:
                                if not ROYALMAIL_API_KEY:
                                                    raise Exception("Royal Mail API key not configured")

                                headers = {
                                    "x-ibm-client-id": ROYALMAIL_API_KEY,
                                    "Accept": "application/json",
                                }
                                resp = requests.get(
                                    self.TRACK_URL,
                                    headers=headers,
                                    params={"trackingNumber": tracking_number},
                                    timeout=30,
                                )
                                resp.raise_for_status()
                                data = resp.json()

            mail_pieces = data.get("mailPieces", [])
            if not mail_pieces:
                                return normalize_result("not_found")

            piece = mail_pieces[0]
            events = piece.get("events", [])
            summary = piece.get("summary", {})

            status_desc = summary.get("statusDescription", "").lower()
            raw_status = summary.get("statusDescription", "")

            location = ""
            if events:
                                latest = events[0]
                                location = latest.get("locationName", "")

            if "delivered" in status_desc:
                                status = "delivered"
elif "out for delivery" in status_desc or "with delivery" in status_desc:
                status = "out_for_delivery"
elif "collected" in status_desc or "accepted" in status_desc:
                status = "in_transit"
elif "in transit" in status_desc or "transit" in status_desc:
                status = "in_transit"
elif "arrived" in status_desc:
                status = "in_transit"
elif "exception" in status_desc or "returned" in status_desc or "failed" in status_desc:
                status = "exception"
elif "posted" in status_desc or "dispatched" in status_desc:
                status = "label_created"
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

                            return normalize_result(status, delivery_date, location, raw_status)
except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 404:
                                return normalize_result("not_found")
                            logger.error(f"Royal Mail tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))
except Exception as e:
            logger.error(f"Royal Mail tracking error for {tracking_number}: {e}")
            return normalize_result("unknown", error=str(e))


# =============================================================================
# Unified Tracker
# =============================================================================
class CarrierTracker:
        """Routes tracking requests to the correct carrier API."""

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
                """Track a shipment using the appropriate carrier API."""
        client = self._clients.get(carrier)
        if not client:
                        logger.warning(f"Unknown carrier '{carrier}' for tracking {tracking_number}")
                        return normalize_result("unknown", error=f"Unsupported carrier: {carrier}")
                    logger.info(f"Tracking {tracking_number} via {carrier.upper()}")
        return client.track(tracking_number)
