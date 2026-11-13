"""Serve Slot Ledger locally with a persistent SQLite reservation store."""
import argparse
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from store import Store, Conflict, Missing

ROOT = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html','text/html'), '/app.js': ('app.js','text/javascript'), '/style.css': ('style.css','text/css')}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value, mime='application/json'):
        body = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime+'; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def handle_request(self):
        try:
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or not re.fullmatch(r'(localhost|127\.0\.0\.1)(:[0-9]{1,5})?', hosts[0], re.I):
                return self.reply(400, {'error': 'Use a loopback Host header.'})
            path = urlsplit(self.path).path
            match = re.fullmatch(r'/api/resources/([A-Za-z0-9][A-Za-z0-9_-]{0,47})(?:/(preview|reserve|cancel/([0-9]+)))?', path)
            if self.command in ('GET','HEAD'):
                if path in ASSETS:
                    filename,mime=ASSETS[path]
                    return self.reply(200,(ROOT/filename).read_bytes(),mime)
                if path == '/api/resources':
                    return self.reply(200,{'resources':self.server.store.list()})
                if match and match.group(2) is None:
                    return self.reply(200,self.server.store.snapshot(match.group(1)))
                return self.reply(404,{'error':'Not found.'})
            if self.command != 'POST':
                return self.reply(405,{'error':'Method not supported.'})
            origin=self.headers.get('Origin')
            if origin is not None and origin not in (f'http://localhost:{self.server.server_port}',f'http://127.0.0.1:{self.server.server_port}'):
                return self.reply(403,{'error':'Cross-origin requests are rejected.'})
            lengths=self.headers.get_all('Content-Length',[])
            if self.headers.get('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit() or not 0<int(lengths[0])<=8192:
                return self.reply(413,{'error':'Provide a JSON body of at most 8 KB.'})
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                return self.reply(415,{'error':'Use application/json.'})
            body=json.loads(self.rfile.read(int(lengths[0])).decode('utf-8'))
            if path=='/api/resources':
                return self.reply(201,self.server.store.create(body))
            if match:
                key,action,ident=match.groups()
                if action=='preview':
                    return self.reply(200,self.server.store.preview(key,body))
                if action=='reserve':
                    result=self.server.store.reserve(key,body)
                    return self.reply(200 if result['replayed'] else 201,result)
                if ident:
                    return self.reply(200,self.server.store.cancel(key,int(ident),body))
            return self.reply(404,{'error':'Not found.'})
        except Missing as error:
            self.reply(404,{'error':str(error)})
        except Conflict as error:
            self.reply(409,{'error':str(error),'details':error.details})
        except (ValueError,UnicodeDecodeError,RecursionError) as error:
            self.reply(400,{'error':str(error)})
        except sqlite3.OperationalError:
            self.reply(503,{'error':'Database temporarily unavailable. Retry shortly.'})

    do_GET=handle_request
    do_HEAD=handle_request
    do_POST=handle_request
    do_PUT=handle_request
    do_DELETE=handle_request


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--database',type=Path,default=ROOT/'data'/'slots.sqlite3')
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    server.store=Store(args.database)
    print(f'Slot Ledger: http://127.0.0.1:{server.server_port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    main()
