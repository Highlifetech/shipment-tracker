"""
Lark Shipment Tracking Bot - Main Entry Point

Scans the following tabs in each spreadsheet:
  - Hannah, Lucy, Other - permanent named tabs, always scanned
  - Current month tab - e.g. MAR
  - Previous month tab - e.g. FEB (catches end-of-month layover)

DELIVERED rows are skipped individually so layover shipments from the previous
month that are still in transit will still appear.

Multi-piece UPS shipments: when one tracking number in the sheet belongs
to a multi-box shipment, the UPS API returns all sibling tracking numbers.
We consolidate those siblings so that only ONE summary line is shown per
shipment (e.g. "1ZHE... (5 boxes): 3 arriving Mar 5, 2 unscanned").

Exception Alerts: When run with --check-exceptions, compares current carrier
status to the last known status stored in /tmp/shipment_status_cache.json.
If a new exception/delay is detected, fires an immediate alert to the chat.

Usage:
    python main.py                     # Run once (full summary)
    python main.py --dry-run           # No writes or messages
    python main.py --check-exceptions  # Alert-only: fires only if new issues found
"""

import sys
import json
import os
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

# Where we store last-known statuses between runs
STATUS_CACHE_PATH = os.environ.get("STATUS_CACHE_PATH", "/tmp/shipment_status_cache.json")


def normalize_carrier(carrier_str):
    return CARRIER_ALIASES.get(carrier_str.lower().strip(), carrier_str.lower().strip())


def tabs_to_scan():
    now = datetime.utcnow()
    current = MONTH_NAMES[now.month - 1]
    previous = MONTH_NAMES[(now.month - 2) % 12]
    return PERMANENT_TABS | {current, previous}


