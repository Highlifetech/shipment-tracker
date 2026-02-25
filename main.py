"""
Lark Shipment Tracking Bot — Main Entry Point

Reads tracking numbers from Lark Sheets (current month tab only),
checks status via carrier APIs, updates the sheet only on real status changes,
and sends a daily summary to a Lark group chat.

Usage:
    python main.py           # Run once
    python main.py --dry-run # Run without writing to sheets or sending messages
"""
import sys
import logging
import time
from datetime import datetime
from config import SHEET_TOKENS, CARRIER_ALIASES
from lark_client import LarkClient
from carriers import CarrierTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Months tabs as they appear in the sheet
MONTH_TABS = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

# These statuses mean the carrier API gave us nothing real — don't write to sheet
BAD_STATUSES = {"UNKNOWN", "NOT FOUND", ""}

# These statuses mean the shipment is done — skip re-checking
DONE_STATUSES = {"DELIVERED"}


def normalize_carrier(carrier_str: str) -> str:
    """Convert carrier name from sheet (e.g. 'Fedex') to API client key (e.g. 'fedex')."""
    return CARRIER_ALIASES.get(carrier_str.lower().strip(), carrier_str.lower().strip())


def current_month_tab() -> str:
    """Return the tab name for the current month, e.g. 'FEB'."""
    return MONTH_TABS[datetime.utcnow().month]


def process_sheet(lark: LarkClient, tracker: CarrierTracker,
                  spreadsheet_token: str, dry_run: bool = False) -> list:
    """Process the current month's tab in a spreadsheet.
    Returns list of result dicts for the daily summary.
    """
    all_results = []

    # Get all tabs
    try:
        tabs = lark.get_sheet_metadata(spreadsheet_token)
    except Exception as e:
        logger.error(f"Failed to read spreadsheet {spreadsheet_token}: {e}")
        return all_results

    # Only process the current month tab
    target_tab = current_month_tab()
    current_tabs = [t for t in tabs if t["title"].upper() == target_tab]

    if not current_tabs:
        logger.warning(f"Tab '{target_tab}' not found in spreadsheet {spreadsheet_token}")
        logger.info(f"Available tabs: {[t['title'] for t in tabs]}")
        return all_results

    tab = current_tabs[0]
    tab_title = tab["title"]
    sheet_id = tab["sheet_id"]
    logger.info(f"Processing tab: {tab_title} ({sheet_id})")

    try:
        rows = lark.read_tracking_data(spreadsheet_token, sheet_id)
    except Exception as e:
        logger.error(f"Failed to read tab {tab_title}: {e}")
        return all_results

    logger.info(f"Found {len(rows)} rows with tracking numbers")

    for row in rows:
        tracking_num = row["tracking_num"]
        carrier_raw = row["carrier"]
        current_status = row.get("current_status", "").strip().upper()

        # Skip already-delivered shipments
        if current_status in DONE_STATUSES:
            logger.info(f"Skipping {tracking_num} — already DELIVERED")
            continue

        carrier = normalize_carrier(carrier_raw)
        if not carrier or carrier not in CARRIER_ALIASES.values():
            logger.warning(f"Row {row['row_num']}: Unknown carrier '{carrier_raw}', including with current status")
            all_results.append({
                **row,
                "new_status": current_status or "UNKNOWN CARRIER",
                "tab": tab_title,
                "sheet_token": spreadsheet_token,
            })
            continue

        # Call carrier API
        result = tracker.track(tracking_num, carrier)
        new_status = result["status"]          # e.g. "IN TRANSIT"
        delivery_date = result.get("delivery_date", "")
        raw_status = result.get("raw_status", "")
        api_error = result.get("error", "")

        # Determine what to write to sheet and show in message
        if api_error or new_status.upper() in BAD_STATUSES:
            # API failed — keep existing sheet status, show it in message
            display_status = current_status if current_status else "PENDING"
            logger.warning(f"{tracking_num}: API error ({api_error[:60]}), keeping '{display_status}'")
            all_results.append({
                **row,
                "new_status": display_status,
                "delivery_date": row.get("delivery_date", ""),
                "raw_status": raw_status,
                "tab": tab_title,
                "sheet_token": spreadsheet_token,
            })
        else:
            # Good status — write to sheet if it changed
            if not dry_run and new_status.upper() != current_status:
                try:
                    lark.update_tracking_row(
                        spreadsheet_token, sheet_id,
                        row["row_num"], new_status, delivery_date,
                    )
                    logger.info(f"Updated {tracking_num}: {current_status} → {new_status}")
                except Exception as e:
                    logger.error(f"Failed to write row {row['row_num']}: {e}")

            all_results.append({
                **row,
                "new_status": new_status,
                "delivery_date": delivery_date,
                "raw_status": raw_status,
                "tab": tab_title,
                "sheet_token": spreadsheet_token,
            })

        time.sleep(0.5)

    return all_results


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("=== DRY RUN MODE — no writes or messages ===")

    if not SHEET_TOKENS:
        logger.error("No sheet tokens configured. Set LARK_SHEET_TOKENS env var.")
        sys.exit(1)

    logger.info(f"Target month tab: {current_month_tab()}")

    lark = LarkClient()
    tracker = CarrierTracker()
    all_results = []

    for token in SHEET_TOKENS:
        logger.info(f"Processing spreadsheet: {token}")
        results = process_sheet(lark, tracker, token, dry_run)
        all_results.extend(results)
        logger.info(f"Got {len(results)} active shipments from {token}")

    logger.info(f"Total active shipments: {len(all_results)}")

    if not dry_run:
        try:
            lark.send_daily_summary(all_results)
            logger.info("Daily summary sent to group chat")
        except Exception as e:
            logger.error(f"Failed to send daily summary: {e}")
    else:
        logger.info("Dry run complete. Results:")
        for r in all_results:
            logger.info(
                f"  [{r.get('tab')}] {r['tracking_num']} | {r['carrier']} | "
                f"{r['new_status']} | {r.get('delivery_date', '')} | "
                f"{r.get('customer', '')}"
            )

    logger.info("Done!")


if __name__ == "__main__":
    main()
