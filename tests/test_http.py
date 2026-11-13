import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from run import Handler
from store import Store


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.store=Store(Path(self.temp.name)/'db.sqlite3')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,method,path,body=None,headers=None):
        client=HTTPConnection(*self.server.server_address,timeout=3)
        client.request(method,path,None if body is None else json.dumps(body),headers or {'Content-Type':'application/json'})
        response=client.getresponse();raw=response.read();status=response.status
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        client.close()
        return status,json.loads(raw) if raw and response.headers['Content-Type'].startswith('application/json') else raw

    def test_full_http_lifecycle_and_conflicts(self):
        self.assertEqual(self.request('POST','/api/resources',{'name':'gpu','capacity':2})[0],201)
        body={'request_key':'run1','start':10,'end':20,'units':2,'note':'training'}
        self.assertTrue(self.request('POST','/api/resources/gpu/preview',body)[1]['fits'])
        self.assertEqual(self.request('POST','/api/resources/gpu/reserve',body)[0],201)
        self.assertTrue(self.request('POST','/api/resources/gpu/reserve',body)[1]['replayed'])
        self.assertEqual(self.request('POST','/api/resources/gpu/reserve',{**body,'request_key':'run2'})[0],409)
        self.assertEqual(self.request('POST','/api/resources/gpu/cancel/1',{'expected_version':1})[1]['version'],2)
        self.assertEqual(self.request('POST','/api/resources/gpu/cancel/1',{'expected_version':1})[0],409)
        self.assertEqual(self.request('GET','/api/resources/gpu')[1]['peak'],0)

    def test_boundary_rejections_and_static_allowlist(self):
        for path in ['/store.py','/data/slots.sqlite3','/.git/config','/../run.py']:
            self.assertEqual(self.request('GET',path)[0],404)
        self.assertEqual(self.request('HEAD','/')[1],b'')
        self.assertEqual(self.request('GET','/',headers={'Host':'external.invalid'})[0],400)
        self.assertEqual(self.request('POST','/api/resources',{}, {'Content-Type':'application/json','Origin':'https://other.invalid'})[0],403)
        self.assertEqual(self.request('POST','/api/resources',{}, {'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('POST','/api/resources',{'name':'x'*9000})[0],413)
        self.assertEqual(self.request('PUT','/api/resources',{})[0],405)
