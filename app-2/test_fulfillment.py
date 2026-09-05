"""Run: python -m unittest -v test_fulfillment (no network or production writes)."""
import copy
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from flask import Flask
from fulfillment import Coordinator, FulfillmentService, Problem, inventory, build_manifest, integer
from fulfillment_base import BaseError, BaseStore, text
from fulfillment_web import register, packing_html


def rows():
    return [{"key": "tblOne:recA", "table_id": "tblOne", "record_id": "recA",
             "source": "Original orders", "source_url": "https://example.org/base?record=recA",
             "order": "SO-1048", "customer": "Customer A", "product": "Blue hat",
             "opening_china": 100, "opening_us": 0, "ready": True,
             "address": "Customer A\n10 Test Street\nNew York NY 10001", "photos": []},
            {"key": "tblOne:recB", "table_id": "tblOne", "record_id": "recB",
             "source": "Original orders", "source_url": "https://example.org/base?record=recB",
             "order": "SO-1049", "customer": "Customer B", "product": "Black tote",
             "opening_china": 50, "opening_us": 0, "ready": True,
             "address": "Customer B\n20 Example Street\nBoston MA 02101", "photos": []}]


def payload(qty=40, route="china_to_us"):
    return {"submission_id": str(uuid.uuid4()), "route": route, "box_count": 2,
            "batch_ref": "1048", "carrier": "UPS", "tracking": "1ZTEST123456",
            "lines": [{"key": "tblOne:recA", "box": 1, "qty": qty // 2},
                      {"key": "tblOne:recA", "box": 2, "qty": qty - qty // 2}]}


class MemoryStore:
    """Test double for Lark, never used by configured_service/production."""
    table = "tblShipments"
    encode = staticmethod(BaseStore.encode)

    def __init__(self):
        self.rows = rows()
        self.docs = []
        self.creates = 0
        self.fail = ""

    def source_rows(self):
        return copy.deepcopy(self.rows)

    def shipments(self):
        return copy.deepcopy(self.docs)

    def record_link(self, table, record):
        return "https://example.org/base?table=" + table + "&record=" + record

    def create(self, doc):
        self.creates += 1
        if self.fail == "before":
            raise BaseError("Connection lost")
        doc = copy.deepcopy(doc)
        record = "recShipment" + str(self.creates)
        doc.update(record_id=record, lark_url=self.record_link(self.table, record))
        self.docs.append(doc)
        if self.fail == "after":
            raise BaseError("Response lost after write")
        return record

    def update(self, doc):
        self.docs = [copy.deepcopy(doc) if s["record_id"] == doc["record_id"] else s for s in self.docs]


class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore()
        self.settings = {"warehouse_address": "TEST US RECEIVING ADDRESS"}
        self.svc = FulfillmentService(self.store, self.settings, Coordinator(self.tmp.name))

    def test_strict_quantities(self):
        for invalid in (None, "", True, -1, 1.2, "3e2", "1,000", "NaN", "10 pieces"):
            with self.subTest(invalid=invalid), self.assertRaises(Problem):
                integer(invalid, "Quantity")
        self.assertEqual(integer("12.0", "Quantity"), 12)

    def test_source_review_no_description_guess(self):
        self.store.rows[0].update(opening_china=None, product="120 pins", ready=True)
        self.assertTrue(self.svc.read()[0][0]["issues"])
        with self.assertRaises(Problem):
            self.svc.preview(payload(), "Tester")

    def test_readiness_required(self):
        self.store.rows[0]["ready"] = "yes"
        with self.assertRaisesRegex(Problem, "Ready to Pack"):
            self.svc.preview(payload(), "Tester")

    def test_split_item_and_partial_shipment(self):
        doc = self.svc.save(payload(), "Tester")
        self.assertEqual(doc["units"], 40)
        self.assertEqual(doc["box_count"], 2)
        self.assertEqual(self.svc.read()[0][0]["china_available"], 60)
        self.assertEqual(self.svc.read()[0][0]["us_available"], 0)
        self.assertEqual(doc["status"], "Packed")

    def test_allocations_sum_before_validation(self):
        with self.assertRaisesRegex(Problem, "only 100 available"):
            self.svc.preview(payload(120), "Tester")

    def test_duplicate_item_in_same_box(self):
        p = payload()
        p["lines"][1]["box"] = 1
        with self.assertRaisesRegex(Problem, "twice in one box"):
            self.svc.preview(p, "Tester")

    def test_unknown_item_and_empty_boxes(self):
        p = payload()
        p["lines"][0]["key"] = "missing"
        with self.assertRaisesRegex(Problem, "no longer exists"):
            self.svc.preview(p, "Tester")
        p = payload()
        p["box_count"] = 3
        with self.assertRaisesRegex(Problem, "Every box"):
            self.svc.preview(p, "Tester")

    def test_mixed_customers_only_inbound(self):
        p = payload()
        p["lines"][1]["key"] = "tblOne:recB"
        self.assertEqual(self.svc.preview(p, "Tester")["units"], 40)
        p["route"] = "china_to_customer"
        with self.assertRaisesRegex(Problem, "one customer"):
            self.svc.preview(p, "Tester")

    def test_same_customer_different_address(self):
        p = payload(route="china_to_customer")
        self.store.rows[1]["customer"] = "Customer A"
        p["lines"][1]["key"] = "tblOne:recB"
        with self.assertRaisesRegex(Problem, "different/missing addresses"):
            self.svc.preview(p, "Tester")

    def test_us_reship_only_after_receipt(self):
        inbound = self.svc.save(payload(), "China")
        p = payload(route="us_to_customer")
        with self.assertRaises(Problem):
            self.svc.preview(p, "US")
        self.svc.transition(inbound["submission_id"], "ship", {"carrier": "UPS", "tracking": "1ZTEST123"}, "China")
        with self.assertRaises(Problem):
            self.svc.preview(p, "US")
        self.svc.transition(inbound["submission_id"], "receive", {}, "US")
        outbound = self.svc.save(p, "US")
        self.assertEqual(outbound["units"], 40)
        self.assertEqual(self.svc.read()[0][0]["us_available"], 0)
        self.assertEqual(self.svc.read()[0][0]["china_available"], 60)

    def test_direct_cannot_be_received_in_us(self):
        doc = self.svc.save(payload(route="china_to_customer"), "China")
        self.svc.transition(doc["submission_id"], "ship", {"carrier": "DHL", "tracking": "12345678"}, "China")
        with self.assertRaises(Problem):
            self.svc.transition(doc["submission_id"], "receive", {}, "US")

    def test_cancel_releases_and_is_idempotent(self):
        doc = self.svc.save(payload(), "Tester")
        for _ in range(2):
            self.svc.transition(doc["submission_id"], "cancel", {}, "Tester")
        self.assertEqual(self.svc.read()[0][0]["china_available"], 100)
        with self.assertRaises(Problem):
            self.svc.transition(doc["submission_id"], "ship", {}, "Tester")

    def test_no_customer_order_group_merge(self):
        self.store.rows[1].update(order="SO-1048", customer="Customer A", product="Blue hat")
        self.svc.save(payload(), "Tester")
        self.assertEqual(self.svc.read()[0][1]["china_available"], 50)

    def test_idempotent_save_and_hash_conflict(self):
        p = payload()
        first = self.svc.save(p, "Tester")
        again = self.svc.save(p, "Tester")
        self.assertEqual(first["record_id"], again["record_id"])
        self.assertEqual(self.store.creates, 1)
        p["batch_ref"] = "changed"
        with self.assertRaisesRegex(Problem, "different contents"):
            self.svc.save(p, "Tester")

    def test_ambiguous_save_never_blindly_retries(self):
        self.store.fail = "before"
        p = payload()
        with self.assertRaises(BaseError):
            self.svc.save(p, "Tester")
        self.store.fail = ""
        with self.assertRaisesRegex(Problem, "unconfirmed"):
            self.svc.save(p, "Tester")
        self.assertEqual(self.store.creates, 1)

    def test_lost_response_reconciles_on_retry_after_restart(self):
        self.store.fail = "after"
        p = payload()
        with self.assertRaises(BaseError):
            self.svc.save(p, "Tester")
        self.store.fail = ""
        restarted = FulfillmentService(self.store, self.settings, Coordinator(self.tmp.name))
        self.assertEqual(restarted.save(p, "Tester")["units"], 40)
        self.assertEqual(self.store.creates, 1)

    def test_external_delete_does_not_free_stock_for_save(self):
        self.svc.save(payload(), "Tester")
        self.store.docs.clear()
        with self.assertRaisesRegex(Problem, "changed or removed"):
            self.svc.save(payload(), "Tester")

    def test_parallel_saves_cannot_overallocate(self):
        outcomes = []
        def run():
            try:
                self.svc.save(payload(75), "Tester")
                outcomes.append("saved")
            except Problem:
                outcomes.append("rejected")
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(outcomes, ["saved", "rejected"])
        self.assertEqual(self.svc.read()[0][0]["china_available"], 25)

    def test_packing_snapshot_and_html_escaping(self):
        p = payload()
        p["notes"] = '<script>alert("XSS")</script>'
        doc = self.svc.save(p, "Tester")
        self.store.rows[0]["product"] = "Updated item"
        html = packing_html(self.store.shipments()[0], saved=True)
        self.assertIn("Blue hat", html)
        self.assertNotIn('<script>', html)
        self.assertIn("Box 2 of 2", html)
        self.assertIn("SAVED", html)
        self.assertIn('Manifest JSON', BaseStore.encode(doc))

    def test_auth_and_csrf_and_no_cache(self):
        app = Flask(__name__)
        from flask import request
        register(app, None, lambda: request.headers.get("X-Dashboard-Token") == "test",
                 lambda: None, service=self.svc)
        client = app.test_client()
        self.assertEqual(client.get('/api/fulfillment/catalog').status_code, 403)
        headers = {"X-Dashboard-Token": "test"}
        self.assertEqual(client.post('/api/fulfillment/shipments', json=payload(), headers=headers).status_code, 403)
        response = client.get('/api/fulfillment/catalog', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['synced_at'].endswith('+00:00'))
        self.assertFalse(response.json['catalog_only'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        headers['X-Fulfillment-Request'] = '1'
        response = client.post('/api/fulfillment/shipments', json=payload(), headers=headers)
        self.assertEqual(response.status_code, 200)
        packing = client.get(response.json['packing_url'], headers=headers)
        self.assertIn(b'Box 2 of 2', packing.data)

    def test_preview_no_writes_and_live_save_disabled(self):
        service = FulfillmentService(self.store, self.settings)
        service.preview(payload(), "Tester")
        self.assertEqual(self.store.creates, 0)
        with self.assertRaises(Problem): service.save(payload(), "Tester")

    def test_pagination_and_bad_cursor(self):
        store = BaseStore(None, {"base_token": "abc", "shipment_table": "tblTest"})
        with patch.object(store, 'api', side_effect=[{'items': [1], 'has_more': True, 'page_token': 'p2'}, {'items': [2], 'has_more': False}]) as api:
            self.assertEqual(store.records('tblTest'), [1, 2])
            self.assertEqual(api.call_args.kwargs['params']['page_token'], 'p2')
        with patch.object(store, 'api', return_value={'items': [], 'has_more': True}):
            with self.assertRaises(BaseError): store.records('tblTest')

    def test_rich_text(self):
        self.assertEqual(text([{'text': 'SO-'}, {'text': '1048'}]), 'SO-1048')
        self.assertEqual(text([{'text': 'Customer '}, {'text': 'A'}]), 'Customer A')

    def test_base_manifest_round_trip_and_corruption(self):
        doc = self.svc.preview(payload(), "Tester")
        fields = BaseStore.encode(doc)
        store = BaseStore(None, {"base_token": "abc", "shipment_table": "tblTest"})
        with patch.object(store, 'records', return_value=[{'record_id': 'recSaved', 'fields': fields}]):
            self.assertEqual(store.shipments()[0]['units'], 40)
            fields['Status'] = 'Shipped'
            with self.assertRaises(BaseError): store.shipments()

    def test_on_demand_tracking_uses_saved_number_and_caches(self):
        app = Flask(__name__)
        doc = self.svc.save(payload(), 'Tester')
        register(app, None, lambda: True, lambda: None, self.svc)
        client = app.test_client()
        with patch('carriers.CarrierTracker') as tracker:
            tracker.return_value.track.return_value = {'status': 'In transit', 'error': ''}
            endpoint = '/api/fulfillment/shipments/' + doc['submission_id'] + '/tracking'
            for _ in range(2): self.assertEqual(client.get(endpoint).status_code, 200)
            tracker.return_value.track.assert_called_once_with('1ZTEST123456', 'ups')
        self.assertEqual(self.store.docs[0]['status'], 'Packed')

    def test_photo_requires_known_source_or_manifest_line(self):
        app = Flask(__name__)
        register(app, None, lambda: True, lambda: None, self.svc)
        client = app.test_client()
        self.assertEqual(client.get('/api/fulfillment/photo?key=attacker-token').status_code, 404)

    def test_primary_layout_requires_user_and_blocks_legacy(self):
        import dashboard
        from types import SimpleNamespace
        app = Flask(__name__)
        chat = SimpleNamespace(_SNAPSHOT={'results': [], 'ts': 0}, update_snapshot=lambda results: None)
        dashboard.register(app, chat, lambda **kwargs: [], None, fulfillment_service=self.svc)
        client = app.test_client()
        with patch('base_access_gate.lark_auth.delegated_token', return_value='user'):
            self.assertEqual(client.get('/dashboard').status_code, 200)
            self.assertEqual(client.get('/dashboard/legacy').status_code, 403)
            self.assertIn(b'id="overviewPage"', client.get('/dashboard?status=in_transit').data)

if __name__ == '__main__':
    unittest.main()
