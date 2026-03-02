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

PERMANENT_TABS = ["Hannah", "Lucy", "Other"]


class LarkClient:
    """Client for Lark Suite API (Sheets + Messaging)."""

    def __init__(self):
        self.base_url = LARK_BASE_URL.rstrip("/")
        self.token = None
        self.token_expires = 0

    def _get_tenant_token(self):
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

    def get_sheet_metadata(self, spreadsheet_token):
        url_v3 = f"{self.base_url}/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
        resp = requests.get(url_v3, headers=self._headers(), timeout=30)
        if resp.ok:
            data = resp.json()
            if data.get("code") == 0:
                return self._parse_sheets(data.get("data", {}).get("sheets", []), spreadsheet_token)
            logger.error("v3 code=%s msg=%s token=%s", data.get("code"), data.get("msg"), spreadsheet_token)
        else:
            logger.error("v3 HTTP %s token=%s body=%s", resp.status_code, spreadsheet_token, resp.text[:200])

        url_v2 = f"{self.base_url}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo"
        resp2 = requests.get(url_v2, headers=self._headers(), timeout=30)
        if resp2.ok:
            data2 = resp2.json()
            if data2.get("code") == 0:
                sheets_raw = data2.get("data", {}).get("sheets", [])
                sheets = [{"title": s.get("title", ""), "sheet_id": s.get("sheetId", "")}
                          for s in sheets_raw]
                return self._parse_sheets(sheets, spreadsheet_token)
            raise Exception(
                f"Cannot read spreadsheet {spreadsheet_token}: "
                f"code={data2.get('code')} msg={data2.get('msg')}"
            )
        raise Exception(f"Cannot read spreadsheet {spreadsheet_token}: HTTP {resp2.status_code}")

    def _parse_sheets(self, sheets, spreadsheet_token):
        result = []
        for s in sheets:
            title = s.get("title", "")
            sheet_id = s.get("sheet_id", "")
            if title not in SKIP_TABS:
                result.append({"title": title, "sheet_id": sheet_id})
        logger.info("Found %d processable tabs in %s", len(result), spreadsheet_token)
        return result

    def read_sheet_range(self, spreadsheet_token, sheet_id, start_col, end_col, start_row, end_row):
        range_str = f"{sheet_id}!{start_col}{start_row}:{end_col}{end_row}"
        url = f"{self.base_url}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}"
        resp = requests.get(url, headers=self._headers(),
                            params={"valueRenderOption": "ToString"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Failed to read range {range_str}: {data}")
        rows = data.get("data", {}).get("valueRange", {}).get("values", [])
        logger.info("Read %d raw rows from %s", len(rows), range_str)
        return rows

    def read_tracking_data(self, spreadsheet_token, sheet_id):
        """Read all rows with tracking data. Column layout (0-indexed, A-Q):
        A=0 shipment_id, B=1 vendor, C=2 recipient, D=3 order_num, E=4 customer,
        F=5 photo, G=6 tracking_num, H=7 carrier, M=12 status, Q=16 delivery_date
        """
        start_row = HEADER_ROW + 1
        rows = self.read_sheet_range(
            spreadsheet_token, sheet_id,
            start_col="A", end_col="Q",
            start_row=start_row, end_row=500,
        )
        MIN_COLS = 17
        results = []
        for i, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            while len(row) < MIN_COLS:
                row.append("")

            tracking = str(row[6] or "").strip()
            if not tracking:
                continue

            carrier_raw = str(row[7] or "").strip()
            status_raw = str(row[12] or "").strip()
            delivery_raw = str(row[16] or "").strip()

            if not carrier_raw:
                logger.warning("  Row %d: tracking=%s but carrier is empty - skipping",
                               start_row + i, tracking)
                continue

            results.append({
                "row_num": start_row + i,
                "shipment_id": str(row[0] or "").strip(),
                "vendor": str(row[1] or "").strip(),
                "recipient": str(row[2] or "").strip(),
                "customer": str(row[4] or "").strip(),
                "order_num": str(row[3] or "").strip(),
                "tracking_num": tracking,
                "carrier": carrier_raw,
                "current_status": status_raw,
                "delivery_date": delivery_raw,
            })

        logger.info("  %d rows with tracking in sheet %s", len(results), sheet_id)
        return results

    def write_cells(self, spreadsheet_token, sheet_id, updates):
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
        logger.info("Updated %d cells in sheet %s", len(updates), sheet_id)

    def update_tracking_row(self, spreadsheet_token, sheet_id, row_num, status, delivery_date=""):
        updates = [{"row": row_num, "col": COLUMNS["status"], "value": status}]
        if delivery_date:
            updates.append({"row": row_num, "col": COLUMNS["delivery_date"], "value": delivery_date})
        self.write_cells(spreadsheet_token, sheet_id, updates)

    def send_group_message(self, message, chat_id=None, message_id=None):
        """Send message to Lark group. Falls back to plain text if card fails."""
        target_chat = chat_id or LARK_CHAT_ID
        if not target_chat:
            logger.warning("No chat_id configured, skipping message")
            return
        try:
            self._send_card(message, target_chat, message_id)
            return
        except Exception as e:
            logger.warning("Interactive card failed (%s), retrying as plain text", e)
        try:
            self._send_text(message, target_chat, message_id)
        except Exception as e:
            logger.error("Plain text message also failed: %s", e)
            raise

    def _send_card(self, message, chat_id, message_id=None):
        url = f"{self.base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": self._build_card_message(message),
        }
        if message_id:
            url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}/reply"
            params = {}
            body = {"msg_type": "interactive", "content": self._build_card_message(message)}
        resp = requests.post(url, headers=self._headers(), params=params, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Card send failed: code={data.get('code')} msg={data.get('msg')}")
        logger.info("Interactive card sent to group chat")

    def _send_text(self, message, chat_id, message_id=None):
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
            body = {"msg_type": "text", "content": json.dumps({"text": message})}
        resp = requests.post(url, headers=self._headers(), params=params, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Text send failed: code={data.get('code')} msg={data.get('msg')}")
        logger.info("Plain text message sent to group chat")

    def _build_card_message(self, text_content):
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "HLT Shipment Update"},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": text_content}],
        }
        return json.dumps(card)

    @staticmethod
    def _format_delivery_date(raw_date):
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
    def _section_for(r):
        token = r.get("sheet_token", "").strip()
        return SHEET_OWNERS.get(token, "Other")

    @staticmethod
    def _shipment_line(r):
        tracking = r.get("tracking_num", "N/A")
        recipient = r.get("recipient", "").strip()
        customer = r.get("customer", "").strip()

        if recipient.upper() == "BRENDAN":
            name = "Brendan"
        elif recipient.upper() == "CUSTOMER DIRECT":
            name = customer or "Unknown"
        else:
            name = recipient or customer or "Unknown"

        delivery = r.get("delivery_date", "").strip()
        status = r.get("new_status", "").upper()
        raw = r.get("raw_status", "").strip()

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

    def send_daily_summary(self, all_results, chat_id=None, message_id=None):
        """Send the shipment summary. Works for scheduled runs and @mention replies."""
        active = [r for r in all_results if r.get("new_status", "").upper() != "DELIVERED"]
        if not active:
            self.send_group_message(
                "All shipments delivered. Nothing to track.",
                chat_id=chat_id, message_id=message_id,
            )
            return

        seen, unique = set(), []
        for r in active:
            tn = r.get("tracking_num", "").strip()
            if tn and tn not in seen:
                seen.add(tn)
                unique.append(r)

        buckets = {tab: [] for tab in PERMANENT_TABS}
        for r in unique:
            section = self._section_for(r)
            buckets[section].append(r)

        lines = ["**HLT Shipment Tracker**"]

        def render_section(label, items):
            lines.append(f"\n**-- {label} --**")
            if not items:
                lines.append("No active shipments")
                return
            by_carrier = {}
            for r in items:
                c = r.get("carrier", "").strip().upper() or "UNKNOWN"
                by_carrier.setdefault(c, []).append(r)
            for carrier in sorted(by_carrier):
                lines.append(f"\n*{carrier}*")
                for r in by_carrier[carrier]:
                    lines.append(LarkClient._shipment_line(r))

        for tab_name in PERMANENT_TABS:
            render_section(tab_name, buckets[tab_name])

        self.send_group_message(
            "\n".join(lines),
            chat_id=chat_id,
            message_id=message_id,
        )
