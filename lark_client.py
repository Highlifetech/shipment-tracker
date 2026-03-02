"""
Lark API Client
Handles authentication, reading/writing Lark Sheets, and sending group chat messages.
Supports both scheduled runs and @mention triggers from Lark chat.
"""
import json
import logging
from datetime import datetime
import time
import requests
from config import (
    LARK_APP_ID,
    LARK_APP_SECRET,
    LARK_BASE_URL,
    LARK_CHAT_ID,
    COLUMNS,
    HEADER_ROW,
    SKIP_TABS,
    SHEET_OWNERS,
)

logger = logging.getLogger(__name__)

# The three named tabs — these are the only sections shown in the bot message
PERMANENT_TABS = ["Hannah", "Lucy", "Other"]


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
                        Tries v3 first, falls back to v2.
                                """
        url_v3 = f"{self.base_url}/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
        resp = requests.get(url_v3, headers=self._headers(), timeout=30)
        if resp.ok:
                        data = resp.json()
                        if data.get("code") == 0:
                                            return self._parse_sheets(data.get("data", {}).get("sheets", []), spreadsheet_token)
                                        logger.error(f"v3 code={data.get('code')} msg={data.get('msg')} token={spreadsheet_token}")
else:
            logger.error(f"v3 HTTP {resp.status_code} token={spreadsheet_token} body={resp.text[:200]}")

        url_v2 = f"{self.base_url}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo"
        resp2 = requests.get(url_v2, headers=self._headers(), timeout=30)
        if resp2.ok:
                        data2 = resp2.json()
            if data2.get("code") == 0:
                                sheets_raw = data2.get("data", {}).get("sheets", [])
                                sheets = [{"title": s.get("title", ""), "sheet_id": s.get("sheetId", "")}
                                          for s in sheets_raw]
                                return self._parse_sheets(sheets, spreadsheet_token)
                            raise Exception(f"Cannot read spreadsheet {spreadsheet_token}: "
                                                                        f"code={data2.get('code')} msg={data2.get('msg')}")
        raise Exception(f"Cannot read spreadsheet {spreadsheet_token}: HTTP {resp2.status_code}")

    def _parse_sheets(self, sheets: list, spreadsheet_token: str) -> list:
                result = []
        for s in sheets:
                        title = s.get("title", "")
            sheet_id = s.get("sheet_id", "")
            if title not in SKIP_TABS:
                                result.append({"title": title, "sheet_id": sheet_id})
                        logger.info(f"Found {len(result)} processable tabs in {spreadsheet_token}")
        return result

    def read_sheet_range(self, spreadsheet_token: str, sheet_id: str,
                                                  start_col: str, end_col: str,
                                                  start_row: int, end_row: int) -> list:
                                                              """Read a range of cells from a sheet tab."""
                                                              range_str = f"{sheet_id}!{start_col}{start_row}:{end_col}{end_row}"
                                                              url = (f"{self.base_url}/open-apis/sheets/v2/spreadsheets/"
                                                                     f"{spreadsheet_token}/values/{range_str}")
                                                              resp = requests.get(url, headers=self._headers(),
                                                                                  params={"valueRenderOption": "ToString"},
                                                                                  timeout=30)
                                                              resp.raise_for_status()
                                                              data = resp.json()
                                                              if data.get("code") != 0:
                                                                              raise Exception(f"Failed to read range {range_str}: {data}")
                                                                          rows = data.get("data", {}).get("valueRange", {}).get("values", [])
        logger.info(f"Read {len(rows)} raw rows from {range_str}")
        return rows

    def read_tracking_data(self, spreadsheet_token: str, sheet_id: str) -> list:
                """Read all rows with tracking data from a sheet tab.

                        Column layout (0-indexed after reading A:Q):
                                  A=0  shipment_id
                                            B=1  vendor
                                                      C=2  recipient
                                                                D=3  order_num
                                                                          E=4  customer
                                                                                    F=5  product_photo
                                                                                              G=6  tracking_num
                                                                                                        H=7  carrier
                                                                                                                  M=12 status
                                                                                                                            Q=16 delivery_date
                                                                                                                                    """
        start_row = HEADER_ROW + 1  # data starts row after header
        rows = self.read_sheet_range(
                        spreadsheet_token, sheet_id,
                        start_col="A", end_col="Q",
                        start_row=start_row, end_row=500,
        )

        # Lark Sheets API sometimes omits trailing empty cells — pad every row
        MIN_COLS = 17  # columns A-Q

        results = []
        for i, row in enumerate(rows):
                        # Ensure the row list is at least MIN_COLS wide
                        if not isinstance(row, list):
                                            continue
            while len(row) < MIN_COLS:
                                row.append("")

            tracking = str(row[6] or "").strip()   # column G
            if not tracking:
                                continue

            carrier_raw = str(row[7] or "").strip()    # column H
            status_raw  = str(row[12] or "").strip()   # column M
            delivery_raw = str(row[16] or "").strip()  # column Q

            if not carrier_raw:
                                logger.warning(f"  Row {start_row + i}: tracking={tracking} but carrier is empty — skipping")
                continue

            results.append({
                                "row_num":        start_row + i,
                                "shipment_id":    str(row[0] or "").strip(),
                                "vendor":         str(row[1] or "").strip(),
                                "recipient":      str(row[2] or "").strip(),
                                "customer":       str(row[4] or "").strip(),
                                "order_num":      str(row[3] or "").strip(),
                                "tracking_num":   tracking,
                                "carrier":        carrier_raw,
                                "current_status": status_raw,
                                "delivery_date":  delivery_raw,
            })

        logger.info(f"  {len(results)} rows with tracking in sheet {sheet_id}")
        return results

    def write_cells(self, spreadsheet_token: str, sheet_id: str, updates: list):
                """Write values to specific cells."""
        if not updates:
                        return
        value_ranges = []
        for u in updates:
                        range_str = f"{sheet_id}!{u['col']}{u['row']}:{u['col']}{u['row']}"
            value_ranges.append({"range": range_str, "values": [[u["value"]]]})
        url = (f"{self.base_url}/open-apis/sheets/v2/spreadsheets/"
                              f"{spreadsheet_token}/values_batch_update")
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
    def send_group_message(self, message: str, chat_id: str = None,
                                                      message_id: str = None):
                                                                  """Send a message to a Lark group chat.

                                                                          If message_id is provided the reply is threaded under that message
                                                                                  (used when responding to an @mention).  Falls back to plain text if
                                                                                          the interactive card is rejected.
                                                                                                  """
        target_chat = chat_id or LARK_CHAT_ID
        if not target_chat:
                        logger.warning("No chat_id configured, skipping message")
            return

        # ---- Try interactive card first ----
        try:
                        self._send_card(message, target_chat, message_id)
            return
except Exception as e:
            logger.warning(f"Interactive card failed ({e}), retrying as plain text")

        # ---- Fallback: plain text ----
        try:
                        self._send_text(message, target_chat, message_id)
except Exception as e:
            logger.error(f"Plain text message also failed: {e}")
            raise

    def _send_card(self, message: str, chat_id: str, message_id: str = None):
                """Send as an interactive card message."""
        url = f"{self.base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        body = {
                        "receive_id": chat_id,
                        "msg_type": "interactive",
                        "content": self._build_card_message(message),
        }
        if message_id:
                        # Reply in the same thread
                        url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}/reply"
            params = {}
            body = {
                                "msg_type": "interactive",
                                "content": self._build_card_message(message),
            }
        resp = requests.post(url, headers=self._headers(), params=params, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
                        raise Exception(f"Card send failed: code={data.get('code')} msg={data.get('msg')}")
        logger.info("Interactive card sent to group chat")

    def _send_text(self, message: str, chat_id: str, message_id: str = None):
                """Send as a plain text message."""
        url = f"{self.base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        body = {
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": message}),
        }
        if message_id:
                        url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}/reply"
            params = {}
            body = {
                                "msg_type": "text",
                                "content": json.dumps({"text": message}),
            }
        resp = requests.post(url, headers=self._headers(), params=params, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
                        raise Exception(f"Text send failed: code={data.get('code')} msg={data.get('msg')}")
        logger.info("Plain text message sent to group chat")

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

    @staticmethod
    def _format_delivery_date(raw_date: str) -> str:
                """Convert '2026-02-25' → 'expected delivery on Tuesday, February 25, 2026'."""
        if not raw_date:
                        return ""
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
                        try:
                                            dt = datetime.strptime(raw_date.strip()[:10], fmt)
                                            return "expected delivery on " + dt.strftime("%A, %B %d, %Y").replace(" 0", " ")
except (ValueError, TypeError):
                    continue
            return raw_date

    @staticmethod
    def _section_for(r: dict) -> str:
                """Determine which named section (Hannah / Lucy / Other) a shipment belongs to.
                        Uses SHEET_OWNERS to map the sheet token to a section name.
                                Falls back to 'Other'.
                                        """
                token = r.get("sheet_token", "").strip()
                return SHEET_OWNERS.get(token, "Other")

    @staticmethod
    def _shipment_line(r: dict) -> str:
                """Format one shipment as: tracking_num -- display_name -- status/date"""
                tracking = r.get("tracking_num", "N/A")
                recipient = r.get("recipient", "").strip()
                customer  = r.get("customer", "").strip()

        if recipient.upper() == "BRENDAN":
                        name = "Brendan"
elif recipient.upper() == "CUSTOMER DIRECT":
                name = customer or "Unknown"
else:
                name = recipient or customer or "Unknown"

        delivery = r.get("delivery_date", "").strip()
        status   = r.get("new_status", "").upper()
        raw      = r.get("raw_status", "").strip()

        if status == "OUT FOR DELIVERY":
                        date_str = "out for delivery today"
elif status == "LABEL CREATED":
                date_str = "waiting to ship"
elif status == "EXCEPTION":
                date_str = f"exception - {raw}" if raw else "exception"
elif status in ("UNKNOWN", "NOT FOUND", "PENDING", ""):
                date_str = "pending"
elif delivery:
                date_str = LarkClient._format_delivery_date(delivery)
else:
                date_str = "in transit"

        return f"{tracking} -- {name} -- {date_str}"

    def send_daily_summary(self, all_results: list,
                                                      chat_id: str = None, message_id: str = None):
                                                                  """Send the shipment summary card to the Lark group chat.

                                                                          Works for both scheduled runs (message_id=None) and @mention replies
                                                                                  (pass the triggering message_id so the reply is threaded).

                                                                                          The message always has exactly three sections:
                                                                                                    **— Hannah —**
                                                                                                              **— Lucy —**
                                                                                                                        **— Other —**
                                                                                                                        
                                                                                                                                Delivered shipments are excluded.  Duplicates are deduped by tracking number.
                                                                                                                                        """
                                                                  active = [r for r in all_results if r.get("new_status", "").upper() != "DELIVERED"]
                                                                  if not active:
                                                                                  self.send_group_message(
                                                                                                      "✅ All shipments delivered. Nothing to track.",
                                                                                                      chat_id=chat_id, message_id=message_id,
                                                                                  )
                                                                                  return

                                                                  # Deduplicate by tracking number (keep first occurrence)
                                                                  seen, unique = set(), []
                                                                  for r in active:
                                                                                  tn = r.get("tracking_num", "").strip()
                                                                                  if tn and tn not in seen:
                                                                                                      seen.add(tn)
                                                                                                      unique.append(r)

                                                                              # Route every shipment into Hannah / Lucy / Other buckets
                                                                              buckets = {tab: [] for tab in PERMANENT_TABS}
        for r in unique:
                        section = self._section_for(r)
            buckets[section].append(r)

        lines = ["**HLT Shipment Tracker**"]

        def render_section(label: str, items: list):
                        lines.append(f"\n**— {label} —**")
            if not items:
                                lines.append("No active shipments")
                return
            by_carrier: dict = {}
            for r in items:
                                c = r.get("carrier", "").strip().upper() or "UNKNOWN"
                by_carrier.setdefault(c, []).append(r)
            for carrier in sorted(by_carrier):
                                lines.append(f"\n*{carrier}*")
                for r in by_carrier[carrier]:
                                        lines.append(self._shipment_line(r))

        for tab_name in PERMANENT_TABS:
                        render_section(tab_name, buckets[tab_name])

        self.send_group_message(
                        "\n".join(lines),
                        chat_id=chat_id,
                        message_id=message_id,
        )
