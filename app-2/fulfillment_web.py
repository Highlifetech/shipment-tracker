"""Authenticated fulfillment routes; registered on the existing Railway Flask app."""
import html
import json
import os
import time
import threading
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from flask import Response, abort, jsonify, redirect, request
from fulfillment import Coordinator, FulfillmentService, Problem
from fulfillment_base import BaseError, BaseStore
from background_sync import BackgroundSnapshot


ROUTES = {"china_to_us": "China → US warehouse", "china_to_customer": "China → customer",
          "us_to_customer": "US warehouse → customer"}


def page_html():
    from logo_data import PNG_B64
    with open(os.path.join(os.path.dirname(__file__), "fulfillment.html"), encoding="utf-8") as f:
        return f.read().replace('__OFF_MENU_LOGO__', 'data:image/png;base64,' + PNG_B64)


def packing_html(doc, photo_prefix="/api/fulfillment/photo?key=", saved=False):
    esc = lambda v: html.escape(str(v), quote=True)
    def source(line):
        url = line.get("source_url", "")
        return esc(url) if urlsplit(url).scheme == "https" else "#"
    def photo(line):
        url = photo_prefix + quote(line["key"], safe="")
        if saved:
            url += "&shipment=" + quote(doc["submission_id"])
        return '<img alt="Project photo" src="%s">' % esc(url) if line.get("photos") else '<span>No photo</span>'
    parts = ['''<!doctype html><html><head><meta charset="utf-8"><title>Packing list</title>
    <style>body{font:14px system-ui;color:#172136;margin:32px}header{display:flex;justify-content:space-between;border-bottom:3px solid #172136;padding-bottom:18px}h1{margin:4px 0}h2{margin-top:32px}small,.muted{color:#69758b}table{width:100%;border-collapse:collapse;margin:20px 0}td,th{text-align:left;border-bottom:1px solid #dce2ec;padding:10px;vertical-align:top}th{background:#f3f5f8}img{width:64px;height:64px;object-fit:contain}address{white-space:pre-wrap;font-style:normal}a{color:#315eea}tr{break-inside:avoid}.box{break-before:page}.tag{padding:8px;background:#f0f4ff}.summary{display:flex;gap:36px;margin:20px 0}@media print{body{margin:0;font-size:11px}@page{size:A4;margin:14mm}a{color:inherit;text-decoration:none}}</style></head><body>''']
    parts.append('<header><div><small>OFF MENU · PICK & PACK</small><h1>Packing list</h1>%s</div><div>%s<br>%s</div></header>' %
                 (esc(doc["shipment_id"]), esc(doc.get("batch_ref", "")), esc(doc["created_at"][:10])))
    parts.append('<p class="tag">%s · %s</p>' % ("SAVED — " + esc(doc["status"]) if saved else "PREVIEW — not saved or reserved", esc(ROUTES[doc["route"]])))
    parts.append('<address><b>Ship to / 收货地址</b><br>%s</address>' % esc(doc["address"]))
    parts.append('<div class="summary"><span><b>%s</b> cartons</span><span><b>%s</b> units</span><span>%s · %s</span></div>' %
                 (doc["box_count"], doc["units"], esc(doc.get("carrier") or "Carrier pending"), esc(doc.get("tracking") or "Tracking pending")))
    parts.append('<p>%s</p>' % esc(doc.get("notes", "")))
    def table(lines, extras=()):
        result = ['<table><thead><tr><th>Photo</th><th>Order / customer</th><th>Item</th><th>Box</th><th>Quantity</th></tr></thead><tbody>']
        for line in lines:
            result.append('<tr><td>%s</td><td><a href="%s">%s</a><br><small>%s</small></td><td>%s</td><td>%s</td><td><b>%s</b></td></tr>' %
                          (photo(line), source(line), esc(line["order"]), esc(line["customer"]), esc(line["product"]), line["box"], line["qty"]))
        for extra in extras:
            result.append('<tr><td><span>No photo</span></td><td><b>Extra item</b><br><small>Not linked to a sales order</small></td><td>%s</td><td>%s</td><td><b>%s</b></td></tr>' %
                          (esc(extra["description"]), extra["box"], extra["qty"]))
        return "".join(result) + '</tbody></table>'
    parts.append('<h2>Shipment summary / 装箱汇总</h2>' + table(doc["lines"], doc.get("extras", [])))
    for box in range(1, doc["box_count"] + 1):
        lines = [l for l in doc["lines"] if l["box"] == box]
        extras = [x for x in doc.get("extras", []) if x["box"] == box]
        details = next((b for b in doc.get("boxes", []) if b["box"] == box), doc)
        shipping = " · ".join(details.get(k, "") for k in ("carrier", "method", "tracking") if details.get(k))
        parts.append('<section class="box"><small>OFF MENU · %s</small><h1>Box %s of %s</h1><address>%s</address><p>%s units · %s</p>%s</section>' %
                     (esc(doc["shipment_id"]), box, doc["box_count"], esc(doc["address"]), sum(l["qty"] for l in lines) + sum(x["qty"] for x in extras), esc(shipping), table(lines, extras)))
    return "".join(parts) + '</body></html>'


