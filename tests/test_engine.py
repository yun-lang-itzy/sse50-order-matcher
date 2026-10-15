import unittest
from sse_sim.rules import Rules, Reject, price_fen, parse_time, phase
from sse_sim.engine import Simulation
from sse_sim.book import Book


def order(oid,side='B',price=1000,qty=100,kind=65,time=93001000):
    return {'kind':'ord','Time':time,'Price':price,'Volume':qty,'Channel':1001,'BizIndex':oid,
            'OrderKind':kind,'FunctionCode':66 if side=='B' else 83,'OrderOriNo':oid}


def trade(qty,price=1000,bid=1,ask=999,flag=83,time=93002000):
    return {'kind':'exe','Time':time,'Price':price,'Volume':qty,'Channel':1001,'BizIndex':10000,
            'BidOrder':bid,'AskOrder':ask,'BSFlag':flag}


def sim(symbol=601688,shares=1000):
    s=Simulation(symbol,1000,[],cash=10000000,yesterday=shares)
    s._clock(93000000)
    return s


def feed(s,e):
    s._clock(e['Time']);s.process(e);s.assert_account()


class RuleTests(unittest.TestCase):
    def test_price_tick(self):
        self.assertEqual(price_fen('10.01'),1001)
        for v in ('10.001','NaN','Infinity','0','-1',True):
            with self.subTest(v=v),self.assertRaises(Reject): price_fen(v)

    def test_main_quantity(self):
        r=Rules(601688,1000)
        for q in (1,99,101,150):
            with self.subTest(q=q),self.assertRaises(Reject): r.validate('B',1000,q,93000000,1000,1000,1001,1000)
        r.validate('B',1000,100,93000000,0,1000,1001,1000)
        with self.assertRaises(Reject): r.validate('B',1000,1000100,93000000,0,1000,1001,1000)

    def test_star_quantity_and_odd_lot(self):
        r=Rules(688008,1000)
        for q in (200,201,99999,100000):r.validate('B',1000,q,93000000,0,1000,1001,1000)
        for q in (100,199,100001):
            with self.subTest(q=q),self.assertRaises(Reject):r.validate('B',1000,q,93000000,0,1000,1001,1000)
        r.validate('S',1000,150,93000000,150,1000,1001,1000)
        with self.assertRaises(Reject):r.validate('S',1000,100,93000000,150,1000,1001,1000)

    def test_main_odd_sell(self):
        r=Rules(601688,1000)
        r.validate('S',1000,150,93000000,150,1000,1001,1000)
        r.validate('S',1000,150,93000000,250,1000,1001,1000)
        r.validate('S',1000,50,93000000,150,1000,1001,1000)
        with self.assertRaises(Reject):r.validate('S',1000,25,93000000,150,1000,1001,1000)

    def test_different_cages(self):
        main=Rules(601688,200);star=Rules(688008,200)
        self.assertEqual(main.limits(200,200,200)['buy_max'],210)
        self.assertEqual(star.limits(200,200,200)['buy_max'],204)
        self.assertEqual(main.limits(200,200,200)['sell_min'],190)
        self.assertEqual(star.limits(200,200,200)['sell_min'],196)
        with self.assertRaises(Reject):star.validate('B',205,200,93000000,0,200,200,200)
        main.validate('B',205,100,93000000,0,200,200,200)

    def test_fallback_base(self):
        r=Rules(601688,1000)
        self.assertEqual(r.limits(900,0,800)['buy_base'],900)
        self.assertEqual(r.limits(0,900,800)['sell_base'],900)
        self.assertEqual(r.limits(0,0,800)['buy_base'],800)
        self.assertEqual(r.limits(0,0,0)['buy_base'],1000)

    def test_daily_limits_round_half_up(self):
        r=Rules(601688,1005)
        self.assertEqual((r.lower,r.upper),(905,1106))
        r=Rules(688008,1000);self.assertEqual((r.lower,r.upper),(800,1200))
        with self.assertRaises(Reject):r.validate('B',1201,200,93000000,0,1200,1200,1200)

    def test_sessions(self):
        for t in (92500000,113000000,120000000,145700000,150500000):
            with self.subTest(t=t),self.assertRaises(Reject):Rules(601688,1000).validate('B',1000,100,t,0,1000,1001,1000)
        for t in (93000000,112959999,130000000,145659999):self.assertEqual(phase(t),'连续竞价')
        self.assertEqual(parse_time('09:30:02.125'),93002125)
        for t in ('24:00:00','09:60:00','09:00:61','oops'):
            with self.assertRaises(Reject):parse_time(t)


