from collections import deque
from .book import Book
from .rules import Rules, Reject, price_fen, integer, phase, format_time


class Simulation:
    def __init__(self, symbol, preclose, events, cash=100000000, yesterday=10000, metadata=None):
        self.symbol, self.preclose, self.events = symbol, preclose, events
        self.rules = Rules(symbol, preclose)
        self.historical, self.shadow = Book(), Book()
        self.cursor, self.time, self.last = 0, 84500000, 0
        self.cash, self.yesterday, self.today = cash, yesterday, 0
        self.initial_equity = cash + yesterday * preclose
        self.cost_basis = yesterday * preclose
        self.realized = 0
        self.orders, self.fills = [], []
        self.order_map = {}
        self.recent = deque(maxlen=40)
        self.trend = deque(maxlen=240)
        self.stats = {'adds':0,'cancels':0,'trades':0,'unknown_cancel':0,
                      'unknown_passive_trade':0,'missing_trade_references':0,'over_reduction':0,
                      'duplicate_add':0,'snapshot_checks':0,'exact_snapshots':0}
        self.metadata = metadata or {}
        self.snapshot, self.check = None, None
        self.continuous_ended = False
        self.trade_volume = 0

    def frozen_cash(self):
        return sum(o['price']*o['remaining'] for o in self.orders if o['side']=='B' and o['status'] in ('待成交','部分成交'))

    def frozen_shares(self):
        return sum(o['remaining'] for o in self.orders if o['side']=='S' and o['status'] in ('待成交','部分成交'))

    def available_shares(self): return self.yesterday - self.frozen_shares()

    def live_orders(self): return [o for o in self.orders if o['status'] in ('待成交','部分成交')]

    def _clock(self, t):
        self.time = t
        if t >= 145700000 and not self.continuous_ended:
            # Explicit prototype boundary: no simulated closing auction.
            for o in self.live_orders(): self._cancel(o, '连续竞价结束：原型不模拟收盘竞价，剩余委托到期', '已到期')
            self.continuous_ended = True

    def step(self, count=1, until=None):
        count = max(1, min(int(count), 2000000))
        moved = 0
        while self.cursor < len(self.events):
            e = self.events[self.cursor]
            if until is not None and e['Time'] > until: break
            if until is None and moved >= count: break
            self._clock(e['Time'])
            self.process(e)
            self.cursor += 1
            moved += 1
        if until is not None: self._clock(until)
        self.assert_account()
        return moved

    def process(self, e):
        k = e['kind']
        if k == 'snp':
            self.snapshot = e
            self._check_snapshot(e)
            if e['Price'] > 0: self.trend.append({'time':e['Time'],'price':e['Price']})
            return
        p, q, ch = e['Price'], e['Volume'], e['Channel']
        if k == 'ord':
            typ = e['OrderKind']
            if typ == 83:
                self.recent.append({'time':self.time,'type':'状态','price':p,'qty':q,'detail':chr(e['FunctionCode'])})
                return
            side = 'B' if e['FunctionCode']==66 else 'S'
            key = f'h:{ch}:{e["OrderOriNo"]}'
            if typ == 65:
                self.stats['adds'] += 1
                if key in self.historical.orders: self.stats['duplicate_add'] += 1
                self.historical.add(key,side,p,q)
                # Historical add reports residual liquidity, never re-match historical vs historical.
                # Counterfactual resting own orders may intercept this new residual quantity.
                remaining = q
                opposite = 'S' if side=='B' else 'B'
                if phase(self.time)=='连续竞价':
                    candidates = sorted((o for o in self.live_orders() if o['side']==opposite and
                                         ((side=='B' and o['price']<=p) or (side=='S' and o['price']>=p))),
                                        key=lambda o: (o['price'] if opposite=='S' else -o['price'],o['id']))
                    for o in candidates:
                        # Do not jump over historical liquidity at a better/same priority.
                        best = self.shadow.best(opposite)
                        if best != o['price'] or self.shadow.ahead(f's:{o["id"]}') > 0: break
                        take = min(remaining,o['remaining'])
                        self._fill(o, o['price'], take, '新增对手挂单触发')
                        self.shadow.reduce(f's:{o["id"]}',take)
                        remaining -= take
                        if not remaining: break
                self.shadow.add(key,side,p,remaining)
                typname = '新增买单' if side=='B' else '新增卖单'
            elif typ == 68:
                self.stats['cancels'] += 1
                if key not in self.historical.orders: self.stats['unknown_cancel'] += 1
                elif self.historical.orders[key]['qty'] < q: self.stats['over_reduction'] += 1
                self.historical.reduce(key,q)
                self.shadow.reduce(key,q)
                typname = '撤买单' if side=='B' else '撤卖单'
            else:
                return
            self.recent.append({'time':self.time,'type':typname,'price':p,'qty':q,'detail':str(e['OrderOriNo'])})
        else:
            self.stats['trades'] += 1
            self.last = p
            self.trade_volume += q
            bidkey, askkey = f'h:{ch}:{e["BidOrder"]}', f'h:{ch}:{e["AskOrder"]}'
            flag = e['BSFlag']
            passive = 'S' if flag==66 else 'B' if flag==83 else None
            if passive and phase(self.time)=='连续竞价':
                passivekey = askkey if passive=='S' else bidkey
                if passivekey not in self.historical.orders: self.stats['unknown_passive_trade'] += 1
                def on_sim(key, fp, fq): self._fill(self.order_map[int(key[2:])], fp, fq, '历史主动成交触发')
                # Volume is consumed once across ALL shadow levels and own orders, not once per order.
                self.shadow.consume(passive,p,q,on_sim)
                self.shadow.reduce(bidkey if passive=='S' else askkey,q)
            else:
                self.shadow.reduce(bidkey,q)
                self.shadow.reduce(askkey,q)
            for key in (bidkey,askkey):
                if key not in self.historical.orders: self.stats['missing_trade_references'] += 1
                elif self.historical.orders[key]['qty'] < q: self.stats['over_reduction'] += 1
                self.historical.reduce(key,q)
            self.recent.append({'time':self.time,'type':'主动买成交' if flag==66 else '主动卖成交' if flag==83 else '成交',
                                'price':p,'qty':q,'detail':str(e['BizIndex'])})

    def _check_snapshot(self,e):
        diffs = []
        for side,prefix in [('B','Bid'),('S','Ask')]:
            levels = self.historical.depth(side)
            for i in range(10):
                actual = levels[i] if i<len(levels) else {'price':0,'qty':0}
                ep, eq = e[f'{prefix}Price{i+1}'], int(e[f'{prefix}Volume{i+1}'])
                if actual['price']!=ep or actual['qty']!=eq:
                    diffs.append({'side':side,'level':i+1,'book_price':actual['price'],'book_qty':actual['qty'],
                                  'snapshot_price':ep,'snapshot_qty':eq})
        eligible = phase(e['Time'])=='连续竞价'
        if eligible:
            self.stats['snapshot_checks'] += 1
            if not diffs: self.stats['exact_snapshots'] += 1
        self.check = {'time':e['Time'],'differences':diffs,'eligible':eligible,
                      'note':'按时间戳对齐，不强制用快照覆盖订单簿；异步发布可能产生差异。' if eligible else '非连续竞价：集合竞价盘口展示口径不同，不计入通过率。'}

    def submit(self,side,price,quantity):
        p, qty = price_fen(price), integer(quantity)
        o = {'id':len(self.orders)+1,'symbol':self.symbol,'side':side,'price':p,'qty':qty,'remaining':qty,
             'filled':0,'amount':0,'time':self.time,'status':'待成交','reason':''}
        self.orders.append(o); self.order_map[o['id']]=o
        # Exclude the candidate from frozen calculations until validation finishes.
        o['status']='校验中'
        try:
            self.rules.validate(side,p,qty,self.time,self.available_shares(),
                                self.historical.best('B'),self.historical.best('S'),self.last)
            if side=='B' and p*qty > self.cash-self.frozen_cash(): raise Reject('可用资金不足（委托按限价全额冻结）')
            if any(x['side']!=side and ((side=='B' and p>=x['price']) or (side=='S' and p<=x['price'])) for x in self.live_orders()):
                raise Reject('原型拒绝与本账户现有委托交叉，避免自成交')
            if self.historical.best('B') and self.historical.best('S') and self.historical.best('B')>=self.historical.best('S'):
                raise Reject('历史盘口当前交叉，请继续回放至事件批次处理完成后报单')
        except Reject as exc:
            o['status']='已拒绝';o['remaining']=0;o['reason']=str(exc)
            raise
        o['status']='待成交'
        opposite = 'S' if side=='B' else 'B'
        fills = self.shadow.consume(opposite,p,qty,lambda *args: None)
        for fp,fq in fills: self._fill(o,fp,fq,'主动限价成交')
        if o['remaining']: self.shadow.add(f's:{o["id"]}',side,p,o['remaining'],True)
        self.assert_account()
        return o

    def _fill(self,o,p,q,reason):
        if not q: return
        if q > o['remaining']: raise AssertionError('overfill')
        before = self.yesterday+self.today
        if o['side']=='B':
            self.cash -= p*q;self.today += q;self.cost_basis += p*q
        else:
            cost = round(self.cost_basis*q/before) if before else 0
            self.cash += p*q;self.yesterday -= q;self.cost_basis -= cost
            self.realized += p*q-cost
        o['remaining'] -= q;o['filled'] += q;o['amount'] += p*q
        o['status']='已成交' if o['remaining']==0 else '部分成交'
        self.fills.append({'id':len(self.fills)+1,'order_id':o['id'],'symbol':self.symbol,'side':o['side'],
                           'price':p,'qty':q,'amount':p*q,'time':self.time,'reason':reason})

    def _cancel(self,o,reason,status='已撤单'):
        self.shadow.remove(f's:{o["id"]}')
        o['status']=status;o['reason']=reason
        o['cancelled']=o['remaining'];o['remaining']=0

    def cancel(self,oid):
        o=self.order_map.get(integer(oid,'委托号'))
        if not o or o['status'] not in ('待成交','部分成交'): raise Reject('委托不存在或已结束')
        if phase(self.time)!='连续竞价': raise Reject('原型仅在连续竞价阶段受理撤单')
        self._cancel(o,'用户撤单');self.assert_account()

    def assert_account(self):
        assert self.cash>=0 and self.cash-self.frozen_cash()>=0
        assert self.yesterday>=0 and self.today>=0 and self.available_shares()>=0
        for o in self.orders:
            if o['status']!='已拒绝': assert o['filled']+o['remaining']+o.get('cancelled',0)==o['qty']

    def state(self):
        mark=self.last or self.preclose
        shares=self.yesterday+self.today
        equity=self.cash+shares*mark
        orders=[dict(o, ahead=self.shadow.ahead(f's:{o["id"]}')) for o in reversed(self.orders)]
        bounds=self.rules.limits(self.historical.best('B'),self.historical.best('S'),self.last)
        return {'symbol':self.symbol,'date':'2026-09-23','time':self.time,'clock':format_time(self.time),
                'phase':phase(self.time),'cursor':self.cursor,'total_events':len(self.events),'last':mark,
                'preclose':self.preclose,'limits':bounds,'book':{s:self.historical.depth(s) for s in ('B','S')},
                'shadow':{s:self.shadow.depth(s) for s in ('B','S')},
                'account':{'cash':self.cash,'frozen_cash':self.frozen_cash(),'available_cash':self.cash-self.frozen_cash(),
                           'yesterday':self.yesterday,'today':self.today,'frozen_shares':self.frozen_shares(),
                           'available_shares':self.available_shares(),'shares':shares,'equity':equity,
                           'initial_equity':self.initial_equity,'pnl':equity-self.initial_equity,'realized':self.realized,
                           'unrealized':shares*mark-self.cost_basis},
                'orders':orders[:500],'order_count':len(orders),'fills':list(reversed(self.fills))[:500],
                'fill_count':len(self.fills),'recent':list(reversed(self.recent)),
                'stats':self.stats,'metadata':self.metadata,'check':self.check,'trend':list(self.trend)}
