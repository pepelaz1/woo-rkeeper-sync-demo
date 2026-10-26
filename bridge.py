"""Local integration laboratory. Mock RK adapter, not a production RK driver."""
import base64
import hashlib
import hmac
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent


def signature(body, secret):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


class Bridge:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS products(sku TEXT PRIMARY KEY, name TEXT, price TEXT, active INTEGER);
        CREATE TABLE IF NOT EXISTS zones(id TEXT PRIMARY KEY, name TEXT, fee TEXT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, digest TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, external_id TEXT UNIQUE,
          payload TEXT, digest TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS mock_orders(external_id TEXT PRIMARY KEY, receipt TEXT, payload TEXT);
        ''')

    def close(self):
        self.db.close()

    def sync(self, snapshot):
        """Complete mock snapshot; all validation occurs before atomic replacement."""
        products, zones = snapshot['menu'], snapshot['zones']
        if len({p['sku'] for p in products}) != len(products):
            raise ValueError('Duplicate menu SKU')
        if len({z['id'] for z in zones}) != len(zones):
            raise ValueError('Duplicate zone ID')
        for row in products + zones:
            amount = Decimal(str(row.get('price', row.get('fee'))))
            if not amount.is_finite() or amount < 0:
                raise ValueError('Invalid money')
        with self.db:
            self.db.execute('UPDATE products SET active=0')
            for p in products:
                if not p['sku'] or not p['name']:
                    raise ValueError('Missing product identity')
                self.db.execute('''INSERT INTO products VALUES(?,?,?,1)
                  ON CONFLICT(sku) DO UPDATE SET name=excluded.name,price=excluded.price,active=1''',
                                (p['sku'], p['name'], str(p['price'])))
            self.db.execute('DELETE FROM zones')
            self.db.executemany('INSERT INTO zones VALUES(?,?,?)',
                               [(z['id'], z['name'], str(z['fee'])) for z in zones])

    def receive(self, raw, sig, delivery, secret):
        if not secret or not hmac.compare_digest(signature(raw, secret), sig):
            raise PermissionError('Invalid WooCommerce webhook signature')
        order = json.loads(raw)
        if not isinstance(order.get('id'), int) or isinstance(order['id'], bool) or order['id'] < 1:
            raise ValueError('Invalid order id')
        if order.get('currency') != 'RUB' or order.get('status') != 'processing':
            raise ValueError('Only processing RUB orders supported in this demonstration')
        if not delivery or len(delivery) > 128:
            raise ValueError('Missing or invalid delivery ID')
        if not order.get('line_items'):
            raise ValueError('Empty order')
        lines = []
        for item in order['line_items']:
            qty = item.get('quantity')
            if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 100:
                raise ValueError('Invalid quantity')
            p = self.db.execute('SELECT * FROM products WHERE sku=? AND active=1', (item['sku'],)).fetchone()
            if p is None:
                raise ValueError('Unknown or inactive SKU')
            # Order prices are taken from a normalized catalog in this demo, not browser input.
            lines.append({'sku':p['sku'], 'quantity':qty, 'unit_price':p['price']})
        zone = self.db.execute('SELECT * FROM zones WHERE id=?', (order.get('zone_id'),)).fetchone()
        if zone is None:
            raise ValueError('Unknown delivery zone')
        expected = sum((Decimal(x['unit_price']) * x['quantity'] for x in lines), Decimal(zone['fee']))
        if Decimal(str(order['total'])) != expected:
            raise ValueError('Order/catalog total mismatch: manual reconciliation required')
        payload = json.dumps({'external_id':f"woo:{order['id']}", 'lines':lines,
                              'zone_id':zone['id'], 'total':str(expected)}, sort_keys=True)
        digest = hashlib.sha256(raw).hexdigest()
        canonical = hashlib.sha256(payload.encode()).hexdigest()
        with self.db:
            event = self.db.execute('SELECT digest FROM events WHERE id=?', (delivery,)).fetchone()
            if event and event['digest'] != digest:
                raise ValueError('Delivery ID reused with a different body')
            previous = self.db.execute('SELECT * FROM jobs WHERE external_id=?', (f"woo:{order['id']}",)).fetchone()
            if previous and previous['digest'] != canonical:
                raise ValueError('Existing order changed: manual reconciliation required')
            self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?)', (delivery, digest))
            self.db.execute('''INSERT OR IGNORE INTO jobs(external_id,payload,digest,status)
              VALUES(?,?,?,?)''', (f"woo:{order['id']}",payload,canonical,'pending'))
        return {'accepted':True, 'duplicate':previous is not None}

    def work(self, fail=False, lose_ack=False):
        """One durable job. Mock receiver deduplicates external_id, including lost ACK."""
        job = self.db.execute("SELECT * FROM jobs WHERE status IN ('pending','retry') ORDER BY id LIMIT 1").fetchone()
        if job is None:
            return {'result':'empty'}
        with self.db:
            self.db.execute('UPDATE jobs SET attempts=attempts+1 WHERE id=?', (job['id'],))
        try:
            if fail:
                raise TimeoutError('Simulated failure before delivery')
            # Separate transaction models an independent receiver accepting an external ID.
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO mock_orders VALUES(?,?,?)',
                                (job['external_id'],f"RK-DEMO-{job['id']:04}",job['payload']))
            if lose_ack:
                raise TimeoutError('Simulated lost acknowledgement after acceptance')
            with self.db:
                self.db.execute("UPDATE jobs SET status='sent',error='' WHERE id=?", (job['id'],))
            return {'result':'sent', 'external_id':job['external_id']}
        except TimeoutError as e:
            attempts = job['attempts'] + 1
            with self.db:
                self.db.execute('UPDATE jobs SET status=?,error=? WHERE id=?',
                                ('manual' if attempts >= 3 else 'retry',str(e),job['id']))
            return {'result':'retry' if attempts < 3 else 'manual', 'error':str(e)}

    def state(self):
        return {name:[dict(r) for r in self.db.execute('SELECT * FROM '+name)]
                for name in ('products','zones','jobs','mock_orders')}


def demo_order():
    return {'id':1042,'currency':'RUB','status':'processing','zone_id':'central',
            'line_items':[{'sku':'pizza-01','quantity':2},{'sku':'tea-01','quantity':1}], 'total':'1430.00'}
