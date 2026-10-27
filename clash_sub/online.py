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


def save_token(token, path=TOKEN_FILE):
    """Atomically store a hidden CLI input in a project-owned private file."""
    if not _valid_token(token):
        raise ValueError('online_credentials_invalid')
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError('online_credentials_invalid')
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o600:
            raise ValueError('online_credentials_invalid')
    fd, candidate = tempfile.mkstemp(prefix='.credentials-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({'XUI_API_TOKEN': token}, handle)
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


def read_token(path=CREDENTIALS):
    """Read the root-only project credential file on demand."""
    path = Path(path)
    with path.open('rb') as handle:
        details = os.fstat(handle.fileno())
        if path.is_symlink() or not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600 or details.st_uid != os.geteuid():
            raise ValueError('online_credentials_invalid')
        data = handle.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('online_credentials_invalid')
    token = json.loads(data)['XUI_API_TOKEN']
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
