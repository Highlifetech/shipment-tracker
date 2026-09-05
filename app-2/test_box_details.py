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

    def test_extra_item_prints_without_changing_order_lines(self):
        p = payload()
        p['address'] = 'US warehouse'
        p['extras'] = [dict(box=1, description='Spare hangtags', qty=12)]
        doc = build_manifest(p, inventory(rows(), []), {}, 'Test')
        self.assertEqual(len(doc['lines']), len(p['lines']))
        self.assertEqual(doc['extras'][0]['description'], 'Spare hangtags')
        self.assertEqual(doc['units'], sum(line['qty'] for line in doc['lines']) + 12)
        html = packing_html(doc)
        self.assertIn('Spare hangtags', html)
        self.assertIn('Not linked to a sales order', html)
