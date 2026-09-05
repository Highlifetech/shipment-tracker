"""Order-first picking and carton allocation. Lark is the shipment system of record.

The SQLite journal is ONLY a write coordinator/uncertain-request ledger, on a
required persistent volume. One Railway replica; OS locking handles its workers.
No order quantity is ever guessed from a description or an inbound tracker row.
"""
import copy
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

from fulfillment_base import BaseError, text


class Problem(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def integer(value, label, minimum=0, maximum=100000000):
    # Reject blank, bool, fractional, exponent and negative input, never truncate.
    value = text(value)
    if not re.fullmatch(r"\d+(?:\.0+)?", value):
        raise Problem(label + " must be a whole number")
    number = int(value.split(".")[0])
    if number < minimum or number > maximum:
        raise Problem("%s must be between %s and %s" % (label, minimum, maximum))
    return number


def address_key(value):
    return " ".join(text(value).casefold().split())


def inventory(rows, shipments):
    china_used, us_used, received = defaultdict(int), defaultdict(int), defaultdict(int)
    for shipment in shipments:
        if shipment["status"] == "Cancelled":
            continue
        for line in shipment["lines"]:
            bucket = us_used if shipment["route"] == "us_to_customer" else china_used
            bucket[line["key"]] += line["qty"]
            if shipment["route"] == "china_to_us" and shipment["status"] == "Received":
                received[line["key"]] += line["qty"]
    items = []
    for row in rows:
        item = {k: text(row.get(k)) for k in ("order", "customer", "product", "address", "source",
                                               "tracking", "carrier", "method", "date_shipped")}
        for quantity in ('ordered_quantity', 'quantity_shipped'):
            try:
                item[quantity] = integer(row.get(quantity), quantity)
            except Problem:
                item[quantity] = None
        for k in ("key", "table_id", "record_id", "source_url", "photos", "photo_field_id"):
            item[k] = row.get(k, [] if k == "photos" else "")
        reasons = []
        if any(not item[k] for k in ("order", "customer", "product")):
            reasons.append("Missing order, customer or item description")
        if row.get("ready") is not True:
            reasons.append("Not approved Ready to Pack")
        china, us = 0, 0
        try:
            china = integer(row.get("opening_china"), "Opening China quantity")
            us = integer(row.get("opening_us", 0) if row.get("opening_us") is not None else 0,
                         "Opening US quantity")
        except Problem as exc:
            reasons.append(str(exc))
        item["china_available"] = china - china_used[row["key"]]
        item["us_available"] = us + received[row["key"]] - us_used[row["key"]]
        if item["china_available"] < 0 or item["us_available"] < 0:
            reasons.append("Quantity conflict: opening balance is below saved allocations")
        item["issues"] = reasons
        items.append(item)
    if len({i["key"] for i in items}) != len(items):
        raise Problem("Duplicate source record IDs; check source table mapping")
    return items


def build_manifest(payload, items, settings, actor):
    if not isinstance(payload, dict):
        raise Problem("Expected a shipment object")
    route = payload.get("route")
    if route not in ("china_to_us", "china_to_customer", "us_to_customer"):
        raise Problem("Choose an origin and destination route")
    try:
        submission = str(uuid.UUID(str(payload.get("submission_id", ""))))
    except ValueError as exc:
        raise Problem("Missing submission ID; reload the packing screen") from exc
    allocations = payload.get("lines")
    if not isinstance(allocations, list) or not 1 <= len(allocations) <= 100:
        raise Problem("Select 1–100 item/box allocations")
    box_count = integer(payload.get("box_count"), "Box count", 1, 30)
    by_key = {i["key"]: i for i in items}
    totals, used, lines = defaultdict(int), set(), []
    for allocation in allocations:
        if not isinstance(allocation, dict):
            raise Problem("Invalid item allocation")
        item = by_key.get(allocation.get("key"))
        if not item:
            raise Problem("An item no longer exists in an approved source table; refresh orders")
        if item["issues"]:
            raise Problem(item["order"] + ": " + "; ".join(item["issues"]))
        qty = integer(allocation.get("qty"), "Packed quantity", 1)
        box = integer(allocation.get("box"), "Box number", 1, box_count)
        pair = (item["key"], box)
        if pair in used:
            raise Problem("The same item appears twice in one box; combine its quantity")
        used.add(pair)
        totals[item["key"]] += qty
        line = {k: copy.deepcopy(v) for k, v in item.items() if k not in (
            "issues", "china_available", "us_available")}
        line.update(qty=qty, box=box)
        lines.append(line)
    availability = "us_available" if route == "us_to_customer" else "china_available"
    for key, qty in totals.items():
        if qty > by_key[key][availability]:
            raise Problem("%s: %s selected, only %s available. Refresh and adjust." %
                          (by_key[key]["order"], qty, by_key[key][availability]))
    extras = payload.get("extras", [])
    if not isinstance(extras, list) or len(extras) > 100:
        raise Problem("Provide no more than 100 extra item lines")
    clean_extras = []
    for extra in extras:
        if not isinstance(extra, dict):
            raise Problem("Invalid extra item")
        description = text(extra.get("description"))
        if not description or len(description) > 200:
            raise Problem("Extra item description must be 1–200 characters")
        clean_extras.append(dict(
            description=description,
            qty=integer(extra.get("qty"), "Extra item quantity", 1),
            box=integer(extra.get("box"), "Extra item box", 1, box_count)))
    if {l["box"] for l in lines}.union(x["box"] for x in clean_extras) != set(range(1, box_count + 1)):
        raise Problem("Every box must contain at least one item")
    override = text(payload.get("address"))
    if len(override) > 2000:
        raise Problem("Shipping address is too long")
    if override:
        if route != "china_to_us" and len({address_key(l["customer"]) for l in lines}) != 1:
            raise Problem("Customer shipments must contain only one customer")
        address = override
    elif route == "china_to_us":
        address = text(settings.get("warehouse_address"))
        if not address:
            raise Problem("Configure the verified US receiving address first")
    else:
        if len({address_key(l["customer"]) for l in lines}) != 1:
            raise Problem("Customer shipments must contain only one customer")
        addresses = {address_key(l["address"]) for l in lines}
        if "" in addresses or len(addresses) != 1:
            raise Problem("These items have different/missing addresses. Create one shipment per destination.")
        address = lines[0]["address"]
    carrier, tracking = text(payload.get("carrier")), text(payload.get("tracking"))
    if carrier and carrier not in ("UPS", "FedEx", "DHL", "Other"):
        raise Problem("Choose a supported carrier")
    if tracking and not re.fullmatch(r"[A-Za-z0-9 -]{5,80}", tracking):
        raise Problem("Check the tracking number")
    box_details = payload.get("boxes")
    if box_details is None:
        box_details = [{"box": n, "carrier": carrier, "tracking": tracking, "method": ""} for n in range(1, box_count + 1)]
    if not isinstance(box_details, list) or len(box_details) != box_count:
        raise Problem("Provide carrier details for every box")
    clean_boxes = []
    for b in box_details:
        if not isinstance(b, dict):
            raise Problem("Invalid box details")
        number = integer(b.get("box"), "Box number", 1, box_count)
        bc, bt, method = text(b.get("carrier")), text(b.get("tracking")), text(b.get("method"))
        if bc and bc not in ("UPS", "FedEx", "DHL", "Other"):
            raise Problem("Choose a supported carrier")
        if bt and (not bc or not re.fullmatch(r"[A-Za-z0-9 -]{5,80}", bt)):
            raise Problem("Check the box carrier and tracking number")
        if len(method) > 100:
            raise Problem("Service name is too long")
        clean_boxes.append(dict(box=number, carrier=bc, tracking=bt, method=method))
    if {b["box"] for b in clean_boxes} != set(range(1, box_count + 1)):
        raise Problem("Box numbers must be unique")
    now = datetime.now(timezone.utc).isoformat()
    return {"schema": 1, "submission_id": submission,
            "shipment_id": "SHP-" + submission[:8].upper(),
            "request_hash": digest(payload), "status": "Packed", "route": route,
            "batch_ref": text(payload.get("batch_ref"))[:100],
            "address": address, "carrier": carrier, "tracking": tracking,
            "box_count": box_count, "boxes": clean_boxes, "lines": sorted(lines, key=lambda l: (l["box"], l["order"], l["product"])),
            "extras": sorted(clean_extras, key=lambda x: (x["box"], x["description"])),
            "units": sum(totals.values()) + sum(x["qty"] for x in clean_extras), "created_at": now, "created_by": actor,
            "notes": text(payload.get("notes"))[:2000],
            "history": [{"status": "Packed", "at": now, "by": actor}]}


class Coordinator:
    """Fail closed after any uncertain Lark write; never blind-retry a create.

    Requires a Railway volume, one replica, all app writes via this coordinator,
    and locked-down shipment/opening-balance fields in Base. Not a multi-region DB.
    """
    def __init__(self, directory):
        self.directory = directory

    @contextmanager
    def lock(self):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        with open(os.path.join(self.directory, "writer.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db = sqlite3.connect(os.path.join(self.directory, "journal.sqlite"))
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE IF NOT EXISTS operations "
                           "(id TEXT PRIMARY KEY, expected TEXT NOT NULL, status TEXT NOT NULL)")
                db.commit()
                yield db
            finally:
                db.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def reconcile(db, shipments):
        actual = {s["submission_id"]: {k: v for k, v in s.items() if k not in ("record_id", "lark_url")}
                  for s in shipments}
        for opid, expected in db.execute("SELECT id, expected FROM operations WHERE status='pending'").fetchall():
            expected = json.loads(expected)
            if canonical(actual.get(expected["submission_id"])) != canonical(expected):
                raise Problem("A previous Lark save is unconfirmed. Packing is paused to prevent duplicates. "
                              "Refresh to reconcile; if it persists, an administrator must check the write journal.")
            db.execute("UPDATE operations SET status='done' WHERE id=?", (opid,))
        # Detect deleted/externally modified manifests: do not silently free stock.
        latest = {}
        for expected, in db.execute("SELECT expected FROM operations WHERE status='done' ORDER BY rowid"):
            expected = json.loads(expected)
            latest[expected["submission_id"]] = expected
        for key, expected in latest.items():
            if canonical(actual.get(key)) != canonical(expected):
                raise Problem("A saved shipment was changed or removed outside the app. Packing is paused for reconciliation.")
        db.commit()

    @staticmethod
    def prepare(db, doc):
        key = str(uuid.uuid4())
        expected = {k: v for k, v in doc.items() if k not in ("record_id", "lark_url")}
        db.execute("INSERT INTO operations VALUES (?, ?, 'pending')", (key, canonical(expected)))
        db.commit()  # durable BEFORE issuing the remote request
        return key

    @staticmethod
    def finish(db, opid):
        db.execute("UPDATE operations SET status='done' WHERE id=?", (opid,))
        db.commit()


class FulfillmentService:
    def __init__(self, store, settings, coordinator=None):
        self.store, self.settings, self.coordinator = store, settings, coordinator

    def read(self):
        shipments = self.store.shipments()
        return inventory(self.store.source_rows(), shipments), shipments

    def preview(self, payload, actor):
        items, _ = self.read()
        return build_manifest(payload, items, self.settings, actor)

    def save(self, payload, actor):
        if not self.coordinator:
            raise Problem("Live saving is disabled until a persistent writer volume is configured")
        with self.coordinator.lock() as db:
            items, shipments = self.read()  # fresh balances INSIDE the lock
            self.coordinator.reconcile(db, shipments)
            existing = next((s for s in shipments if s["submission_id"] == payload.get("submission_id")), None)
            if existing:
                if existing["request_hash"] != digest(payload):
                    raise Problem("This submission ID was already used with different contents")
                return existing
            doc = build_manifest(payload, items, self.settings, actor)
            self.store.encode(doc)  # validate manifest size before preparing a write
            opid = self.coordinator.prepare(db, doc)
            record = self.store.create(doc)
            self.coordinator.finish(db, opid)
            doc.update(record_id=record, lark_url=self.store.record_link(self.store.table, record))
            return doc

    def transition(self, submission, action, payload, actor):
        if not self.coordinator:
            raise Problem("Live saving is disabled")
        with self.coordinator.lock() as db:
            shipments = self.store.shipments()
            self.coordinator.reconcile(db, shipments)
            doc = next((copy.deepcopy(s) for s in shipments if s["submission_id"] == submission), None)
            if not doc:
                raise Problem("Shipment not found")
            targets = {"ship": "Shipped", "receive": "Received", "cancel": "Cancelled"}
            if action not in targets:
                raise Problem("Unknown shipment action")
            target = targets[action]
            if doc["status"] == target:
                return doc  # repeated click
            if target == "Received":
                if doc["status"] != "Shipped" or doc["route"] != "china_to_us":
                    raise Problem("Only a shipped inbound batch can be received in the US")
            elif doc["status"] != "Packed":
                raise Problem("Only a packed shipment can be shipped or cancelled")
            if target == "Shipped":
                carrier, tracking = text(payload.get("carrier")), text(payload.get("tracking"))
                if carrier not in ("UPS", "FedEx", "DHL", "Other") or not re.fullmatch(r"[A-Za-z0-9 -]{5,80}", tracking):
                    raise Problem("Enter the carrier and tracking from the label before marking shipped")
                doc.update(carrier=carrier, tracking=tracking)
            doc["status"] = target
            doc["history"].append({"status": target, "at": datetime.now(timezone.utc).isoformat(), "by": actor})
            opid = self.coordinator.prepare(db, doc)
            self.store.update(doc)
            self.coordinator.finish(db, opid)
            return doc
