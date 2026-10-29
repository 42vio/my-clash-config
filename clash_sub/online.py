"""On-demand bridge to 3x-ui's own online list; never estimates presence from traffic."""

import http.client
import json
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

from clash_sub.xui import read_panel_settings

TOKEN_FILE = '/etc/clash-sub/credentials.json'
CREDENTIALS = TOKEN_FILE
LIMIT = 1024 * 1024


def parse_online(payload):
    if not isinstance(payload, dict) or payload.get('success') is not True or 'obj' not in payload:
        raise ValueError('online_unavailable')
    names = payload.get('obj')
    # 3x-ui serializes its nil slice as null when nobody is online.
    if names is None:
        return frozenset()
    if not isinstance(names, list) or len(names) > 10000 or any(not isinstance(name, str) or not 0 < len(name) <= 254 for name in names):
        raise ValueError('online_unavailable')
    return frozenset(names)


def _valid_token(token):
    return isinstance(token, str) and 16 <= len(token) <= 2048 and all(33 <= ord(ch) <= 126 for ch in token)


def read_credentials(path=CREDENTIALS):
    """Read the shared private credential mapping without following aliases."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('credentials_invalid')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as handle:
        details = os.fstat(handle.fileno())
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) != 0o600 or details.st_uid != os.geteuid()):
            raise ValueError('credentials_invalid')
        data = handle.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('credentials_invalid')
    values = json.loads(data)
    if not isinstance(values, dict):
        raise ValueError('credentials_invalid')
    return values


def save_credentials(updates, path=TOKEN_FILE, *, remove=()):
    """Merge private fields atomically, preserving other integrations."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError('credentials_invalid')
    values = {}
    if path.exists() or path.is_symlink():
        try:
            values = read_credentials(path)
        except (OSError, ValueError) as error:
            raise ValueError('credentials_invalid') from error
    values.update(updates)
    for key in remove:
        values.pop(key, None)
    payload = json.dumps(values).encode()
    if len(payload) > LIMIT:
        raise ValueError('credentials_invalid')
    fd, candidate = tempfile.mkstemp(prefix='.credentials-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)


def save_token(token, path=TOKEN_FILE):
    if not _valid_token(token):
        raise ValueError('online_credentials_invalid')
    save_credentials({'XUI_API_TOKEN': token}, path)


def read_token(path=CREDENTIALS):
    token = read_credentials(path)['XUI_API_TOKEN']
    if not _valid_token(token):
        raise ValueError('online_credentials_invalid')
    return token


def query_online(database, *, credentials=CREDENTIALS, connection_factory=http.client.HTTPConnection):
    connection = None
    try:
        port, base, _ = read_panel_settings(database)
        token = read_token(credentials)
        # Direct HTTPConnection ignores proxy env vars and cannot follow redirects.
        # The endpoint is always local, even if panel settings specify another listen IP.
        connection = connection_factory('127.0.0.1', port, timeout=2)
        connection.request('POST', base + 'panel/api/clients/onlines', body=b'', headers={'Authorization':'Bearer '+token, 'Accept':'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            return None
        return parse_online(json.loads(data))
    except Exception:
        # No URL, token, response body or credential-file content reaches logs.
        return None
    finally:
        if connection is not None:
            connection.close()


class OnlineCache:
    def __init__(self, reader, *, clock=time.monotonic):
        self.reader, self.clock = reader, clock
        self.lock = threading.Lock()
        self.checked = None
        self.value = None

    def get(self):
        with self.lock:
            now = self.clock()
            if self.checked is None or now - self.checked >= 10:
                try:
                    self.value = self.reader()
                except Exception:
                    self.value = None
                self.checked = now
            return self.value
