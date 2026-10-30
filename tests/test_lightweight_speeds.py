import unittest
from types import SimpleNamespace as NS
from clash_sub.speeds import SpeedSamples

class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.counters=(100,200)
        self.calls=0
        self.fail=False
        self.state=NS(users={1:NS(active=True,email='member',token='identity')})
        def read():
            self.calls+=1
            if self.fail:raise ValueError('private')
            return NS(clients=[NS(client_id=1,email='member',enabled=True,upload=self.counters[0],download=self.counters[1])])
        self.samples=SpeedSamples(read,clock=lambda:self.now)
    def get(self):return self.samples.get(self.state)['1']
    def test_first_unknown_then_average_and_shared_cache(self):
        self.assertIsNone(self.get()['upload'])
        self.now=3;self.counters=(160,500)
        self.assertEqual(self.get(),{'upload':20,'download':100})
        self.assertEqual(self.get()['download'],100)
        self.assertEqual(self.calls,2)
    def test_reset_and_long_pause_require_new_baseline(self):
        self.get();self.now=3;self.counters=(0,0)
        self.assertIsNone(self.get()['upload'])
        self.now=6;self.assertEqual(self.get()['upload'],0)
        self.now=30;self.assertIsNone(self.get()['upload'])
    def test_failure_discards_old_baseline_without_exposing_error(self):
        self.get();self.now=3;self.fail=True
        self.assertEqual(self.get(),{'upload':None,'download':None})
        self.now=6;self.fail=False;self.assertIsNone(self.get()['upload'])
    def test_identity_change_cannot_reuse_old_rates(self):
        self.get();self.now=3;self.state.users[1].token='other'
        self.assertIsNone(self.get()['upload'])
    def test_invalid_counters_are_unknown(self):
        self.counters=(-1,4);self.assertIsNone(self.get()['upload'])
