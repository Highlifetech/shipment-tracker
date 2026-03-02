"""
Lark Shipment Tracking Bot - Main Entry Point

Scans the following tabs in each spreadsheet:
  - Hannah, Lucy, Other - permanent named tabs, always scanned
  - Current month tab - e.g. MAR
  - Previous month tab - e.g. FEB (catches end-of-month layover)

DELIVERED rows are skipped individually so layover shipments from the
previous month that are still in transit will still appear.

Usage:
    python main.py            # Run once
    python main.py --dry-run  # No writes or messages
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

PERMANENT_TABS = {"Hannah", "Lucy", "Other"}

MONTH_NAMES = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]

BAD_STATUSES = {"UNKNOWN", "NOT FOUND", ""}
DONE_STATUSES = {"DELIVERED"}


def normalize_carrier(carrier_str):
    return CARRIER_ALIASES.get(carrier_str.lower().strip(), carrier_str.lower().strip())


def tabs_to_scan():
    now = datetime.utcnow()
    current = MONTH_NAMES[now.month - 1]
    previous = MONTH_NAMES[(now.month - 2) % 12]
    return PERMANENT_TABS | {current, previous}


def process_sheet(lark, tracker, spreadsheet_token, dry_run=False):
    all_results = []
    try:
        tabs = lark.get_sheet_metadata(spreadsheet_token)
    except Exception as e:
        logger.error("Failed to read spreadsheet %s: %s", spreadsheet_token, e)
        return all_results

    target_tabs = tabs_to_scan()
    tabs_to_process = [t for t in tabs if t["title"] in target_tabs]

    if not tabs_to_process:
        logger.warning(
            "No matching tabs in %s. Want: %s. Have: %s",
            spreadsheet_token,
            sorted(target_tabs),
            [t["title"] for t in tabs],
        )
        return all_results

    logger.info("Scanning %s in %s", [t["title"] for t in tabs_to_process], spreadsheet_token)

    for tab in tabs_to_process:
        tab_title = tab["title"]
        sheet_id = tab["sheet_id"]
        logger.info("  Tab: %s (%s)", tab_title, sheet_id)

        try:
            rows = lark.read_tracking_data(spreadsheet_token, sheet_id)
        except Exception as e:
            logger.error("  Failed to read tab '%s': %s", tab_title, e)
            continue

        logger.info("  %d rows with tracking in '%s'", len(rows), tab_title)

        for row in rows:
            tracking_num = row["tracking_num"]
            carrier_raw = row["carrier"]
            current_status = row.get("current_status", "").strip().upper()

            if current_status in DONE_STATUSES:
                logger.info("    Skipping %s - already DELIVERED", tracking_num)
                continue

            carrier = normalize_carrier(carrier_raw)
            if not carrier or carrier not in CARRIER_ALIASES.values():
                logger.warning("    Row %d: unknown carrier '%s'", row["row_num"], carrier_raw)
                all_results.append({
                    **row,
                    "new_status": current_status or "UNKNOWN CARRIER",
                    "tab": tab_title,
                    "sheet_token": spreadsheet_token,
                })
                continue

            result = tracker.track(tracking_num, carrier)
            new_status = result["status"]
            delivery_date = result.get("delivery_date", "")
            raw_status = result.get("raw_status", "")
            api_error = result.get("error", "")

            if api_error or new_status.upper() in BAD_STATUSES:
                display_status = current_status if current_status else "PENDING"
                logger.warning(
                    "    %s: API error (%s), keeping '%s'",
                    tracking_num,
                    str(api_error)[:60],
                    display_status,
                )
                all_results.append({
                    **row,
                    "new_status": display_status,
                    "delivery_date": row.get("delivery_date", ""),
                    "raw_status": raw_status,
                    "tab": tab_title,
                    "sheet_token": spreadsheet_token,
                })
            else:
                if not dry_run and new_status.upper() != current_status:
                    try:
                        lark.update_tracking_row(
                            spreadsheet_token, sheet_id,
                            row["row_num"], new_status, delivery_date,
                        )
                        logger.info(
                            "    Updated %s: %s -> %s",
                            tracking_num, current_status, new_status,
                        )
                    except Exception as e:
                        logger.error("    Failed to write row %d: %s", row["row_num"], e)

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


def run_tracker(dry_run=False, chat_id=None, message_id=None):
    if not SHEET_TOKENS:
        logger.error("No sheet tokens configured. Set LARK_SHEET_TOKENS env var.")
        return []

    logger.info("Tabs to scan: %s", sorted(tabs_to_scan()))

    lark = LarkClient()
    tracker = CarrierTracker()

    all_results = []
    for token in SHEET_TOKENS:
        logger.info("Processing spreadsheet: %s", token)
        results = process_sheet(lark, tracker, token, dry_run)
        all_results.extend(results)
        logger.info("  -> %d active shipments from %s", len(results), token)

    logger.info("Total active shipments: %d", len(all_results))

    if not dry_run:
        try:
            lark.send_daily_summary(
                all_results,
                chat_id=chat_id,
                message_id=message_id,
            )
            logger.info("Summary sent to group chat")
        except Exception as e:
            logger.error("Failed to send summary: %s", e)
    else:
        logger.info("Dry run complete. Results:")
        for r in all_results:
            logger.info(
                "  [%s] %s | %s | %s | %s | %s",
                r.get("tab"), r["tracking_num"], r["carrier"],
                r["new_status"], r.get("delivery_date", ""), r.get("customer", ""),
            )

    return all_results


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("=== DRY RUN MODE - no writes or messages ===")
    run_tracker(dry_run=dry_run)
    logger.info("Done!")


if __name__ == "__main__":
    main()
