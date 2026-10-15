"""Order-id indexed FIFO levels; historical and simulated books are independent."""
from collections import OrderedDict


class Book:
    def __init__(self):
        self.orders = {}
        self.levels = {'B': {}, 'S': {}}
        self.sequence = 0

    def add(self, key, side, price, qty, simulated=False):
        if qty <= 0 or price <= 0: return
        self.remove(key)
        self.sequence += 1
        node = {'key': key, 'side': side, 'price': price, 'qty': qty,
                'simulated': simulated, 'seq': self.sequence}
        self.orders[key] = node
        self.levels[side].setdefault(price, OrderedDict())[key] = node

    def reduce(self, key, qty):
        node = self.orders.get(key)
        if not node: return 0
        used = min(qty, node['qty'])
        node['qty'] -= used
        if node['qty'] == 0: self.remove(key)
        return used

    def remove(self, key):
        node = self.orders.pop(key, None)
        if node:
            level = self.levels[node['side']][node['price']]
            level.pop(key, None)
            if not level: del self.levels[node['side']][node['price']]

    def prices(self, side):
        return sorted(self.levels[side], reverse=side == 'B')

    def best(self, side):
        prices = self.levels[side]
        return (max(prices) if side == 'B' else min(prices)) if prices else 0

    def depth(self, side, n=10):
        return [{'price': p, 'qty': sum(x['qty'] for x in self.levels[side][p].values()),
                 'orders': len(self.levels[side][p]),
                 'own': sum(x['qty'] for x in self.levels[side][p].values() if x['simulated'])}
                for p in self.prices(side)[:n]]

    def ahead(self, key):
        node = self.orders.get(key)
        if not node: return 0
        count = 0
        for x in self.levels[node['side']][node['price']].values():
            if x['key'] == key: break
            count += x['qty']
        return count

    def consume(self, side, limit, qty, on_simulated):
        """Consume resting side by price then FIFO, never beyond aggressor limit."""
        filled = []
        for p in self.prices(side):
            if (side == 'S' and p > limit) or (side == 'B' and p < limit): break
            for key in list(self.levels[side].get(p, {})):
                node = self.orders[key]
                take = min(qty, node['qty'])
                if node['simulated']: on_simulated(key, p, take)
                filled.append((p, take))
                self.reduce(key, take)
                qty -= take
                if qty == 0: return filled
        return filled