def load_status_cache():
    """Load last-known statuses from cache file."""
    try:
        if os.path.exists(STATUS_CACHE_PATH):
            with open(STATUS_CACHE_PATH, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Could not load status cache: %s", e)
    return {}


def save_status_cache(cache):
    """Save current statuses to cache file."""
    try:
        with open(STATUS_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
        logger.info("Status cache saved (%d entries)", len(cache))
    except Exception as e:
        logger.warning("Could not save status cache: %s", e)


def is_exception_status(status_str, raw_status=""):
    """Return True if the status string indicates a new problem."""
    s = status_str.upper()
    r = raw_status.upper()
    if "EXCEPTION" in s or "EXCEPTION" in r:
        return True
    if "DELAY" in s or "DELAY" in r:
        return True
    if "CLEARANCE" in r:
        return True
    if "IMPORT C.O.D" in r:
        return True
    if "CUSTOMS" in r:
        return True
    if "HELD" in r:
        return True
    if "GOVERNMENT AGENCY" in r:
        return True
    if "PROOF OF VALUE" in r:
        return True
    if "RETURNED" in r:
        return True
    if "REFUSED" in r:
        return True
    if "ADDRESS" in r and "CORRECTED" in r:
        return True
    return False


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

    # sibling_skip: set of tracking numbers already covered by a multi-box result
    sibling_skip = set()

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
                logger.info("  Skipping %s - already DELIVERED", tracking_num)
                continue

            # Skip if this tracking number is a sibling already shown
            if tracking_num in sibling_skip:
                logger.info("  Skipping %s - already covered by multi-box parent", tracking_num)
                continue

            carrier = normalize_carrier(carrier_raw)
            if not carrier or carrier not in CARRIER_ALIASES.values():
                logger.warning("  Row %d: unknown carrier '%s'", row["row_num"], carrier_raw)
                all_results.append({
                    **row,
                    "new_status": current_status or "UNKNOWN CARRIER",
                    "packages": [],
                    "tab": tab_title,
                    "sheet_token": spreadsheet_token,
                })
                continue

            result = tracker.track(tracking_num, carrier)
            new_status = result["status"]
            delivery_date = result.get("delivery_date", "")
            raw_status = result.get("raw_status", "")
            api_error = result.get("error", "")
            packages = result.get("packages", [])

            # Register sibling tracking numbers so we don't double-list them
            if packages:
                for pkg in packages:
                    sib = pkg.get("tracking_num", "").strip()
                    if sib and sib != tracking_num:
                        sibling_skip.add(sib)
                logger.info(
                    "  %s is a %d-box shipment; registered %d siblings to skip",
                    tracking_num,
                    len(packages),
                    len(packages) - 1,
                )

            if api_error or new_status.upper() in BAD_STATUSES:
                display_status = current_status if current_status else "PENDING"
                logger.warning(
                    "  %s: API error (%s), keeping '%s'",
                    tracking_num,
                    str(api_error)[:60],
                    display_status,
                )
                all_results.append({
                    **row,
                    "new_status": display_status,
                    "delivery_date": row.get("delivery_date", ""),
                    "raw_status": raw_status,
                    "packages": packages,
                    "tab": tab_title,
                    "sheet_token": spreadsheet_token,
                })
            else:
                if not dry_run and new_status.upper() != current_status:
                    try:
                        lark.update_tracking_row(
                            spreadsheet_token,
                            sheet_id,
                            row["row_num"],
                            new_status,
                            delivery_date,
                        )
                        logger.info(
                            "  Updated %s: %s -> %s",
                            tracking_num,
                            current_status,
                            new_status,
                        )
                    except Exception as e:
                        logger.error("  Failed to write row %d: %s", row["row_num"], e)

                all_results.append({
                    **row,
                    "new_status": new_status,
                    "delivery_date": delivery_date,
                    "raw_status": raw_status,
                    "packages": packages,
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
            pkg_count = len(r.get("packages", []))
            logger.info(
                "  [%s] %s | %s | %s | %s | %s | %d boxes",
                r.get("tab"),
                r["tracking_num"],
                r["carrier"],
                r["new_status"],
                r.get("delivery_date", ""),
                r.get("customer", ""),
                pkg_count,
            )

    return all_results


def run_exception_check(external_cache=None):
    """
    Exception-alert-only run. Checks all shipments every 30 min.
    Compares current status to last known status stored in cache.
    Only sends a Lark alert if a NEW exception or delay is detected.
    Does not send the full summary - only fires for problems.
    """
    if not SHEET_TOKENS:
        logger.error("No sheet tokens configured.")
        return

    logger.info("=== EXCEPTION CHECK MODE ===")
    cache = external_cache if external_cache is not None else load_status_cache()            

    lark = LarkClient()
    tracker = CarrierTracker()
    alerts = []
    sibling_skip = set()

    for token in SHEET_TOKENS:
        try:
            tabs = lark.get_sheet_metadata(token)
        except Exception as e:
            logger.error("Failed to read spreadsheet %s: %s", token, e)
            continue

        target_tabs = tabs_to_scan()
        tabs_to_process = [t for t in tabs if t["title"] in target_tabs]

        for tab in tabs_to_process:
            tab_title = tab["title"]
            sheet_id = tab["sheet_id"]

            try:
                rows = lark.read_tracking_data(token, sheet_id)
            except Exception as e:
                logger.error("  Failed to read tab '%s': %s", tab_title, e)
                continue

            for row in rows:
                tracking_num = row["tracking_num"]
                current_status = row.get("current_status", "").strip().upper()

                if current_status in DONE_STATUSES:
                    continue
                if tracking_num in sibling_skip:
                    continue

                carrier_raw = row["carrier"]
                carrier = normalize_carrier(carrier_raw)
                if not carrier or carrier not in CARRIER_ALIASES.values():
                    continue

                result = tracker.track(tracking_num, carrier)
                new_status = result["status"].upper()
                raw_status = result.get("raw_status", "")
                api_error = result.get("error", "")
                packages = result.get("packages", [])

                # Register siblings
                if packages:
                    for pkg in packages:
                        sib = pkg.get("tracking_num", "").strip()
                        if sib and sib != tracking_num:
                            sibling_skip.add(sib)

                if api_error or new_status in BAD_STATUSES:
                    time.sleep(0.5)
                    continue

                cache_key = tracking_num
                last_status = cache.get(cache_key, {}).get("status", "")
                last_raw = cache.get(cache_key, {}).get("raw_status", "")

                # Detect NEW exception (status or raw message changed to a problem)
                status_changed = new_status != last_status
                raw_changed = raw_status != last_raw

                if (status_changed or raw_changed) and is_exception_status(new_status, raw_status):
                    recipient = row.get("recipient", "").strip()
                    customer = row.get("customer", "").strip()
                    if recipient.upper() == "CUSTOMER DIRECT":
                        name = customer or "Unknown"
                    else:
                        name = recipient or customer or "Unknown"

                    alerts.append({
                        "tracking_num": tracking_num,
                        "carrier": carrier_raw.upper(),
                        "name": name,
                        "tab": tab_title,
                        "new_status": new_status,
                        "raw_status": raw_status,
                        "prev_status": last_status,
                    })
                    logger.warning(
                        "NEW EXCEPTION on %s (%s): %s -> %s | %s",
                        tracking_num, carrier_raw, last_status, new_status, raw_status
                    )

                # Check package-level exceptions for multi-box UPS
                if packages:
                    for pkg in packages:
                        pkg_tn = pkg.get("tracking_num", "")
                        pkg_status = pkg.get("status", "").upper()
                        pkg_cache_key = pkg_tn
                        last_pkg_status = cache.get(pkg_cache_key, {}).get("status", "")
                        if pkg_status != last_pkg_status and is_exception_status(pkg_status):
                            recipient = row.get("recipient", "").strip()
                            customer = row.get("customer", "").strip()
                            if recipient.upper() == "CUSTOMER DIRECT":
                                name = customer or "Unknown"
                            else:
                                name = recipient or customer or "Unknown"
                            alerts.append({
                                "tracking_num": pkg_tn,
                                "carrier": "UPS",
                                "name": name,
                                "tab": tab_title,
                                "new_status": pkg_status,
                                "raw_status": "",
                                "prev_status": last_pkg_status,
                                "parent_tracking": tracking_num,
                            })
                            logger.warning(
                                "NEW EXCEPTION on UPS sibling %s: %s -> %s",
                                pkg_tn, last_pkg_status, pkg_status
                            )
                        cache[pkg_cache_key] = {"status": pkg_status, "raw_status": ""}

                # Update cache
                cache[cache_key] = {"status": new_status, "raw_status": raw_status}
                time.sleep(0.5)

    # Save updated cache
    if external_cache is None:
        save_status_cache(cache)

    # Send alerts if any
    if alerts:
        logger.info("Sending %d exception alert(s) to Lark", len(alerts))
        lark.send_exception_alerts(alerts)
    else:
        logger.info("No new exceptions detected. No alert sent.")


def main():
    dry_run = "--dry-run" in sys.argv
    check_exceptions = "--check-exceptions" in sys.argv

    if check_exceptions:
        run_exception_check()
        logger.info("Exception check done!")
        return

    if dry_run:
        logger.info("=== DRY RUN MODE - no writes or messages ===")

    run_tracker(dry_run=dry_run)
    logger.info("Done!")


if __name__ == "__main__":
    main()
