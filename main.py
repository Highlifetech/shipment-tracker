"""
Lark Shipment Tracking Bot — Main Entry Point

Scans the following tabs in each spreadsheet:
  - Hannah, Lucy, Other  — permanent named tabs, always scanned
  - Current month tab    — e.g. FEB
  - Previous month tab   — e.g. JAN  (catches end-of-month layover)

DELIVERED rows are skipped individually, so layover shipments from the
previous month that are still in transit will still appear.

Usage:
    python main.py            # Run once
    python main.py --dry-run  # Run without writing to sheets or sending messages
"""
import sys
import logging
import time
from datetime import datetime
from config import SHEET_TOKENS, CARRIER_ALIASES, SHEET_OWNERS
from lark_client import LarkClient
from carriers import CarrierTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Permanent named tabs — always scanned
PERMANENT_TABS = {"Hannah", "Lucy", "Other"}

# Month tab names in order — used to compute current + previous month
MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# These statuses mean the carrier API gave us nothing real — don't write to sheet
BAD_STATUSES = {"UNKNOWN", "NOT FOUND", ""}

# These statuses mean the shipment is done — skip re-checking
DONE_STATUSES = {"DELIVERED"}


def normalize_carrier(carrier_str: str) -> str:
    """Convert carrier name from sheet (e.g. 'Fedex') to API client key (e.g. 'fedex')."""
    return CARRIER_ALIASES.get(carrier_str.lower().strip(), carrier_str.lower().strip())


def tabs_to_scan() -> set:
    """Return the set of tab titles to scan: named tabs + current + previous month."""
    now = datetime.utcnow()
    current = MONTH_NAMES[now.month - 1]          # e.g. "FEB"
    previous = MONTH_NAMES[(now.month - 2) % 12]  # e.g. "JAN"  (wraps Dec→Nov)
    return PERMANENT_TABS | {current, previous}


def process_sheet(lark: LarkClient, tracker: CarrierTracker,
                  spreadsheet_token: str, dry_run: bool = False) -> list:
    """Process the relevant tabs in a spreadsheet and return result dicts."""
    all_results = []

    try:
        tabs = lark.get_sheet_metadata(spreadsheet_token)
    except Exception as e:
        logger.error(f"Failed to read spreadsheet {spreadsheet_token}: {e}")
        return all_results

    target_tabs = tabs_to_scan()
    tabs_to_process = [t for t in tabs if t["title"] in target_tabs]

    if not tabs_to_process:
        logger.warning(f"No matching tabs found in {spreadsheet_token}. "
                       f"Looking for: {sorted(target_tabs)}. "
                       f"Available: {[t['title'] for t in tabs]}")
        return all_results

    logger.info(f"Scanning tabs {[t['title'] for t in tabs_to_process]} in {spreadsheet_token}")

    for tab in tabs_to_process:
        tab_title = tab["title"]
        sheet_id  = tab["sheet_id"]
        logger.info(f"  Tab: {tab_title} ({sheet_id})")

        try:
            rows = lark.read_tracking_data(spreadsheet_token, sheet_id)
        except Exception as e:
            logger.error(f"  Failed to read tab '{tab_title}': {e}")
            continue

        logger.info(f"  {len(rows)} rows with tracking in '{tab_title}'")

        for row in rows:
            tracking_num   = row["tracking_num"]
            carrier_raw    = row["carrier"]
            current_status = row.get("current_status", "").strip().upper()

            if current_status in DONE_STATUSES:
                logger.info(f"  Skipping {tracking_num} — already DELIVERED")
                continue

            carrier = normalize_carrier(carrier_raw)
            if not carrier or carrier not in CARRIER_ALIASES.values():
                logger.warning(f"  Row {row['row_num']}: unknown carrier '{carrier_raw}'")
                all_results.append({
                    **row,
                    "new_status": current_status or "UNKNOWN CARRIER",
                    "tab": tab_title,
                    "sheet_token": spreadsheet_token,
                })
                continue

            result        = tracker.track(tracking_num, carrier)
            new_status    = result["status"]
            delivery_date = result.get("delivery_date", "")
            raw_status    = result.get("raw_status", "")
            api_error     = result.get("error", "")

            if api_error or new_status.upper() in BAD_STATUSES:
                display_status = current_status if current_status else "PENDING"
                logger.warning(f"  {tracking_num}: API error ({api_error[:60]}), keeping '{display_status}'")
                all_results.append({
                    **row,
                    "new_status":    display_status,
                    "delivery_date": row.get("delivery_date", ""),
                    "raw_status":    raw_status,
                    "tab":           tab_title,
                    "sheet_token":   spreadsheet_token,
                })
            else:
                if not dry_run and new_status.upper() != current_status:
                    try:
                        lark.update_tracking_row(
                            spreadsheet_token, sheet_id,
                            row["row_num"], new_status, delivery_date,
                        )
                        logger.info(f"  Updated {tracking_num}: {current_status} → {new_status}")
                    except Exception as e:
                        logger.error(f"  Failed to write row {row['row_num']}: {e}")

                all_results.append({
                    **row,
                    "new_status":    new_status,
                    "delivery_date": delivery_date,
                    "raw_status":    raw_status,
                    "tab":           tab_title,
                    "sheet_token":   spreadsheet_token,
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

    scanning = sorted(tabs_to_scan())
    logger.info(f"Tabs to scan this run: {scanning}")
          logger.info(f"SHEET_OWNERS mapping: {SHEET_OWNERS}")

    lark    = LarkClient()
    tracker = CarrierTracker()
    all_results = []

    for token in SHEET_TOKENS:
        logger.info(f"Processing spreadsheet: {token}")
        results = process_sheet(lark, tracker, token, dry_run)
        all_results.extend(results)
        logger.info(f"  → {len(results)} active shipments from {token}")

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