class EngineTests(unittest.TestCase):
    def test_new_cancel_trade_historical(self):
        s=sim();feed(s,order(1,qty=300));feed(s,order(1,qty=100,kind=68));feed(s,trade(50))
        self.assertEqual(s.historical.orders['h:1001:1']['qty'],150)

    def test_fifo_ahead_and_behind_cancel(self):
        s=sim();feed(s,order(1,qty=300));o=s.submit('B','10',100)
        feed(s,order(2,qty=200));feed(s,order(2,qty=200,kind=68))
        self.assertEqual(s.shadow.ahead('s:1'),300)
        feed(s,order(1,qty=200,kind=68));self.assertEqual(s.shadow.ahead('s:1'),100)
        feed(s,trade(150));self.assertEqual((o['filled'],o['remaining']),(50,50))
        self.assertEqual(s.today,50);self.assertEqual(s.frozen_cash(),50000)
        s.cancel(1);self.assertEqual(s.frozen_cash(),0)

    def test_one_trade_volume_shared(self):
        s=sim();feed(s,order(1,qty=100));a=s.submit('B','10',100);b=s.submit('B','10',100)
        feed(s,trade(250));self.assertEqual((a['filled'],b['filled']),(100,50))
        self.assertEqual(sum(f['qty'] for f in s.fills),150)

    def test_price_priority_before_time(self):
        s=sim();a=s.submit('B','9.99',100);b=s.submit('B','10',100)
        feed(s,trade(100,price=999));self.assertEqual((a['filled'],b['filled']),(0,100))

    def test_active_sweep_partial_and_no_reuse(self):
        s=sim();feed(s,order(1,'S',1000,50));feed(s,order(2,'S',1001,100))
        a=s.submit('B','10.01',200)
        self.assertEqual((a['filled'],a['remaining']),(150,50))
        self.assertEqual(s.cash,10000000-50*1000-100*1001)
        self.assertEqual(s.frozen_cash(),50*1001)
        b=s.submit('B','10.01',100);self.assertEqual(b['filled'],0)
        self.assertEqual(s.historical.orders['h:1001:1']['qty'],50)

    def test_t_plus_one(self):
        s=sim(shares=0);feed(s,order(1,'S',1000,200));s.submit('B','10',100)
        self.assertEqual(s.today,100)
        with self.assertRaises(Reject):s.submit('S','10',100)
        self.assertEqual(s.yesterday,0)

    def test_sell_freeze_no_double_spend(self):
        s=sim(shares=100);s.submit('S','10',100)
        self.assertEqual(s.available_shares(),0)
        with self.assertRaises(Reject):s.submit('S','10',100)
        s.cancel(1);self.assertEqual(s.available_shares(),100)

    def test_cash_freeze(self):
        s=sim();s.cash=100000;s.submit('B','10',100)
        with self.assertRaises(Reject):s.submit('B','10',100)
        s.cancel(1);self.assertEqual(s.cash-s.frozen_cash(),100000)

    def test_price_improvement_refund(self):
        s=sim();s.cash=100100;feed(s,order(1,'S',1000,100));s.submit('B','10.01',100)
        self.assertEqual((s.cash,s.frozen_cash()),(100,0))

    def test_sell_partial_and_realized(self):
        s=sim();o=s.submit('S','10.01',200)
        feed(s,trade(50,price=1001,flag=66))
        self.assertEqual(o['filled'],50);self.assertEqual(s.realized,50)
        self.assertEqual((s.yesterday,s.frozen_shares(),s.available_shares()),(950,150,800))
        s.cancel(1);self.assertEqual(s.available_shares(),950)

    def test_unknown_passive_visible_diagnostic(self):
        s=sim();feed(s,trade(100));self.assertEqual(s.stats['unknown_passive_trade'],1)

    def test_new_opposite_order_can_fill_own(self):
        s=sim();o=s.submit('B','10',100);feed(s,order(1,'S',1000,150))
        self.assertEqual(o['filled'],100)
        self.assertEqual(s.shadow.orders['h:1001:1']['qty'],50)
        self.assertEqual(s.historical.orders['h:1001:1']['qty'],150)

    def test_self_cross_rejected(self):
        s=sim();s.submit('B','10',100)
        with self.assertRaises(Reject):s.submit('S','10',100)

    def test_no_simulated_auction_fill_and_expiry(self):
        s=sim();s.submit('B','10',100);s._clock(145700000)
        self.assertEqual(s.orders[0]['status'],'已到期');self.assertEqual(s.frozen_cash(),0)
        feed(s,trade(100,time=150000000));self.assertEqual(len(s.fills),0)

    def test_midday_keeps_order(self):
        s=sim();s.submit('B','10',100);s._clock(120000000)
        self.assertEqual(s.orders[0]['status'],'待成交')
        with self.assertRaises(Reject):s.cancel(1)
        s._clock(130000000);s.cancel(1)

    def test_exact_step_not_all_timestamp(self):
        e=[order(1),order(2)]
        s=Simulation(601688,1000,e)
        s.step(1);self.assertEqual(s.cursor,1)
        s.step(1);self.assertEqual(s.cursor,2)

    def test_invalid_order_is_recorded(self):
        s=sim()
        with self.assertRaises(Reject):s.submit('B','10',150)
        self.assertEqual(s.orders[0]['status'],'已拒绝');self.assertEqual(s.frozen_cash(),0)


if __name__=='__main__':unittest.main()
