"""
Lark API Client
Handles authentication, reading/writing Lark Sheets, and sending group chat messages.
Uses JP-region endpoints for Lark Suite.
"""
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
        # Expire 5 min early to be safe
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
        """Get all sheet tabs (name, id) in a spreadsheet."""
        url = f"{self.base_url}/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Failed to get sheet metadata: {data}")

        sheets = data.get("data", {}).get("sheets", [])
        result = []
        for s in sheets:
            title = s.get("title", "")
            sheet_id = s.get("sheet_id", "")
            if title not in SKIP_TABS:
                result.append({"title": title, "sheet_id": sheet_id})

        logger.info(f"Found {len(result)} tabs in spreadsheet {spreadsheet_token}")
        return result

    def read_sheet_range(self, spreadsheet_token: str, sheet_id: str,
                         start_col: str, end_col: str, start_row: int, end_row: int) -> list:
        """Read a range of cells from a sheet tab.
        
        Returns list of rows, each row is a list of cell values.
        """
        range_str = f"{sheet_id}!{start_col}{start_row}:{end_col}{end_row}"
        url = (
            f"{self.base_url}/open-apis/sheets/v2/spreadsheets/"
            f"{spreadsheet_token}/values/{range_str}"
        )
        params = {
            "valueRenderOption": "ToString",
        }
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Failed to read range {range_str}: {data}")

        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        return values

    def read_tracking_data(self, spreadsheet_token: str, sheet_id: str) -> list:
        """Read all rows with tracking data from a sheet tab.
        
        Returns list of dicts with row_num, tracking_num, carrier, status, customer, recipient, etc.
        """
        # Read a generous range (row 3 to 500) — columns A through Q
        start_row = HEADER_ROW + 1  # Data starts after header
        rows = self.read_sheet_range(
            spreadsheet_token, sheet_id,
            start_col="A", end_col="Q",
            start_row=start_row, end_row=500,
        )

        # Column index mapping (0-based within our read range A-Q)
        col_idx = {
            "shipment_id": 0,   # A
            "vendor": 1,        # B
            "recipient": 2,     # C
            "order_num": 3,     # D
            "customer": 4,      # E
            "tracking_num": 6,  # G
            "carrier": 7,       # H
            "status": 12,       # M
            "delivery_date": 16, # Q (new column)
        }

        results = []
        for i, row in enumerate(rows):
            # Ensure row has enough columns
            while len(row) < 17:
                row.append(None)

            tracking = str(row[col_idx["tracking_num"]] or "").strip()
            if not tracking:
                continue

            results.append({
                "row_num": start_row + i,  # Actual row number in sheet
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

        logger.info(f"Found {len(results)} rows with tracking numbers in sheet {sheet_id}")
        return results

    def write_cells(self, spreadsheet_token: str, sheet_id: str, updates: list):
        """Write values to specific cells.
        
        updates: list of {"row": int, "col": str, "value": str}
        """
        if not updates:
            return

        # Lark Sheets API: batch update using valueRanges
        value_ranges = []
        for u in updates:
            range_str = f"{sheet_id}!{u['col']}{u['row']}"
            value_ranges.append({
                "range": range_str,
                "values": [[u["value"]]],
            })

        url = (
            f"{self.base_url}/open-apis/sheets/v2/spreadsheets/"
            f"{spreadsheet_token}/values_batch_update"
        )
        body = {
            "valueRanges": value_ranges,
        }
        resp = requests.post(url, headers=self._headers(), json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Failed to write cells: {data}")

        logger.info(f"Updated {len(updates)} cells in sheet {sheet_id}")

    def update_tracking_row(self, spreadsheet_token: str, sheet_id: str,
                            row_num: int, status: str, delivery_date: str = ""):
        """Update status and delivery date for a single row."""
        updates = [
            {"row": row_num, "col": COLUMNS["status"], "value": status},
        ]
        if delivery_date:
            updates.append(
                {"row": row_num, "col": COLUMNS["delivery_date"], "value": delivery_date}
            )
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
        resp = requests.post(url, headers=self._headers(), params=params, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Failed to send message: {data}")

        logger.info("Message sent to group chat")

    def _build_card_message(self, text_content: str) -> str:
        """Build a Lark interactive card message."""
        import json
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "📦 Shipment Tracking Update"
                },
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": text_content,
                }
            ],
        }
        return json.dumps(card)

    def send_daily_summary(self, all_results: list):
        """Send daily update grouped by carrier for all non-delivered shipments."""

        # Filter out delivered shipments
        active = [r for r in all_results if r.get("new_status", "").upper() != "DELIVERED"]

        if not active:
            self.send_group_message("✅ All shipments have been delivered. Nothing to track today.")
            return

        # Group by carrier
        by_carrier = {}
        for r in active:
            carrier = r.get("carrier", "Unknown").strip().upper()
            by_carrier.setdefault(carrier, []).append(r)

        lines = []
        lines.append(f"**📦 Daily Shipment Update** — {len(active)} active shipment(s)\n")

        for carrier in sorted(by_carrier.keys()):
            items = by_carrier[carrier]
            lines.append(f"\n**{carrier}** ({len(items)})")
            for r in items:
                tracking = r.get("tracking_num", "N/A")
                name = r.get("recipient") or r.get("customer") or "Unknown"
                status = r.get("new_status", "UNKNOWN")
                delivery = r.get("delivery_date", "")

                # Build status/date info
                if delivery:
                    status_info = f"{status} — Est. {delivery}"
                else:
                    status_info = status

                lines.append(f"  • {tracking} | {name} | {status_info}")

        message = "\n".join(lines)
        self.send_group_message(message)
