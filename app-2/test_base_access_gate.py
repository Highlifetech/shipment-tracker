import unittest
from flask import Flask
from base_access_gate import register


class GateTests(unittest.TestCase):
    def test_all_shipping_paths_fail_closed(self):
        app = Flask(__name__)
        register(app)
        client = app.test_client()
        for path in ['/dashboard', '/dashboard/legacy',
                     '/fulfillment', '/api/fulfillment/catalog',
                     '/api/fulfillment/photo?key=secret', '/packing-list?tracking=secret',
                     '/fulfillment/packing-list/secret', '/api/shipping-workspace/tracking',
                     '/api/open-orders', '/api/shipments', '/api/mark-delivered']:
            for method in ['GET', 'POST']:
                with self.subTest(path=path, method=method):
                    result = client.open(path, method=method,
                                         headers={'X-Dashboard-Token': 'shared'})
                    self.assertEqual(result.status_code, 403)
                    self.assertIn('no-store', result.headers['Cache-Control'])
                    if path.startswith('/api'):
                        self.assertEqual(result.json['code'], 'BASE_PERMISSION_VERIFICATION_UNAVAILABLE')
                    else:
                        self.assertIn(b'Access paused', result.data)
                        self.assertNotIn(b'trackingKpis', result.data)

    def test_health_and_lark_callback_not_intercepted(self):
        app = Flask(__name__)
        register(app)
        self.assertEqual(app.test_client().get('/dashboard/health').json, {'ok': True, 'access': 'locked'})
        for path in ['/health', '/auth/lark/callback', '/webhook']:
            self.assertEqual(app.test_client().get(path).status_code, 404)


if __name__ == '__main__':
    unittest.main()
