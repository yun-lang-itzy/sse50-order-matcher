"""Optional full-day replay audit, without simulated orders, across the supplied universe."""
import json
import os
from pathlib import Path
from sse_sim.data import DataStore
from sse_sim.engine import Simulation

if __name__=='__main__':
    store=DataStore(os.environ.get('SSE_DATA_DIR', str(Path.home() / 'Friday.18.40')))
    results=[]
    for symbol in store.symbols:
        events,meta=store.load(symbol)
        s=Simulation(symbol,store.preclose[symbol],events,metadata=meta)
        s.step(until=153100000)
        results.append({'symbol':symbol,**meta,**s.stats,'processed':s.cursor})
        print(symbol,s.cursor,'unknown_cancel',s.stats['unknown_cancel'],'unknown_passive',s.stats['unknown_passive_trade'],'over_reduction',s.stats['over_reduction'],flush=True)
    Path('docs/replay-audit.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
