import unittest
from unittest.mock import patch
from flask import Flask
from base_access_gate import register, imported_shipments
import lark_auth


class GateTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        register(self.app)
        self.client = self.app.test_client()

    def test_shared_tokens_cannot_access_data(self):
        for path in ['/api/fulfillment/catalog', '/api/fulfillment/photo?key=x',
                     '/packing-list', '/dashboard/legacy', '/api/shipments']:
            result = self.client.get(path, headers={'X-Dashboard-Token': 'shared'})
            self.assertEqual(result.status_code, 401)
            self.assertIn('no-store', result.headers['Cache-Control'])

    def test_invalid_oauth_state_rejected(self):
        self.assertEqual(self.client.get('/auth/lark/callback?code=x&state=x').status_code, 403)

    def test_health(self):
        self.assertEqual(self.client.get('/dashboard/health').json, {'ok': True})

    @patch('base_access_gate.lark_auth.delegated_token', return_value='user-token')
    def test_verified_identity_does_not_unlock_legacy_or_exports(self, token):
        for path in ['/packing-list', '/fulfillment/packing-list/x', '/api/shipments', '/dashboard/legacy']:
            self.assertEqual(self.client.get(path).status_code, 403)
        result = self.client.get('/dashboard?status=flagged')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Shipping navigation', result.data)

    @patch('base_access_gate.lark_auth.delegated_token', return_value='user-A')
    @patch('base_access_gate.read_user_rows', return_value=[])
    @patch.dict('os.environ', {'FULFILLMENT_CONFIG':'{"base_token":"base","sources":[]}'})
    def test_catalog_uses_user_token_and_no_shared_data(self, rows, token):
        result = self.client.get('/api/fulfillment/catalog')
        self.assertEqual(result.status_code, 200)
        rows.assert_called_once_with('user-A', {'base_token':'base', 'sources':[]})
        self.assertEqual(result.json['items'], [])
        self.assertFalse(result.json['can_save'])

    @patch('lark_auth.exchange_code', return_value='secret-user-token')
    @patch('lark_auth.user_info', return_value={'open_id':'A','name':'Test'})
    def test_token_never_in_cookie(self, info, exchange):
        user = lark_auth.sign_in('code','url')
        self.assertNotIn('secret-user-token', str(user))
        self.assertEqual(lark_auth.delegated_token(user), 'secret-user-token')
        self.assertIsNone(lark_auth.delegated_token(dict(user, open_id='B')))

    def test_backfill_is_stable_and_does_not_change_order_balance(self):
        item = {'key':'tblA:rec1', 'table_id':'tblA', 'record_id':'rec1',
                'order':'SO-1', 'customer':'Client', 'product':'Hat',
                'address':'US warehouse', 'source':'Production', 'source_url':'https://example.com',
                'photos':[], 'photo_field_id':'', 'ordered_quantity':10,
                'quantity_shipped':4, 'tracking':'TRACK123', 'carrier':'DHL',
                'method':'DDP Air', 'date_shipped':'2026-09-01',
                'china_available':0, 'us_available':0, 'issues':[]}
        first = imported_shipments([item])
        second = imported_shipments([item])
        self.assertEqual(first[0]['submission_id'], second[0]['submission_id'])
        self.assertEqual(first[0]['units'], 4)
        self.assertEqual(first[0]['tracking'], 'TRACK123')
        self.assertTrue(first[0]['imported'])
        self.assertEqual(item['quantity_shipped'], 4)

    def test_backfill_skips_unshipped_cards(self):
        self.assertEqual(imported_shipments([{'quantity_shipped':0}]), [])


if __name__ == '__main__':
    unittest.main()
