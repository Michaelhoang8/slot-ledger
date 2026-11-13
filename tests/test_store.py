import json
import random
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from store import Store, Conflict, Missing, occupancy


def request(key='a', start=0, end=60, units=1):
    return {'request_key':key,'start':start,'end':end,'units':units,'note':'test'}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'db.sqlite3'
        self.store=Store(self.path)
        self.store.create({'name':'lab','capacity':3})

    def tearDown(self):
        self.temp.cleanup()

    def test_half_open_boundaries_and_capacity(self):
        self.store.reserve('lab',request('a',0,60,3))
        self.store.reserve('lab',request('b',60,120,3))
        with self.assertRaises(Conflict) as caught:
            self.store.reserve('lab',request('c',59,61))
        self.assertEqual([s['start'] for s in caught.exception.details['conflicts']],[59,60])
        self.assertEqual(len(self.store.snapshot('lab')['bookings']),2)

    def test_preview_is_read_only_and_matches_reservation_gate(self):
        self.assertTrue(self.store.preview('lab',request())['fits'])
        self.assertEqual(self.store.snapshot('lab')['bookings'],[])
        self.store.reserve('lab',request(units=3))
        check=self.store.preview('lab',request('b',10,20))
        self.assertFalse(check['fits'])
        self.assertEqual(check['available_min'],0)
        self.assertEqual(check['conflicts'][0]['booking_ids'],[1])

    def test_retry_returns_same_booking_and_current_cancelled_state(self):
        first=self.store.reserve('lab',request())
        second=self.store.reserve('lab',request())
        self.assertTrue(second['replayed'])
        self.assertEqual(first['booking'],second['booking'])
        self.store.cancel('lab',1,{'expected_version':1})
        self.assertEqual(self.store.reserve('lab',request())['booking']['status'],'cancelled')
        with self.assertRaises(Conflict):
            self.store.reserve('lab',request(units=2))

    def test_cancel_version_prevents_stale_mutation_and_frees_capacity(self):
        self.store.reserve('lab',request(units=3))
        self.store.cancel('lab',1,{'expected_version':1})
        with self.assertRaises(Conflict):
            self.store.cancel('lab',1,{'expected_version':1})
        self.assertEqual(self.store.cancel('lab',1,{'expected_version':2})['version'],2)
        self.store.reserve('lab',request('b',units=3))
        self.assertEqual(self.store.snapshot('lab')['peak'],3)

    def test_simultaneous_reservations_cannot_oversubscribe(self):
        def reserve(i):
            try:
                return self.store.reserve('lab',request(str(i)))['booking']['id']
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(reserve,range(20)))
        self.assertEqual(sum(x is not None for x in results),3)
        self.assertTrue(self.store.snapshot('lab')['capacity_ok'])

    def test_concurrent_duplicate_requests_insert_once(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(lambda _:self.store.reserve('lab',request()),range(12)))
        self.assertEqual(sum(not x['replayed'] for x in results),1)
        self.assertEqual(len({x['booking']['id'] for x in results}),1)

    def test_failed_insert_rolls_back_without_capacity_leak(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER fail BEFORE INSERT ON bookings BEGIN SELECT RAISE(ABORT,'injected'); END")
        db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reserve('lab',request())
        self.assertEqual(self.store.snapshot('lab')['bookings'],[])

    def test_durable_reopen_and_resource_isolation(self):
        self.store.reserve('lab',request())
        self.store.create({'name':'other','capacity':1})
        self.store.reserve('other',request())
        self.assertEqual(len(Store(self.path).snapshot('lab')['bookings']),1)
        with self.assertRaises(Missing):
            self.store.cancel('other',1,{'expected_version':1})

    def test_validation_rejects_boolean_numbers_and_unknown_fields(self):
        for edit in [{'units':True},{'start':1.0},{'end':0},{'end':525601},{'note':'bad\n'},{'request_key':'../x'},{'extra':1}]:
            with self.assertRaises(ValueError):
                self.store.reserve('lab',{**request(),**edit})
        with self.assertRaises(Conflict):
            self.store.create({'name':'lab','capacity':2})

    def test_sweep_matches_minute_by_minute_oracle_for_random_intervals(self):
        rng=random.Random(73)
        for trial in range(300):
            bookings=[]
            for i in range(rng.randrange(1,30)):
                start=rng.randrange(60)
                bookings.append({'id':i+1,'start':start,'end':rng.randrange(start+1,61),'units':rng.randrange(1,6),'status':rng.choice(['active','active','cancelled'])})
            segments=occupancy(bookings,0,60)
            self.assertEqual(sum(s['end']-s['start'] for s in segments),60)
            for minute in range(60):
                expected=[b for b in bookings if b['status']=='active' and b['start']<=minute<b['end']]
                actual=next(s for s in segments if s['start']<=minute<s['end'])
                self.assertEqual(actual['used'],sum(b['units'] for b in expected))
                self.assertEqual(actual['booking_ids'],sorted(b['id'] for b in expected))

    def test_random_reservation_admission_matches_independent_load_array(self):
        rng=random.Random(17)
        load=[0]*60
        for i in range(200):
            start=rng.randrange(60); end=rng.randrange(start+1,61); units=rng.randrange(1,5)
            fits=all(load[t]+units<=3 for t in range(start,end))
            if fits:
                self.store.reserve('lab',request(str(i),start,end,units))
                for t in range(start,end):
                    load[t]+=units
            else:
                with self.assertRaises(Conflict):
                    self.store.reserve('lab',request(str(i),start,end,units))
        snapshot=self.store.snapshot('lab')
        self.assertEqual(snapshot['unit_minutes'],sum(load))
        self.assertEqual(snapshot['peak'],max(load))
