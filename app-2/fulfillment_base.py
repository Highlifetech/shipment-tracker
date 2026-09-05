"""Lark Base adapter. No writes to production orders or legacy shipping sheets."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests


class BaseError(RuntimeError):
    pass


SHIPMENT_FIELDS = [
    "Shipment ID", "Submission ID", "Request Hash", "Status", "Route",
    "Destination", "Sales Orders", "Customers", "Tracking", "Carrier",
    "Created By", "Created At", "Packing Summary", "Manifest JSON",
]


def text(value):
    """Decode Base text/rich-text/lookup values without inventing numbers."""
    def flatten(v):
        if v is None:
            return ""
        if isinstance(v, list):
            return "".join(flatten(part) for part in v)
        if isinstance(v, dict):
            return flatten(v.get("text", v.get("name", v.get("value", ""))))
        return str(v)
    return flatten(value).strip()


def ident(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
        raise BaseError("Invalid Lark identifier in fulfillment configuration")
    return value


class BaseStore:
    def __init__(self, lark, settings):
        self.lark = lark
        self.settings = settings
        self.base = ident(settings["base_token"])
        self.table = ident(settings["shipment_table"]) if settings.get("shipment_table") else None
        if not self.table and not settings.get("catalog_only"):
            raise BaseError("Dedicated shipment table is required")
        self.root = "/open-apis/bitable/v1/apps/" + self.base

    def api(self, method, path, **kwargs):
        # Do not retry mutations: the caller journals and reconciles uncertain writes.
        try:
            response = requests.request(method, self.lark.base_url + path,
                                        headers=self.lark._headers(), timeout=45, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise BaseError("Lark request failed; check connectivity and app permissions") from exc
        if payload.get("code") != 0:
            error = BaseError("Lark API error %s; check Base access and field mapping" % payload.get("code"))
            error.code = payload.get('code')
            raise error
        return payload.get("data", {})

    def pages(self, path):
        result, seen, cursor = [], set(), None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["page_token"] = cursor
            data = self.api("GET", path, params=params)
            result.extend(data.get("items", []))
            if not data.get("has_more"):
                return result
            cursor = data.get("page_token")
            if not cursor or cursor in seen:
                raise BaseError("Incomplete Lark pagination; refusing partial inventory")
            seen.add(cursor)

    def records(self, table):
        return self.pages(self.root + "/tables/" + ident(table) + "/records")

    def fields(self, table):
        return self.pages(self.root + "/tables/" + ident(table) + "/fields")

    def record_link(self, table, record):
        origin = self.settings.get("base_web_url", "https://off-menu.jp.larksuite.com/base/" + self.base)
        return origin + "?table=" + quote(table) + "&record=" + quote(record)

    def source_rows(self):
        sources = [s for s in self.settings['sources']
                   if not re.search(r'quote|quotation', s.get('name', ''), re.I)]
        tables = [ident(s['table_id']) for s in sources]
        if len(set(tables)) != len(tables) or self.table in tables:
            raise BaseError('A source table is duplicated or is the shipment table')
        if len(sources) > 1:
            # Warm authentication before bounded concurrent read-only table requests.
            self.lark._headers()
            def read_source(source):
                settings = dict(self.settings, sources=[source])
                return BaseStore(self.lark, settings).source_rows()
            with ThreadPoolExecutor(max_workers=3) as pool:
                batches = list(pool.map(read_source, sources))
            return [row for batch in batches for row in batch]
        rows, seen_tables = [], set()
        for source in sources:
            table = ident(source["table_id"])
            if table in seen_tables or table == self.table:
                raise BaseError("A source table is duplicated or is the shipment table")
            seen_tables.add(table)
            mapping = source["fields"]
            metadata = self.fields(table)
            actual = {f["field_name"] for f in metadata}
            required = ("order", "customer") if self.settings.get("catalog_only") else ("order", "customer", "product", "opening_china", "ready")
            missing = [mapping.get(k, k) for k in required if mapping.get(k) not in actual]
            if missing:
                raise BaseError("%s: missing mapped fields: %s" % (source["name"], ", ".join(missing)))
            for record in self.records(table):
                values = record.get("fields", {})
                row = {key: values.get(name) for key, name in mapping.items()}
                if not text(row.get('order')):
                    continue
                row['ordered_quantity'] = values.get(mapping.get('ordered_quantity', 'Quantity'))
                row['quantity_shipped'] = values.get(mapping.get('quantity_shipped', 'Quantity Shipped'))
                row['tracking'] = values.get(mapping.get('tracking', 'Tracking Number'))
                row['carrier'] = values.get(mapping.get('carrier', 'Carrier'))
                row['method'] = values.get(mapping.get('method', 'Shipment Method'))
                row['date_shipped'] = values.get(mapping.get('date_shipped', 'Date Shipped'))
                row.update(key=table + ":" + record["record_id"], table_id=table,
                           record_id=record["record_id"], source=source["name"],
                           source_url=self.record_link(table, record["record_id"]))
                # Prefer artwork over production-progress photos. Keep the field ID
                # paired with the selected attachment for Lark's download permission.
                candidates = [mapping.get("artwork"), "Production Artwork",
                              "Approved Production Artwork", "Production Approved Artwork"]
                photo_name = next((name for name in candidates if name and
                                   isinstance(values.get(name), list) and
                                   any(isinstance(f, dict) and f.get("file_token")
                                       for f in values[name])), None)
                row["photo_field_id"] = next((f["field_id"] for f in metadata
                                             if f["field_name"] == photo_name), "")
                photo = values.get(photo_name) or []
                photo = sorted(photo, key=lambda f: not bool(re.search(
                    r"\.(png|jpe?g|webp|gif)$", f.get("name", ""), re.I))
                    if isinstance(f, dict) else True)
                row["photos"] = [{"file_token": f["file_token"], "name": f.get("name", "Photo")}
                                 for f in photo if isinstance(f, dict) and f.get("file_token")]
                rows.append(row)
        return rows

    def shipments(self):
        if self.settings.get("catalog_only"):
            return []  # Explicit read-only setup mode; never claims shipment history was imported.
        shipments = []
        seen = set()
        for row in self.records(self.table):
            fields = row.get("fields", {})
            raw = text(fields.get("Manifest JSON"))
            if not raw and not text(fields.get("Submission ID")):
                continue  # unused blank grid rows
            try:
                doc = json.loads(raw)
                assert doc["schema"] == 1 and doc["submission_id"] not in seen
                assert doc["submission_id"] == text(fields.get("Submission ID"))
                assert doc["status"] in ("Packed", "Shipped", "Received", "Cancelled")
                assert doc["status"] == text(fields.get("Status"))
                assert doc["request_hash"] == text(fields.get("Request Hash"))
                assert doc["route"] in ("china_to_us", "china_to_customer", "us_to_customer")
                assert isinstance(doc["lines"], list) and doc["lines"]
                assert type(doc["box_count"]) is int and 1 <= doc["box_count"] <= 30
                for line in doc["lines"]:
                    assert type(line["qty"]) is int and line["qty"] > 0
                    assert type(line["box"]) is int and 1 <= line["box"] <= doc["box_count"]
                    assert line["key"] == line["table_id"] + ":" + line["record_id"]
                assert isinstance(doc.get("extras", []), list)
                for extra in doc.get("extras", []):
                    assert type(extra["qty"]) is int and extra["qty"] > 0
                    assert type(extra["box"]) is int and 1 <= extra["box"] <= doc["box_count"]
                    assert isinstance(extra["description"], str) and extra["description"]
                assert doc["units"] == sum(line["qty"] for line in doc["lines"]) + sum(extra["qty"] for extra in doc.get("extras", []))
            except (ValueError, TypeError, KeyError, AssertionError) as exc:
                raise BaseError("Shipment data is inconsistent. Resolve it before packing more orders.") from exc
            seen.add(doc["submission_id"])
            doc["record_id"] = row["record_id"]
            doc["lark_url"] = self.record_link(self.table, row["record_id"])
            shipments.append(doc)
        return shipments

    @staticmethod
    def encode(doc):
        persisted = {k: v for k, v in doc.items() if k not in ("record_id", "lark_url")}
        raw = json.dumps(persisted, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 90000:
            raise BaseError("Shipment is too large for one manifest; split this batch")
        summary = "\n".join("Box %s · %s · %s · %s × %s" %
                            (l["box"], l["customer"], l["order"], l["product"], l["qty"])
                            for l in doc["lines"])
        if doc.get("extras"):
            summary += ("\n" if summary else "") + "\n".join(
                "Box %s · Extra item · %s × %s" % (x["box"], x["description"], x["qty"])
                for x in doc["extras"])
        return {"Shipment ID": doc["shipment_id"], "Submission ID": doc["submission_id"],
                "Request Hash": doc["request_hash"], "Status": doc["status"],
                "Route": doc["route"], "Destination": doc["address"],
                "Sales Orders": ", ".join(sorted({l["order"] for l in doc["lines"]})),
                "Customers": ", ".join(sorted({l["customer"] for l in doc["lines"]})),
                "Tracking": doc.get("tracking", ""), "Carrier": doc.get("carrier", ""),
                "Created By": doc["created_by"], "Created At": doc["created_at"],
                "Packing Summary": summary, "Manifest JSON": raw}

    def create(self, doc):
        data = self.api("POST", self.root + "/tables/" + self.table + "/records",
                        json={"fields": self.encode(doc)})
        return data["record"]["record_id"]

    def update(self, doc):
        self.api("PUT", self.root + "/tables/" + self.table + "/records/" + ident(doc["record_id"]),
                 json={"fields": self.encode(doc)})

    def photo(self, line):
        """Only accept a token from a server-read order/saved manifest, never a client URL."""
        if not line.get("photos"):
            raise BaseError("No project photo attached; use the source record link")
        token = ident(line["photos"][0]["file_token"])
        url = self.lark.base_url + "/open-apis/drive/v1/medias/" + token + "/download"
        extra = {"bitablePerm": {"tableId": line["table_id"],
                 "attachments": {line["photo_field_id"]: {line["record_id"]: [token]}}}} if line.get("photo_field_id") else None
        params = {"extra": json.dumps(extra)} if extra else {}
        with requests.get(url, headers=self.lark._headers(), params=params,
                          timeout=30, stream=True) as response:
            response.raise_for_status()
            mime = response.headers.get("Content-Type", "").split(";")[0]
            if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                raise BaseError("Photo unavailable in a supported image format")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise BaseError("Photo exceeds preview size limit; open its Lark record")
                chunks.append(chunk)
            return b"".join(chunks), mime
