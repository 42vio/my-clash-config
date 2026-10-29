"""Private BandwagonHost credentials and fixed read-only usage queries."""

import http.client
import json
import math
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from clash_sub.online import CREDENTIALS, read_credentials, save_credentials


class UsageCache:
    """On-demand one-minute cache; keep last success only for the same account."""

    def __init__(self, *, credentials_reader=None, reader=None, clock=time.time):
        self.credentials_reader = credentials_reader or load_credentials
        self.reader = reader or query_usage
        self.clock = clock
        self.lock = threading.Lock()
        self.credentials = None
        self.checked = None
        self.usage = None
        self.updated = None
        self.error = False

    def get(self):
        with self.lock:
            try:
                credentials = self.credentials_reader()
            except Exception:
                self.credentials = self.usage = self.updated = self.checked = None
                return {'usage': None, 'updated': None, 'status': 'error'}
            if credentials != self.credentials:
                self.credentials = credentials
                self.usage = self.updated = self.checked = None
            if credentials is None:
                return {'usage': None, 'updated': None, 'status': 'unconfigured'}
            now = self.clock()
            if self.checked is None or now - self.checked >= 60:
                try:
                    self.usage = dict(self.reader(*credentials))
                    self.updated = now
                    self.error = False
                except Exception:
                    self.error = True
                self.checked = self.clock()
            return {'usage': dict(self.usage) if self.usage is not None else None,
                    'updated': self.updated, 'status': 'error' if self.error else 'ok'}


def validate_credentials(veid, api_key):
    if (not isinstance(veid, str) or not 1 <= len(veid) <= 19
            or not veid.isascii() or not veid.isdigit() or int(veid) <= 0
            or not isinstance(api_key, str) or not 16 <= len(api_key) <= 512
            or not all(33 <= ord(c) <= 126 for c in api_key)):
        raise ValueError('bwh_credentials_invalid')


def load_credentials(path=CREDENTIALS):
    try:
        data = read_credentials(path)
    except FileNotFoundError:
        return None
    if 'BWH_VEID' not in data and 'BWH_API_KEY' not in data:
        return None
    veid, key = data.get('BWH_VEID'), data.get('BWH_API_KEY')
    validate_credentials(veid, key)
    return veid, key


def save_api(veid, api_key, path=CREDENTIALS):
    validate_credentials(veid, api_key)
    save_credentials({'BWH_VEID': veid, 'BWH_API_KEY': api_key}, path)


def delete_api(path=CREDENTIALS):
    try:
        data = read_credentials(path)
    except FileNotFoundError:
        return
    if 'BWH_VEID' not in data and 'BWH_API_KEY' not in data:
        return
    save_credentials({}, path, remove=('BWH_VEID', 'BWH_API_KEY'))


def query_usage(veid, api_key, *, connection_factory=http.client.HTTPSConnection):
    """POST secrets only to the fixed usage endpoint; never follow redirects."""
    validate_credentials(veid, api_key)
    connection = None
    try:
        connection = connection_factory('api.64clouds.com', 443, timeout=8)
        connection.request('POST', '/v1/getServiceInfo',
                           body=urlencode({'veid': veid, 'api_key': api_key}).encode(),
                           headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('bwh_query_failed')
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('bwh_query_failed')
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get('error')) is not int or data['error'] != 0:
            raise ValueError('bwh_query_failed')
        values = {}
        for field in ('data_counter', 'plan_monthly_data', 'monthly_data_multiplier', 'data_next_reset'):
            value = data.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('bwh_query_failed')
            values[field] = value
        multiplier = values['monthly_data_multiplier']
        if multiplier <= 0 or values['plan_monthly_data'] <= 0 or values['data_next_reset'] > 253402300799:
            raise ValueError('bwh_query_failed')
        used, total = values['data_counter'] * multiplier, values['plan_monthly_data'] * multiplier
        if not math.isfinite(used) or not math.isfinite(total):
            raise ValueError('bwh_query_failed')
        return {'used': int(used), 'total': int(total), 'next_reset': int(values['data_next_reset'])}
    except Exception:
        # No upstream body, URL parameters or exception text reaches the CLI.
        raise ValueError('bwh_query_failed') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def format_usage(usage):
    reset = datetime.fromtimestamp(usage['next_reset'], ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
    return '套餐用量 %.2f / %.2f GB；剩余 %.2f GB；下次重置 %s（北京时间）。' % (
        usage['used'] / 1024**3, usage['total'] / 1024**3,
        max(0, usage['total'] - usage['used']) / 1024**3, reset)
