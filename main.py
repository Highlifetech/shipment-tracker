"""
Lark Shipment Tracking Bot — Main Entry Point

Reads tracking numbers from Lark Sheets, checks status via carrier APIs,
updates the sheet, and sends a daily summary to a Lark group chat.

Usage:
    python main.py              # Run once (check all sheets)
    python main.py --dry-run    # Run without writing to sheets or sending messages
"""
import sys
import logging
import time
from config import SHEET_TOKENS, CARRIER_ALIASES
from lark_client import LarkClient
from carriers import CarrierTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def normalize_carrier(carrier_str: str) -> str:
    """Convert carrier name from sheet to API client key."""
    key = carrier_str.lower().strip()
    return CARRIER_ALIASES.get(key, key)


def process_sheet(lark: LarkClient, tracker: CarrierTracker,
                  spreadsheet_token: str, dry_run: bool = False) -> list:
    """Process all tabs in a single spreadsheet.
    
    Returns list of result dicts for the daily summary.
    """
    all_results = []

    try:
        tabs = lark.get_sheet_metadata(spreadsheet_token)
    except Exception as e:
        logger.error(f"Failed to read spreadsheet {spreadsheet_token}: {e}")
        return all_results

    for tab in tabs:
        tab_title = tab["title"]
        sheet_id = tab["sheet_id"]
        logger.info(f"Processing tab: {tab_title} ({sheet_id})")

        try:
            rows = lark.read_tracking_data(spreadsheet_token, sheet_id)
        except Exception as e:
            logger.error(f"Failed to read tab {tab_title}: {e}")
            continue

        for row in rows:
            tracking_num = row["tracking_num"]
            carrier_raw = row["carrier"]
            carrier = normalize_carrier(carrier_raw)

            if not carrier or carrier not in CARRIER_ALIASES.values():
                logger.warning(
                    f"Row {row['row_num']}: Unknown carrier '{carrier_raw}', skipping"
                )
                all_results.append({
                    **row,
                    "new_status": "UNKNOWN CARRIER",
                    "tab": tab_title,
                    "sheet_token": spreadsheet_token,
                })
                continue

            # Check tracking status
            result = tracker.track(tracking_num, carrier)

            new_status = result["status"]
            delivery_date = result.get("delivery_date", "")
            location = result.get("location", "")

            # Update sheet (unless dry run)
            if not dry_run:
                try:
                    lark.update_tracking_row(
                        spreadsheet_token, sheet_id,
                        row["row_num"], new_status, delivery_date,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to update row {row['row_num']} in {tab_title}: {e}"
                    )

            all_results.append({
                **row,
                "new_status": new_status,
                "delivery_date": delivery_date,
                "location": location,
                "raw_status": result.get("raw_status", ""),
                "tab": tab_title,
                "sheet_token": spreadsheet_token,
            })

            # Small delay to avoid rate limits
            time.sleep(0.5)

    return all_results


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        logger.info("=== DRY RUN MODE — no writes or messages ===")

    if not SHEET_TOKENS:
        logger.error("No sheet tokens configured. Set LARK_SHEET_TOKENS env var.")
        sys.exit(1)

    lark = LarkClient()
    tracker = CarrierTracker()

    all_results = []

    for token in SHEET_TOKENS:
        logger.info(f"Processing spreadsheet: {token}")
        results = process_sheet(lark, tracker, token, dry_run)
        all_results.extend(results)

    logger.info(f"Total shipments processed: {len(all_results)}")

    # Send daily summary to Lark group chat
    if not dry_run:
        try:
            lark.send_daily_summary(all_results)
            logger.info("Daily summary sent to group chat")
        except Exception as e:
            logger.error(f"Failed to send daily summary: {e}")
    else:
        logger.info("Dry run — skipping group chat message")
        # Print summary to console instead
        for r in all_results:
            logger.info(
                f"  {r['tracking_num']} | {r['carrier']} | "
                f"{r['new_status']} | {r.get('delivery_date', '')} | "
                f"{r.get('customer', '')}"
            )

    logger.info("Done!")


if __name__ == "__main__":
    main()
