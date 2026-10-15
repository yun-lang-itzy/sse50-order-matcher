#!/usr/bin/env python3
"""Single local shared simulation, no brokerage/network trading connection."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from sse_sim.data import DataStore
from sse_sim.engine import Simulation
from sse_sim.rules import Reject, integer, price_fen, parse_time

ROOT = Path(__file__).resolve().parent


class Application:
    def __init__(self, store, save_path):
        self.store, self.save_path = store, save_path
        self.lock, self.token = threading.RLock(), secrets.token_hex(24)
        self.sim = None
        self.journal = {'initial':None,'actions':[]}
        self.restore_error = ''
        if save_path.exists():
            try:
                saved = json.loads(save_path.read_text())
                self.reset(saved['initial'],save=False)
                for action in saved['actions']:
                    try: self.apply(action,record=False)
                    except Reject: pass  # Rejected orders are part of the audit trail.
                self.journal=saved
            except Exception as exc:
                self.restore_error=f'上次会话恢复失败：{exc}。当前使用新账户。'
        if self.sim is None or self.restore_error:
            self.reset({'symbol':601688 if 601688 in store.symbols else store.symbols[0],
                        'cash':'1000000','yesterday':10000,'start':'09:30:00'},save=False)

    def save(self):
        self.save_path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.save_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.journal,ensure_ascii=False),encoding='utf-8')
        os.replace(tmp,self.save_path)

    def reset(self,payload,save=True):
        symbol=integer(payload.get('symbol'),'证券代码')
        cash=price_fen(payload.get('cash','1000000'))
        shares=integer(payload.get('yesterday',10000),'昨日持仓',minimum=0)
        start=payload.get('start','09:30:00')
        t=parse_time(start)
        if not 84500000<=t<=153100000: raise Reject('回放起点须在 08:45–15:31')
        events,meta=self.store.load(symbol)
        sim=Simulation(symbol,self.store.preclose[symbol],events,cash,shares,meta)
        sim.step(until=t)
        self.sim=sim
        self.journal={'initial':{'symbol':symbol,'cash':str(cash/100),'yesterday':shares,'start':start},'actions':[]}
        if save: self.save()

    def apply(self,action,record=True):
        op=action['op']
        try:
            if op=='advance':
                target=integer(action['cursor'],'事件位置',minimum=0)
                delta=target-self.sim.cursor
                if delta<0: raise Reject('回放不能倒退；请重置会话')
                if delta: self.sim.step(count=delta)
                self.sim._clock(action['time'])
            elif op=='order': self.sim.submit(action['side'],action['price'],action['quantity'])
            elif op=='cancel': self.sim.cancel(action['id'])
            else: raise Reject('未知操作')
        finally:
            if record:
                actions=self.journal['actions']
                if op=='advance' and actions and actions[-1]['op']=='advance': actions[-1]=action
                else: actions.append(action)
                self.save()

    def advance(self,payload):
        if 'until' in payload:
            t=parse_time(payload['until'])
            if t<self.sim.time: raise Reject('只能向前跳转；倒退请重置会话')
            if t>153100000: raise Reject('最晚跳转到 15:31:00')
            self.sim.step(until=t)
        else:
            self.sim.step(integer(payload.get('count',1),'步数',maximum=100000))
        action={'op':'advance','cursor':self.sim.cursor,'time':self.sim.time}
        # State already advanced; apply with a zero delta records exact cursor including tied timestamps.
        self.apply(action)

    def state(self):
        return dict(self.sim.state(),restore_error=self.restore_error)


def handler_factory(app,port):
    allowed_hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            if args and str(args[1] if len(args)>1 else '') not in ('200','304'):
                super().log_message(fmt,*args)

        def send(self,status,body,ctype='application/json; charset=utf-8'):
            if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',ctype)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(body)

        def guard(self,mutation=False):
            if self.headers.get('Host') not in allowed_hosts:
                self.send(403,{'error':'仅允许本机访问'});return False
            if mutation:
                origin=self.headers.get('Origin')
                if origin and origin not in {f'http://{h}' for h in allowed_hosts}:
                    self.send(403,{'error':'拒绝跨站请求'});return False
                if self.headers.get('X-Sim-Token')!=app.token:
                    self.send(403,{'error':'会话已更新，请刷新页面'});return False
            return True

        def do_GET(self):
            if not self.guard(): return
            path=urlparse(self.path).path
            with app.lock:
                if path=='/api/config':
                    self.send(200,{'token':app.token,'symbols':app.store.symbols,'date':app.store.date});return
                if path=='/api/state': self.send(200,app.state());return
                if path in ('/api/export/orders','/api/export/fills'):
                    rows=app.sim.orders if path.endswith('orders') else app.sim.fills
                    out=io.StringIO()
                    if rows:
                        keys=list(dict.fromkeys(k for r in rows for k in r))
                        w=csv.DictWriter(out,fieldnames=keys);w.writeheader();w.writerows(rows)
                    self.send(200,('\ufeff'+out.getvalue()).encode(),'text/csv; charset=utf-8');return
                files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
                if path in files:
                    types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}
                    f=ROOT/'static'/files[path]
                    self.send(200,f.read_bytes(),types[f.suffix]);return
                self.send(404,{'error':'未找到页面'})

        def do_POST(self):
            if not self.guard(True): return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=16384: raise Reject('请求体大小无效')
                payload=json.loads(self.rfile.read(n))
                if not isinstance(payload,dict): raise Reject('请求体必须为对象')
                with app.lock:
                    if self.path=='/api/reset': app.reset(payload)
                    elif self.path=='/api/advance': app.advance(payload)
                    elif self.path=='/api/order': app.apply({'op':'order','side':payload.get('side'),'price':payload.get('price'),'quantity':payload.get('quantity')})
                    elif self.path=='/api/cancel': app.apply({'op':'cancel','id':payload.get('id')})
                    else: raise Reject('未知操作')
                    self.send(200,app.state())
            except (Reject,ValueError,KeyError,TypeError) as exc:
                with app.lock: self.send(400,{'error':str(exc),'state':app.state()})
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self.send(500,{'error':f'本地服务异常：{type(exc).__name__}。详见终端。'})
    return Handler


def main():
    p=argparse.ArgumentParser(description='上交所本地订单簿回放与模拟下单')
    p.add_argument('--data-dir',default=os.environ.get('SSE_DATA_DIR', str(Path.home() / 'Friday.18.40')))
    p.add_argument('--port',default=8765,type=int)
    p.add_argument('--session',default=str(ROOT/'.runtime/session.json'))
    args=p.parse_args()
    app=Application(DataStore(args.data_dir),Path(args.session))
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler_factory(app,args.port))
    print(f'本地模拟交易已启动：http://127.0.0.1:{args.port}\n数据：{args.data_dir}\nCtrl+C 停止。无实盘接口。',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()


if __name__=='__main__': main()
