"""
Carrier Tracking API Clients
Direct integration with FedEx, UPS, USPS, and DHL free APIs.
Each carrier returns a normalized status dict.
"""
import logging
import time
import requests
from config import (
    FEDEX_API_KEY, FEDEX_SECRET_KEY,
    UPS_CLIENT_ID, UPS_CLIENT_SECRET,
    USPS_CLIENT_ID, USPS_CLIENT_SECRET,
    DHL_API_KEY, STATUS_MAP,
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
        self.token_expires = time.time() + data.get("expires_in", 3600) - 300
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
                    {
                        "trackingNumberInfo": {
                            "trackingNumber": tracking_number,
                        }
                    }
                ],
                "includeDetailedScans": False,
            }
            resp = requests.post(self.TRACK_URL, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Parse response
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

            # Map FedEx status codes
            status_map = {
                "DL": "delivered",
                "IT": "in_transit",
                "OD": "out_for_delivery",
                "DE": "exception",
                "PU": "in_transit",
                "PL": "label_created",
            }
            status = status_map.get(status_code, "in_transit")

            # Get delivery date
            delivery_date = ""
            dates = results.get("dateAndTimes", [])
            for d in dates:
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
        self.token_expires = time.time() + data.get("expires_in", 14400) - 300
        return self.token

    def track(self, tracking_number: str) -> dict:
        try:
            token = self._authenticate()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "transId": f"track-{tracking_number}",
                "transactionSrc": "lark-tracking-bot",
            }
            params = {
                "locale": "en_US",
                "returnSignature": "false",
            }
            url = f"{self.TRACK_URL}/{tracking_number}"
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Parse response
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

            # Map UPS status types
            status_map = {
                "D": "delivered",
                "I": "in_transit",
                "P": "in_transit",
                "M": "label_created",
                "X": "exception",
                "O": "out_for_delivery",
            }
            status = status_map.get(status_type, "in_transit")

            # Get delivery date
            delivery_date = ""
            del_date = package.get("deliveryDate", [{}])
            if del_date:
                d = del_date[0] if isinstance(del_date, list) else del_date
                date_str = d.get("date", "")
                if date_str and len(date_str) == 8:
                    delivery_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            # Also check activity date for delivered
            if status == "delivered" and not delivery_date:
                date_str = latest.get("date", "")
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
        self.token_expires = time.time() + data.get("expires_in", 3600) - 300
        return self.token

    def track(self, tracking_number: str) -> dict:
        try:
            token = self._authenticate()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            url = f"{self.TRACK_URL}/{tracking_number}"
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Parse response
            tracking_info = data.get("trackingNumber", tracking_number)
            status_category = data.get("statusCategory", "").upper()
            raw_status = data.get("status", "")

            location_parts = []
            if data.get("destinationCity"):
                location_parts.append(data["destinationCity"])
            if data.get("destinationState"):
                location_parts.append(data["destinationState"])
            location = ", ".join(location_parts)

            # Map USPS status categories
            status_map = {
                "DELIVERED": "delivered",
                "IN_TRANSIT": "in_transit",
                "OUT_FOR_DELIVERY": "out_for_delivery",
                "ALERT": "exception",
                "PRE_TRANSIT": "label_created",
                "ACCEPTED": "in_transit",
            }
            status = status_map.get(status_category, "in_transit")

            # Delivery date
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

            headers = {
                "DHL-API-Key": DHL_API_KEY,
                "Accept": "application/json",
            }
            params = {
                "trackingNumber": tracking_number,
            }
            resp = requests.get(self.TRACK_URL, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            shipments = data.get("shipments", [])
            if not shipments:
                return normalize_result("not_found")

            shipment = shipments[0]
            status_obj = shipment.get("status", {})
            status_code = status_obj.get("statusCode", "").lower()
            raw_status = status_obj.get("description", "")

            location_obj = status_obj.get("location", {}).get("address", {})
            location = location_obj.get("addressLocality", "")

            # Map DHL status codes
            status_map = {
                "delivered": "delivered",
                "transit": "in_transit",
                "failure": "exception",
                "pre-transit": "label_created",
                "unknown": "unknown",
            }
            status = status_map.get(status_code, "in_transit")

            # Delivery date
            delivery_date = ""
            estimated = shipment.get("estimatedTimeOfDelivery")
            if status == "delivered":
                ts = status_obj.get("timestamp", "")
                if ts:
                    delivery_date = ts[:10]
            elif estimated:
                delivery_date = estimated[:10] if isinstance(estimated, str) else ""

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
# Unified Tracker
# =============================================================================
class CarrierTracker:
    """Routes tracking requests to the correct carrier API."""

    def __init__(self):
        self.fedex = FedExTracker()
        self.ups = UPSTracker()
        self.usps = USPSTracker()
        self.dhl = DHLTracker()

        self._clients = {
            "fedex": self.fedex,
            "ups": self.ups,
            "usps": self.usps,
            "dhl": self.dhl,
        }

    def track(self, tracking_number: str, carrier: str) -> dict:
        """Track a shipment using the appropriate carrier API.
        
        Args:
            tracking_number: The tracking number
            carrier: Normalized carrier key (fedex, ups, usps, dhl)
            
        Returns:
            Normalized tracking result dict
        """
        client = self._clients.get(carrier)
        if not client:
            logger.warning(f"Unknown carrier '{carrier}' for tracking {tracking_number}")
            return normalize_result("unknown", error=f"Unsupported carrier: {carrier}")

        logger.info(f"Tracking {tracking_number} via {carrier.upper()}")
        return client.track(tracking_number)