def configured_service(lark):
    raw = os.environ.get("FULFILLMENT_CONFIG", "")
    if not raw:
        return None
    settings = json.loads(raw)
    if not settings.get("sources") or (not settings.get("shipment_table") and not settings.get("catalog_only")):
        raise Problem("Configure approved order sources and the dedicated shipment table")
    directory = os.environ.get("FULFILLMENT_STATE_DIR")
    writer = Coordinator(directory) if directory and os.environ.get("FULFILLMENT_SINGLE_REPLICA") == "1" and not settings.get("catalog_only") else None
    return FulfillmentService(BaseStore(lark, settings), settings, writer)


def register(app, lark, authorized, current_user, service=None):
    tracking_cache = {}
    tracking_lock = threading.Lock()
    carrier_tracker = None
    def get_service():
        result = service or configured_service(lark)
        if not result:
            raise Problem("Production Base connection is not configured. No demo data is substituted. Ask the administrator to complete the fulfillment setup.")
        return result

    def actor():
        user = current_user() or {}
        return "%s (%s)" % (user.get("name", "Lark user"), user.get("open_id", "")) if user else "Shared dashboard access"

    def read_catalog():
        svc = get_service()
        items, shipments = svc.read()
        return {"items": items, "shipments": shipments, "can_save": bool(svc.coordinator),
                "catalog_only": bool(svc.settings.get("catalog_only")),
                "warehouse_address": svc.settings.get("warehouse_address", ""),
                "demo": bool(svc.settings.get("demo"))}

    catalog_sync = BackgroundSnapshot(read_catalog, 60)
    if service is None and os.environ.get("FULFILLMENT_CONFIG"):
        catalog_sync.start()
    app.extensions['fulfillment_sync'] = catalog_sync

    def sync_carriers():
        nonlocal carrier_tracker
        snapshot = catalog_sync.snapshot()['data'] or {}
        if snapshot.get('demo'):
            return {}
        results = {}
        for doc in snapshot.get('shipments', []):
            if doc.get('status') != 'Shipped' or not doc.get('tracking') or doc.get('carrier') not in ('UPS', 'FedEx', 'DHL'):
                continue
            with tracking_lock:
                if carrier_tracker is None:
                    from carriers import CarrierTracker
                    carrier_tracker = CarrierTracker()
                result = carrier_tracker.track(doc['tracking'], doc['carrier'].lower())
                result['checked_at'] = datetime.now(timezone.utc).isoformat()
                tracking_cache[(doc['carrier'], doc['tracking'])] = (time.time(), result)
                results[doc['submission_id']] = result
        return results

    carrier_sync = BackgroundSnapshot(sync_carriers, 300)
    if service is None and os.environ.get('FULFILLMENT_CONFIG'):
        carrier_sync.start()
    app.extensions['fulfillment_carrier_sync'] = carrier_sync

    def photo_prefix():
        token = request.args.get("t") or request.headers.get("X-Dashboard-Token", "")
        return "/api/fulfillment/photo?" + ("t=" + quote(token, safe="") + "&" if token else "") + "key="

    def guard(write=False):
        if not authorized():
            abort(403)
        if write and request.headers.get("X-Fulfillment-Request") != "1":
            abort(403)  # non-simple same-origin header, no cross-origin form submissions

    def respond(fn):
        try:
            return jsonify(fn())
        except Problem as exc:
            return jsonify(error=str(exc)), 409
        except (BaseError, KeyError, ValueError) as exc:
            app.logger.warning("Fulfillment configuration/API failure: %s", type(exc).__name__)
            return jsonify(error=str(exc) if isinstance(exc, BaseError) else "Fulfillment configuration/data is invalid; administrator review required"), 502

    @app.get("/fulfillment")
    def fulfillment_page():
        if not authorized():
            return redirect("/dashboard")  # reuse existing Lark sign-in
        return Response(page_html(), mimetype="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})

    @app.get("/api/fulfillment/catalog")
    def fulfillment_catalog():
        guard()
        def get():
            if service is not None:
                return dict(read_catalog(), synced_at=datetime.now(timezone.utc).isoformat())
            catalog_sync.start()
            snapshot = catalog_sync.snapshot()
            data = snapshot.pop('data') or {"items": [], "shipments": [], "can_save": False, "catalog_only": True}
            if snapshot['error']:
                data['can_save'] = False
            return dict(data, synced_at=snapshot['last_synced'], sync=snapshot)
        return respond(get)

    @app.post('/api/fulfillment/sync')
    def request_catalog_sync():
        guard(True)
        catalog_sync.start()
        catalog_sync.request_refresh()
        return jsonify(queued=True)

    @app.post("/api/fulfillment/preview")
    def fulfillment_preview():
        guard(True)
        def preview():
            doc = get_service().preview(request.get_json(silent=True), actor())
            return {"shipment": doc, "html": packing_html(doc, photo_prefix())}
        return respond(preview)

    @app.post("/api/fulfillment/shipments")
    def fulfillment_save():
        guard(True)
        def save():
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise Problem("Expected a shipment object")
            doc = get_service().save(payload, actor())
            catalog_sync.request_refresh()
            return {"shipment": doc, "packing_url": "/fulfillment/packing-list/" + doc["submission_id"]}
        return respond(save)

    @app.post("/api/fulfillment/shipments/<submission>/<action>")
    def fulfillment_action(submission, action):
        guard(True)
        def transition():
            doc = get_service().transition(submission, action, request.get_json(silent=True) or {}, actor())
            catalog_sync.request_refresh()
            return {'shipment': doc}
        return respond(transition)

    @app.get("/fulfillment/packing-list/<submission>")
    def fulfillment_packing(submission):
        guard()
        try:
            doc = next((s for s in get_service().store.shipments() if s["submission_id"] == submission), None)
            if not doc:
                abort(404)
            return Response(packing_html(doc, photo_prefix(), saved=True), mimetype="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})
        except (BaseError, Problem):
            return Response("Packing list unavailable; reconnect to Lark and retry", status=503)

    @app.get("/api/fulfillment/photo")
    def fulfillment_photo():
        guard()
        try:
            svc = get_service()
            submission = request.args.get("shipment")
            if submission:
                doc = next((s for s in svc.store.shipments() if s["submission_id"] == submission), None)
                lines = doc["lines"] if doc else []
            else:
                snapshot = catalog_sync.snapshot()['data']
                lines = snapshot['items'] if snapshot else svc.store.source_rows()
            line = next((l for l in lines if l["key"] == request.args.get("key")), None)
            if not line:
                abort(404)
            data, mime = svc.store.photo(line)
            return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})
        except (BaseError, Problem):
            abort(404)

    @app.get("/api/fulfillment/shipments/<submission>/tracking")
    def fulfillment_tracking(submission):
        guard()
        def lookup():
            nonlocal carrier_tracker
            svc = get_service()
            if svc.settings.get("demo"):
                raise Problem("Carrier APIs are disabled in this local sample-data test")
            doc = next((s for s in svc.store.shipments() if s["submission_id"] == submission), None)
            if not doc or not doc.get("tracking") or doc.get("carrier") not in ("UPS", "FedEx", "DHL"):
                raise Problem("Save a UPS, FedEx or DHL tracking number before checking live tracking")
            key = (doc["carrier"], doc["tracking"])
            with tracking_lock:
                cached = tracking_cache.get(key)
                if cached and time.time() - cached[0] < 300:
                    return cached[1]
                if carrier_tracker is None:
                    from carriers import CarrierTracker
                    carrier_tracker = CarrierTracker()
                result = carrier_tracker.track(doc["tracking"], doc["carrier"].lower())
                result["checked_at"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
                if len(tracking_cache) > 200:
                    tracking_cache.clear()
                tracking_cache[key] = (time.time(), result)
                return result  # transport status does not change physical receipt/fulfillment
        return respond(lookup)

    @app.after_request
    def fulfillment_no_cache(response):
        if request.path.startswith("/api/fulfillment"):
            response.headers["Cache-Control"] = "no-store"
        return response
