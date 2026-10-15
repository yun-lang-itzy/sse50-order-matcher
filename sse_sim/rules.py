"""SSE 2026 rules, ordinary listed A shares only. All prices/cash in fen."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class Reject(ValueError):
    pass


def price_fen(value):
    try:
        d = Decimal(str(value)) * 100
        if not d.is_finite() or d <= 0 or d != d.to_integral_value():
            raise Reject('价格必须为正数，且以 0.01 元为最小单位（3.3.11）')
        if d > 100000000:
            raise Reject('价格超出原型支持范围')
        return int(d)
    except (InvalidOperation, ValueError, TypeError):
        raise Reject('请输入有效价格，最小单位 0.01 元')


def integer(value, label='数量', minimum=1, maximum=1000000000):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d != d.to_integral_value() or not minimum <= d <= maximum:
            raise ValueError()
        return int(d)
    except (InvalidOperation, ValueError, TypeError):
        raise Reject(f'{label}必须为 {minimum} 至 {maximum} 之间的整数')


def round_ratio(p, n, denominator=100):
    return int((Decimal(p) * n / denominator).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def phase(t):
    if t < 91500000: return '盘前'
    if t < 92500000: return '开盘集合竞价'
    if t < 93000000: return '开盘间歇'
    if t < 113000000: return '连续竞价'
    if t < 130000000: return '午间休市'
    if t < 145700000: return '连续竞价'
    if t < 150000000: return '收盘集合竞价'
    if t < 150500000: return '收盘后'
    if t <= 153000000: return '盘后固定价格交易'
    return '已收市'


def format_time(t):
    v = f'{int(t):09d}'
    return f'{v[:2]}:{v[2:4]}:{v[4:6]}.{v[6:]}'


def parse_time(s):
    try:
        parts = s.split(':')
        h, m = int(parts[0]), int(parts[1])
        sec = Decimal(parts[2])
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60): raise ValueError()
        ms = sec * 1000
        if ms != ms.to_integral_value(): raise ValueError()
        return h * 10000000 + m * 100000 + int(ms)
    except (ValueError, IndexError, InvalidOperation, AttributeError):
        raise Reject('时间格式应为 HH:MM:SS 或 HH:MM:SS.mmm')


class Rules:
    def __init__(self, symbol, preclose):
        self.star = str(symbol).startswith('688')
        self.preclose = preclose
        rate = 20 if self.star else 10
        self.lower = max(1, min(preclose - 1, round_ratio(preclose, 100-rate)))
        self.upper = max(preclose + 1, round_ratio(preclose, 100+rate))

    def limits(self, bid, ask, last):
        buybase = ask or bid or last or self.preclose
        sellbase = bid or ask or last or self.preclose
        up = max(buybase+1, round_ratio(buybase, 102))
        down = min(sellbase-1, round_ratio(sellbase, 98))
        if not self.star:
            up = max(up, buybase+10)
            down = min(down, sellbase-10)
        return {'buy_base': buybase, 'sell_base': sellbase,
                'buy_max': min(self.upper, up), 'sell_min': max(self.lower, down, 1),
                'lower': self.lower, 'upper': self.upper,
                'board': '科创板' if self.star else '主板'}

    def validate(self, side, p, qty, t, available, bid, ask, last):
        if side not in ('B','S'): raise Reject('方向必须为 B 或 S')
        if phase(t) != '连续竞价': raise Reject('此原型仅接受连续竞价阶段的限价申报')
        cap = 100000 if self.star else 1000000
        if qty > cap: raise Reject(f'单笔限价申报上限为 {cap:,} 股（6.7 / 3.3.9）')
        if self.star:
            if qty < 200 and not (side == 'S' and qty == available and available < 200):
                raise Reject('科创板限价单至少 200 股；不足 200 股的可卖余额须一次卖出（6.7）')
        elif side == 'B' and qty % 100:
            raise Reject('主板买入数量须为 100 股或其整数倍（3.3.8）')
        elif side == 'S' and qty % 100 and qty % 100 != available % 100:
            raise Reject('主板卖出的零股部分须与可卖余额的零股部分一致，不可拆分零股（3.3.8）')
        if not self.lower <= p <= self.upper:
            raise Reject(f'超出普通已上市股票涨跌停范围 {self.lower/100:.2f}–{self.upper/100:.2f}')
        bounds = self.limits(bid, ask, last)
        if side == 'B' and p > bounds['buy_max']:
            raise Reject(f'买价超过当前有效申报上限 {bounds["buy_max"]/100:.2f}（3.3.14 / 6.8）')
        if side == 'S' and p < bounds['sell_min']:
            raise Reject(f'卖价低于当前有效申报下限 {bounds["sell_min"]/100:.2f}（3.3.14 / 6.8）')
        if side == 'S' and qty > available:
            raise Reject('可卖持仓不足：当日买入不可当日卖出，且已冻结持仓不能重复卖出（T+1）')
