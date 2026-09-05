import unittest
from test_fulfillment import rows, payload
from fulfillment import inventory, build_manifest, Problem
from fulfillment_web import packing_html


class BoxDetailsTests(unittest.TestCase):
    def test_address_and_tracking_survive_preview(self):
        p = payload()
        p['address'] = 'Edited recipient\n12 Example Road\nUS'
        p['boxes'] = [dict(box=1, carrier='UPS', method='Ground', tracking='TRACK111'),
                      dict(box=2, carrier='DHL', method='Express', tracking='TRACK222')]
        doc = build_manifest(p, inventory(rows(), []), {}, 'Test')
        self.assertEqual(doc['address'], p['address'])
        self.assertEqual(doc['boxes'], p['boxes'])
        html = packing_html(doc)
        for value in ('Edited recipient', 'TRACK111', 'TRACK222', 'Express'):
            self.assertIn(value, html)

    def test_duplicate_box_rejected(self):
        p = payload()
        p['address'] = 'Example address'
        p['boxes'] = [dict(box=1), dict(box=1)]
        with self.assertRaises(Problem):
            build_manifest(p, inventory(rows(), []), {}, 'Test')
