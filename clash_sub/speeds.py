"""Lease-bound, shared connection to 3x-ui's own traffic WebSocket."""
import http.client
import http.cookies
import json
import math
import re
import socket
import threading
import time
from urllib.parse import urlencode

from clash_sub.xui import read_panel_settings

LIMIT = 1024 * 1024
LEASE = 8


class PanelAuthError(ValueError):
    pass


def panel_login(port, base, username, password, *, connection_factory=http.client.HTTPConnection):
    """Local-only login. Cookies and passwords are never persisted or logged."""
    cookies = http.cookies.SimpleCookie()
    def request(method, path, body=None, extra=None):
        conn = connection_factory('127.0.0.1', port, timeout=3)
        try:
            headers = {'Accept': 'application/json', 'Cookie': '; '.join('%s=%s' % (m.key,m.coded_value) for m in cookies.values())}
            headers.update(extra or {})
            conn.request(method, base + path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read(LIMIT + 1)
            if response.status != 200 or len(payload) > LIMIT:
                raise PanelAuthError('panel_login_required')
            for name, value in response.getheaders():
                if name.lower() == 'set-cookie':
                    cookies.load(value)
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get('success') is not True:
                raise PanelAuthError('panel_login_required')
            return data
        finally:
            conn.close()
    csrf = request('GET', 'csrf-token').get('obj')
    if not isinstance(csrf, str) or not csrf or len(csrf) > 4096 or any(ord(c)<33 or ord(c)>126 for c in csrf):
        raise PanelAuthError('panel_login_required')
    request('POST', 'login', urlencode({'username': username, 'password': password}),
            {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': csrf})
    if not cookies:
        raise PanelAuthError('panel_login_required')
    return '; '.join('%s=%s' % (m.key,m.coded_value) for m in cookies.values())


def connect_panel(port, base, cookie):
    # Supply an already connected loopback socket: no proxy env or redirects.
    import websocket
    sock = socket.create_connection(('127.0.0.1', port), timeout=3)
    try:
        connection = websocket.create_connection('ws://127.0.0.1:%d%sws' % (port, base),
            socket=sock, cookie=cookie, timeout=3, suppress_origin=True, redirect_limit=0)
        connection.settimeout(1)
        return connection
    except Exception:
        sock.close()
        raise


def parse_traffic(message):
    """Match 3x-ui 3.11.0 useClients.ts, including per-source expiry."""
    if not isinstance(message, (str, bytes)) or len(message) > LIMIT:
        raise ValueError('invalid_speed_frame')
    data = json.loads(message)
    if not isinstance(data, dict):raise ValueError('invalid_speed_frame')
    if data.get('type') != 'traffic':return None
    payload = data.get('payload')
    if not isinstance(payload, dict) or 'clientTraffics' not in payload:
        return None
    rows = payload['clientTraffics']
    interval = payload.get('clientTrafficIntervalMs', 5000)
    if not isinstance(rows, list) or len(rows) > 10000 or type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 3600000:
        raise ValueError('invalid_speed_frame')
    source = 'tuic' if payload.get('clientTrafficSource') == 'tuic' else 'xray'
    values = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('email'), str) or not 0 < len(row['email']) <= 254:
            raise ValueError('invalid_speed_frame')
        up, down = row.get('up', 0), row.get('down', 0)
        if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in (up, down)):
            raise ValueError('invalid_speed_frame')
        current = values.setdefault(row['email'], {'upload': 0, 'download': 0})
        current['upload'] += up / (interval / 1000)
        current['download'] += down / (interval / 1000)
        if not all(math.isfinite(v) for v in current.values()):raise ValueError('invalid_speed_frame')
    return source, values, min(interval / 1000 * 2, 120)


class PanelSpeeds:
    def __init__(self, database, login_provider, *, connector=connect_panel, login=panel_login,
                 settings_reader=read_panel_settings, clock=time.monotonic):
        self.database, self.login_provider = database, login_provider
        self.connector, self.login, self.settings_reader, self.clock = connector, login, settings_reader, clock
        self.lock = threading.RLock()
        self.viewers = {}
        self.sources = {}
        self.thread = None
        self.connection = None
        self.cookie = None
        self.error = None
        self.retry_after = 0
        self.closed = False

    def _prune(self):
        now = self.clock()
        self.viewers = {k:v for k,v in self.viewers.items() if v[1] > now}

    def renew(self, viewer, session, state):
        if not isinstance(viewer, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', viewer):
            raise ValueError('invalid_viewer')
        with self.lock:
            self._prune()
            if self.closed or viewer in self.viewers and self.viewers[viewer][0] != session:
                raise ValueError('invalid_viewer')
            if viewer not in self.viewers and len(self.viewers) >= 64:
                raise ValueError('too_many_viewers')
            self.viewers[viewer] = (session, self.clock() + LEASE)
            if self.thread is None and self.clock() >= self.retry_after:
                self.error = None
                self.thread = threading.Thread(target=self._run, daemon=True, name='panel-speeds')
                self.thread.start()
            values = {str(k): {'upload': None, 'download': None} for k,v in state.users.items() if v.active}
            if not self.error and self.sources:
                now = self.clock()
                valid = [rates for rates,expiry in self.sources.values() if expiry > now]
                # Match panel expiry: once a valid source expires, render zero.
                for key,user in state.users.items():
                    if not user.active:continue
                    values[str(key)] = {field:sum(source.get(user.email, {}).get(field, 0) for source in valid)
                                        for field in ('upload','download')}
            return {'speeds': values, 'status': self.error or ('ok' if self.sources else 'waiting')}

    def release(self, viewer, session):
        conn = None
        with self.lock:
            if self.viewers.get(viewer, (None,))[0] == session:
                self.viewers.pop(viewer, None)
            self._prune()
            if not self.viewers:
                conn = self.connection
                self.sources = {}
        if conn is not None:
            try:conn.abort()
            except Exception:pass

    def credentials_changed(self):
        with self.lock:
            if self.error == 'panel_login_required':
                self.retry_after = 0
                self.error = None

    def revoke(self, session):
        with self.lock:
            viewers = [key for key, value in self.viewers.items() if value[0] == session]
        for viewer in viewers:self.release(viewer, session)

    def close(self):
        with self.lock:
            self.closed = True
            self.viewers.clear()
            conn = self.connection
            self.sources = {}
            self.cookie = None
        if conn is not None:
            try:conn.abort()
            except Exception:pass

    def _run(self):
        conn = None
        try:
            port, base, _ = self.settings_reader(self.database)
            with self.lock:
                self._prune()
                if not self.viewers:return
                session = next(iter(self.viewers.values()))[0]
                cookie = self.cookie
            if cookie is None:
                credentials = self.login_provider(session)
                if not credentials:raise PanelAuthError('panel_login_required')
                username, password = credentials
                try:cookie = self.login(port, base, username, password)
                except Exception:raise PanelAuthError('panel_login_required') from None
                finally:del credentials, username, password
                with self.lock:self.cookie = cookie
            with self.lock:
                self._prune()
                if not self.viewers or self.closed:return
            conn = self.connector(port, base, cookie)
            with self.lock:
                self.connection = conn
                self._prune()
                if not self.viewers or self.closed:return
            while True:
                with self.lock:
                    self._prune()
                    if not self.viewers or self.closed:break
                try:message = conn.recv()
                except (TimeoutError, socket.timeout):continue
                except Exception as error:
                    if type(error).__name__ == 'WebSocketTimeoutException':continue
                    raise
                if not message:break
                parsed = parse_traffic(message)
                if parsed is not None:
                    source, rates, ttl = parsed
                    with self.lock:self.sources[source] = (rates, self.clock() + ttl)
        except Exception as error:
            with self.lock:
                self.error = 'panel_login_required' if isinstance(error, PanelAuthError) or getattr(error, 'status_code', None) == 401 else 'unavailable'
                if self.error == 'panel_login_required':self.cookie = None
                self.sources = {}
                self.retry_after = self.clock() + 30
        finally:
            if conn is not None:
                try:conn.close(timeout=0)
                except Exception:pass
            with self.lock:
                self.connection = None
                self.sources = {}
                self.thread = None
                self._prune()
                # A new viewer may have arrived as the last connection closed.
                if self.viewers and not self.closed and self.clock() >= self.retry_after:
                    self.retry_after = self.clock() + 1
