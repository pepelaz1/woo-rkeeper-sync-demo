import json
import tempfile
import unittest
from pathlib import Path
from bridge import Bridge, ROOT, demo_order, signature


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.sqlite'
        self.b=Bridge(self.path)
        self.snapshot=json.loads((ROOT/'mock_snapshot.json').read_text())
        self.b.sync(self.snapshot)
    def tearDown(self):
        self.b.close(); self.tmp.cleanup()
    def receive(self,order=None,delivery='d1'):
        raw=json.dumps(order or demo_order()).encode()
        return self.b.receive(raw,signature(raw,'secret'),delivery,'secret')
    def test_upsert_price_without_duplicates(self):
        self.snapshot['menu'][0]['price']='600.00'; self.b.sync(self.snapshot)
        self.assertEqual(len(self.b.state()['products']),3)
        self.assertEqual(self.b.state()['products'][0]['price'],'600.00')
    def test_removed_product_deactivated(self):
        self.snapshot['menu'].pop(); self.b.sync(self.snapshot)
        self.assertEqual(sum(p['active'] for p in self.b.state()['products']),2)
    def test_snapshot_rollback(self):
        self.snapshot['menu'][0]['name']=''
        with self.assertRaises(ValueError): self.b.sync(self.snapshot)
        self.assertEqual(sum(p['active'] for p in self.b.state()['products']),3)
    def test_bad_signature(self):
        with self.assertRaises(PermissionError): self.b.receive(b'{}','bad','d1','secret')
        self.assertEqual(self.b.state()['jobs'],[])
    def test_replay_new_delivery_same_order(self):
        self.receive(); self.assertTrue(self.receive(delivery='d2')['duplicate'])
        self.assertEqual(len(self.b.state()['jobs']),1)
    def test_changed_payload_rejected(self):
        self.receive(); o=demo_order(); o['total']='1530.00'; o['zone_id']='north'
        with self.assertRaises(ValueError): self.receive(o,delivery='d2')
    def test_unknown_sku_and_mismatched_money(self):
        o=demo_order(); o['line_items'][0]['sku']='unknown'
        with self.assertRaises(ValueError): self.receive(o)
        o=demo_order(); o['total']='1.00'
        with self.assertRaises(ValueError): self.receive(o)
    def test_lost_ack_restart_no_duplicate(self):
        self.receive(); self.b.work(lose_ack=True); self.b.close(); self.b=Bridge(self.path)
        self.assertEqual(self.b.work()['result'],'sent')
        s=self.b.state(); self.assertEqual(len(s['mock_orders']),1)
        self.assertEqual(s['jobs'][0]['attempts'],2)
    def test_retry_limit(self):
        self.receive()
        for _ in range(3): self.b.work(fail=True)
        self.assertEqual(self.b.state()['jobs'][0]['status'],'manual')
        self.assertEqual(self.b.work()['result'],'empty')
    def test_invalid_quantity(self):
        for q in (0,-1,True,1.2):
            o=demo_order(); o['line_items'][0]['quantity']=q
            with self.assertRaises(ValueError): self.receive(o)


if __name__=='__main__': unittest.main()
