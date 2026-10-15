from functools import lru_cache
from pathlib import Path
import pyarrow.parquet as pq


def fen(value):
    # Source prices are float32 encodings of two-decimal A-share prices.
    return int(round(float(value) * 100))


class DataStore:
    def __init__(self, directory, date='20260923'):
        self.directory = Path(directory)
        self.date = date
        self.paths = {k: self.directory / f'{k}_{date}.parquet' for k in ('ord','exe','snp')}
        for p in self.paths.values():
            if not p.is_file(): raise FileNotFoundError(f'找不到行情文件：{p}')
        table = pq.read_table(self.paths['snp'], columns=['Symbol','PreClose'])
        self.symbols = sorted(set(table.column('Symbol').to_pylist()))
        self.preclose = {}
        for symbol, p in zip(table.column('Symbol').to_pylist(), table.column('PreClose').to_pylist()):
            if p > 0 and symbol not in self.preclose: self.preclose[symbol] = fen(p)

    @lru_cache(maxsize=2)
    def load(self, symbol):
        if symbol not in self.symbols: raise ValueError('股票不在本地数据集中')
        events = []
        for kind in ('ord','exe'):
            rows = pq.read_table(self.paths[kind], filters=[('Symbol','=',symbol)]).to_pylist()
            for r in rows:
                r['kind'] = kind
                r['Price'] = fen(r['Price'])
                events.append(r)
        # Within a channel BizIndex determines ties; cross-channel times have no exchange-wide sequence.
        events.sort(key=lambda r: (r['Time'], r['Channel'], r['BizIndex']))
        seen, duplicates = set(), 0
        clean = []
        for r in events:
            key = (r['Channel'], r['BizIndex'])
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            clean.append(r)
        snaps = pq.read_table(self.paths['snp'], filters=[('Symbol','=',symbol)]).to_pylist()
        for r in snaps:
            r['kind'] = 'snp'
            for k in list(r):
                if 'Price' in k or k in ('PreClose','Open','High','Low'): r[k] = fen(r[k])
        clean.extend(snaps)
        clean.sort(key=lambda r: (r['Time'], 1 if r['kind']=='snp' else 0,
                                  r.get('Channel',0), r.get('BizIndex',0)))
        return clean, {'duplicates': duplicates, 'events': len(clean), 'snapshots': len(snaps)}
