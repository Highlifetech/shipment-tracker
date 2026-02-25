"""
Lark API Client
Handles authentication, reading/writing Lark Sheets, and sending group chat messages.
Uses JP-region endpoints for Lark Suite.
"""
import json
import logging
import time
import requests
from config import (
    LARK_APP_ID, LARK_APP_SECRET, LARK_BASE_URL,
    LARK_CHAT_ID, COLUMNS, HEADER_ROW, SKIP_TABS,
)

logger = logging.getLogger(__name__)


class LarkClient:
    """Client for Lark Suite API (Sheets + Messaging)."""

    def __init__(self):
        self.base_url = LARK_BASE_URL.rstrip("/")
        self.token = None
        self.token_expires = 0

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------

    def _get_tenant_token(self):
        """Get or refresh tenant access token."""
        if self.token and time.time() < self.token_expires:
            return self.token
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={
            "app_id": LARK_APP_ID,
            "app_secret": LARK_APP_SECRET,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Lark auth failed: {data}")
        self.token = data["tenant_access_token"]
        self.token_expires = time.time() + data.get("expire", 7200) - 300
        logger.info("Lark tenant token acquired")
        return self.token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._get_tenant_token()}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------------------------
    # Sheet Operations
    # -------------------------------------------------------------------------

    def get_sheet_metadata(self, spreadsheet_token: str) -> list:
        """Get all sheet tabs (name, id) in a spreadsheet.
        Tries v3 first, falls back to v2 if that fails.
        """
        url_v3 = f"{self.base_url}/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
        resp = requests.get(url_v3, headers=self._headers(), timeout=30)
        if resp.ok:
            data = resp.json()
            if data.get("code") == 0:
                sheets = data.get("data", {}).get("sheets", [])
                return self._parse_sheets(sheets, spreadsheet_token)
            else:
                logger.error(f"v3 code={data.get('code')} msg={data.get('msg')} token={spreadsheet_token}")
        else:
            logger.error(f"v3 HTTP {resp.status_code} token={spreadsheet_token} body={resp.text[:200]}")

        url_v2 = f"{self.base_url}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo"
        resp2 = requests.get(url_v2, headers=self._headers(), timeout=30)
        if resp2.ok:
            data2 = resp2.json()
            if data2.get("code") == 0:
                sheets_raw = data2.get("data", {}).get("sheets", [])
                sheets = [{"title": s.get("title", ""), "sheet_id": s.get("sheetId", "")} for s in sheets_raw]
                return self._parse_sheets(sheets, spreadsheet_token)
            else:
                logger.error(f"v2 code={data2.get('code')} msg={data2.get('msg')} token={spreadsheet_token}")
                raise Exception(f"Cannot read spreadsheet {spreadsheet_token}: code={data2.get('code')} msg={data2.get('msg')}")
        else:
            logger.error(f"v2 HTTP {resp2.status_code} token={spreadsheet_token} body={resp2.text[:200]}")
            raise Exception(f"Cannot read spreadsheet {spreadsheet_token}: HTTP {resp2.status_code}")

    def _parse_sheets(self, sheets: list, spreadsheet_token: str) -> list:
        result = []
        for s in sheets:
            title = s.get("title", "")
            sheet_id = s.get("sheet_id", "")
            if title not in SKIP_TABS:
                result.append({"title": title, "sheet_id": sheet_id})
        logger.info(f"Found {len(result)} tabs in {spreadsheet_token}")
        return result

    def read_sheet_range(self, spreadsheet_token: str, sheet_id: str,
                         start_col: str, end_col: str,
                         start_row: int, end_row: int) -> list:
        """Read a range of cells from a sheet tab."""
        range_str = f"{sheet_id}!{start_col}{start_row}:{end_col}{end_row}"
        url = (
            f"{self.base_url}/open-apis/sheets/v2/spreadsheets/"
            f"{spreadsheet_token}/values/{range_str}"
        )
        resp = requests.get(url, headers=self._headers(),
                            params={"valueRenderOption": "ToString"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Failed to read range {range_str}: {data}")
        return data.get("data", {}).get("valueRange", {}).get("values", [])

    def read_tracking_data(self, spreadsheet_token: str, sheet_id: str) -> list:
        """Read all rows with tracking data from a sheet tab."""
        start_row = HEADER_ROW + 1
        rows = self.read_sheet_range(
            spreadsheet_token, sheet_id,
            start_col="A", end_col="Q",
            start_row=start_row, end_row=500,
        )
        col_idx = {
            "shipment_id": 0,    # A
            "vendor": 1,         # B
            "recipient": 2,      # C
            "order_num": 3,      # D
            "customer": 4,       # E
            "tracking_num": 6,   # G
            "carrier": 7,        # H
            "status": 12,        # M
            "delivery_date": 16, # Q
        }
        results = []
        for i, row in enumerate(rows):
            while len(row) < 17:
                row.append(None)
            tracking = str(row[col_idx["tracking_num"]] or "").strip()
            if not tracking:
                continue
            results.append({
                "row_num": start_row + i,
                "shipment_id": str(row[col_idx["shipment_id"]] or "").strip(),
                "vendor": str(row[col_idx["vendor"]] or "").strip(),
                "recipient": str(row[col_idx["recipient"]] or "").strip(),
                "customer": str(row[col_idx["customer"]] or "").strip(),
                "order_num": str(row[col_idx["order_num"]] or "").strip(),
                "tracking_num": tracking,
                "carrier": str(row[col_idx["carrier"]] or "").strip(),
                "current_status": str(row[col_idx["status"]] or "").strip(),
                "delivery_date": str(row[col_idx["delivery_date"]] or "").strip(),
            })
        logger.info(f"Found {len(results)} rows with tracking in sheet {sheet_id}")
        return results

    def write_cells(self, spreadsheet_token: str, sheet_id: str, updates: list):
        """Write values to specific cells."""
        if not updates:
            return
        value_ranges = []
        for u in updates:
            range_str = f"{sheet_id}!{u['col']}{u['row']}:{u['col']}{u['row']}"
            value_ranges.append({"range": range_str, "values": [[u["value"]]]})
        url = (
            f"{self.base_url}/open-apis/sheets/v2/spreadsheets/"
            f"{spreadsheet_token}/values_batch_update"
        )
        resp = requests.post(url, headers=self._headers(),
                             json={"valueRanges": value_ranges}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Failed to write cells: {data}")
        logger.info(f"Updated {len(updates)} cells in sheet {sheet_id}")

    def update_tracking_row(self, spreadsheet_token: str, sheet_id: str,
                             row_num: int, status: str, delivery_date: str = ""):
        """Update status and delivery date for a single row."""
        updates = [{"row": row_num, "col": COLUMNS["status"], "value": status}]
        if delivery_date:
            updates.append({"row": row_num, "col": COLUMNS["delivery_date"], "value": delivery_date})
        self.write_cells(spreadsheet_token, sheet_id, updates)

    # -------------------------------------------------------------------------
    # Messaging
    # -------------------------------------------------------------------------

    def send_group_message(self, message: str, chat_id: str = None):
        """Send a text message to a Lark group chat."""
        target_chat = chat_id or LARK_CHAT_ID
        if not target_chat:
            logger.warning("No chat_id configured, skipping message")
            return
        url = f"{self.base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        body = {
            "receive_id": target_chat,
            "msg_type": "interactive",
            "content": self._build_card_message(message),
        }
        resp = requests.post(url, headers=self._headers(),
                             params=params, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Failed to send message: {data}")
        logger.info("Message sent to group chat")

    def _build_card_message(self, text_content: str) -> str:
        """Build a Lark interactive card message."""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📦 HLT Shipment Update"},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": text_content}],
        }
        return json.dumps(card)

    def send_daily_summary(self, all_results: list, sheet_name: str = "HLT"):
        """Send daily update grouped by carrier, matching the HLT format.

        Format:
            HLT

            FEDEX
            888598301681 (Lydon Borg Bonaci) - estimate 2/25 in CZ

            ROYALMAIL - CALJEWELRY
            RY480249909GB (Hannah) - needs to return the stuff
        """
        # Filter out delivered shipments
        active = [r for r in all_results if r.get("new_status", "").upper() != "DELIVERED"]

        if not active:
            self.send_group_message("✅ All shipments delivered. Nothing to track today.")
            return

        # Group by carrier, then by vendor within carrier
        # Key: (carrier_display, vendor) -> list of rows
        by_carrier_vendor = {}
        for r in active:
            carrier = r.get("carrier", "").strip().upper()
            if not carrier:
                carrier = "UNKNOWN"
            vendor = r.get("vendor", "").strip().upper()

            # Build header: "CARRIER - VENDOR" if vendor present, else just "CARRIER"
            if vendor:
                header = f"{carrier} - {vendor}"
            else:
                header = carrier

            by_carrier_vendor.setdefault(header, []).append(r)

        lines = [f"**HLT**\n"]

        for header in sorted(by_carrier_vendor.keys()):
            items = by_carrier_vendor[header]
            lines.append(f"\n**{header}**")
            for r in items:
                tracking = r.get("tracking_num", "N/A")
                name = r.get("recipient") or r.get("customer") or "Unknown"
                status = r.get("new_status", "")
                delivery = r.get("delivery_date", "")
                raw = r.get("raw_status", "")

                # Build a human-readable status description
                if status.upper() == "IN TRANSIT" and delivery:
                    desc = f"estimate {delivery}"
                elif status.upper() == "OUT FOR DELIVERY":
                    desc = "out for delivery"
                elif status.upper() == "LABEL CREATED":
                    desc = "waiting to ship"
                elif status.upper() == "DELIVERED":
                    desc = f"delivered {delivery}" if delivery else "delivered"
                elif status.upper() == "EXCEPTION":
                    desc = f"exception - {raw}" if raw else "exception"
                elif status.upper() == "NOT FOUND":
                    desc = "not found / pre-shipment"
                elif raw:
                    desc = raw.lower()
                else:
                    desc = status.lower() if status else "pending"

                lines.append(f"{tracking} ({name}) - {desc}")

        message = "\n".join(lines)
        self.send_group_message(message)
