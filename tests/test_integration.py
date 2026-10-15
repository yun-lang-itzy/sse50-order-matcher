import json
import os
from pathlib import Path
import tempfile
import unittest
from sse_sim.data import DataStore
from server import Application
from sse_sim.rules import Reject

DATA=Path(os.environ.get('SSE_DATA_DIR', str(Path.home() / 'Friday.18.40')))

@unittest.skipUnless((DATA/'ord_20260923.parquet').exists(),'local dataset not available')
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.store=DataStore(DATA)

    def test_main_star_replay_and_restore(self):
        for symbol in (601688,688008):
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as d:
                p=Path(d)/'session.json';app=Application(self.store,p)
                app.reset({'symbol':symbol,'cash':'1000000','yesterday':10000,'start':'10:00:00'})
                s=app.sim
                self.assertEqual(s.stats['unknown_cancel'],0)
                self.assertEqual(s.stats['over_reduction'],0)
                self.assertEqual(s.stats['unknown_passive_trade'],0)
                self.assertTrue(s.historical.best('B')<s.historical.best('S'))
                price=str(s.historical.best('B')/100)
                qty=200 if symbol==688008 else 100
                app.apply({'op':'order','side':'B','price':price,'quantity':qty})
                app.advance({'count':20})
                # Include a rejected order and check it survives restoration too.
                with self.assertRaises(Reject):app.apply({'op':'order','side':'B','price':'0.01','quantity':qty})
                saved=app.state();restored=Application(self.store,p)
                self.assertEqual(restored.restore_error,'')
                self.assertEqual(saved,restored.state())
                self.assertGreater(len(json.loads(p.read_text())['actions']),0)

    def test_end_of_day_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            app=Application(self.store,Path(d)/'s.json')
            app.advance({'until':'15:31:00'})
            self.assertEqual(app.sim.cursor,len(app.sim.events))
            self.assertEqual(app.sim.stats['unknown_cancel'],0)
            self.assertEqual(app.sim.stats['unknown_passive_trade'],0)
            with self.assertRaises(Reject):app.advance({'until':'10:00:00'})
            app.reset({'symbol':688008,'cash':'1000','yesterday':0,'start':'09:30:00'})
            self.assertEqual(app.sim.today,0);self.assertEqual(len(app.sim.orders),0)

if __name__=='__main__':unittest.main()
