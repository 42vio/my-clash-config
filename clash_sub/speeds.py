"""On-demand average rates from local 3x-ui counters; no background sampler."""
import threading
import time


class SpeedSamples:
    def __init__(self, reader, *, clock=time.monotonic):
        self.reader, self.clock = reader, clock
        self.lock = threading.Lock()
        self.previous = None
        self.checked = None
        self.values = {}
        self.identities = None

    def get(self, state):
        identities = {str(k): (v.email, v.token) for k, v in state.users.items() if v.active}
        empty = {key: {'upload': None, 'download': None} for key in identities}
        with self.lock:
            now = self.clock()
            if identities == self.identities and self.checked is not None and 0 <= now-self.checked < 3:
                return self.values
            try:
                snapshot = self.reader()
                counters = {str(c.client_id): (c.upload, c.download) for c in snapshot.clients
                            if c.enabled and identities.get(str(c.client_id), (None,))[0] == c.email}
                if any(type(n) is not int or n < 0 for pair in counters.values() for n in pair):
                    raise ValueError('invalid_counters')
                elapsed = now-self.checked if self.checked is not None else 0
                if self.previous is not None and 3 <= elapsed <= 15:
                    for key, current in counters.items():
                        before = self.previous.get(key)
                        if before is not None and self.identities.get(key) == identities.get(key) and all(a >= b for a,b in zip(current,before)):
                            empty[key] = dict(zip(('upload','download'), ((a-b)/elapsed for a,b in zip(current,before))))
                self.previous = counters
            except Exception:
                self.previous = None
            self.checked, self.identities, self.values = now, identities, empty
            return empty
