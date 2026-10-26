"""Loopback-only demo dashboard and signed webhook entry point, Python 3.9+."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from bridge import Bridge, ROOT, demo_order, signature

SECRET = 'local-demo-secret-not-a-production-credential'
DB = os.getenv('DEMO_DB', str(ROOT/'demo.sqlite'))


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, value, kind='application/json'):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/':
            return self.reply(200,(ROOT/'index.html').read_bytes(),'text/html')
        if self.path == '/api/state':
            bridge = Bridge(DB)
            try: self.reply(200,bridge.state())
            finally: bridge.close()
        else: self.reply(404,{'error':'Not found'})

    def do_POST(self):
        # Reject foreign browser origins. Only local demo, no public deployment or credentials.
        origin = self.headers.get('Origin')
        if origin and origin != 'http://'+self.headers.get('Host',''):
            return self.reply(403,{'error':'Foreign origin'})
        size = int(self.headers.get('Content-Length','0'))
        if size > 65536:
            return self.reply(413,{'error':'Body too large'})
        raw = self.rfile.read(size)
        bridge = Bridge(DB)
        try:
            if self.path == '/api/sync':
                bridge.sync(json.loads((ROOT/'mock_snapshot.json').read_text()))
                result = {'result':'Menu, prices and zones synchronized from mock snapshot'}
            elif self.path == '/api/demo-order':
                body = json.dumps(demo_order()).encode()
                result = bridge.receive(body,signature(body,SECRET),'demo-delivery-1042',SECRET)
            elif self.path == '/webhooks/woocommerce':
                result = bridge.receive(raw,self.headers.get('X-WC-Webhook-Signature',''),
                                        self.headers.get('X-WC-Webhook-Delivery-ID',''),SECRET)
            elif self.path in ('/api/work','/api/fail','/api/lose-ack'):
                result = bridge.work(fail=self.path.endswith('/fail'),lose_ack=self.path.endswith('/lose-ack'))
            else:
                return self.reply(404,{'error':'Not found'})
            self.reply(200,result)
        except PermissionError as e: self.reply(401,{'error':str(e)})
        except (ValueError, KeyError, TypeError) as e: self.reply(400,{'error':str(e)})
        finally: bridge.close()


if __name__ == '__main__':
    port = int(os.getenv('PORT','8781'))
    print(f'Demo only: http://127.0.0.1:{port}', flush=True)
    HTTPServer(('127.0.0.1',port),Handler).serve_forever()
