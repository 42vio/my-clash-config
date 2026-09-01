"""On-demand subscription traffic metadata with a five-minute cache.

This module never touches profiles, releases, Nginx, YAML, or the network.
3x-ui traffic comes from one cached snapshot read per refresh (shared by
every client), airport traffic comes from the saved airport source record,
and queries never raise: a failed or absent source of data yields None.
"""

import json
import math
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

from clash_sub.airport_source import (
    AIRPORT_SOURCE_FILENAME,
    AirportSourceError,
    read_source_file,
)
from clash_sub.domain import Traffic, XuiClient, XuiSnapshot
from clash_sub.xui import read_xui_snapshot

CACHE_FILENAME = "traffic-cache.json"
CACHE_TTL_SECONDS = 300
CACHE_SCHEMA_VERSION = 1
_CACHE_MODE = 0o600
_MAX_CLIENT_ID_DIGITS = 19
_CANDIDATE_PREFIX = ".%s." % CACHE_FILENAME
_TRAFFIC_KEYS = frozenset({"download", "expire", "total", "upload"})
_CACHE_KEYS = frozenset({"clients", "refreshed_at", "schema_version"})


def render_subscription_userinfo(traffic) -> str:
    """Render the canonical four-field subscription-userinfo header."""
    if not isinstance(traffic, Traffic) or _invalid_traffic(traffic):
        raise ValueError("traffic_invalid")
    return "upload=%d; download=%d; total=%d; expire=%d" % (
        traffic.upload,
        traffic.download,
        traffic.total,
        traffic.expiry_ms,
    )


class TrafficMetadataStore:
    """Serve per-client traffic from a 300-second snapshot cache.

    All state lives behind one lock: a concurrent miss blocks until the
    winning refresh finishes and then shares its result (single-flight).
    The persisted cache file is loaded lazily, validated strictly, ignored
    on any irregularity, and rewritten atomically on refresh.  Reader and
    persistence failures never propagate; queries fall back to the stale
    cache or None.
    """

    def __init__(self, config, *, reader=read_xui_snapshot, clock=time.time):
        self._database = Path(config.xui_database)
        self._private_root = Path(config.private_root)
        self._cache_path = self._private_root / CACHE_FILENAME
        self._reader = reader
        self._clock = clock
        self._expected_uid = _expected_uid()
        self._lock = threading.Lock()
        self._entries = None
        self._refreshed_at = None

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def traffic_for(self, client_id) -> Traffic | None:
        """Return the cached traffic for one 3x-ui client, or None."""
        if isinstance(client_id, bool) or not isinstance(client_id, int):
            return None
        with self._lock:
            if self._entries is None:
                self._load_cached()
            if self._entries is not None and self._fresh():
                return self._entries.get(client_id)
            return self._refresh(client_id)

    def airport_traffic(self) -> Traffic | None:
        """Return the last saved airport traffic; never download or raise."""
        try:
            source = read_source_file(
                self._private_root / AIRPORT_SOURCE_FILENAME,
                expected_uid=self._expected_uid,
            )
        except AirportSourceError:
            return None
        return source.traffic

    def _fresh(self) -> bool:
        return self._clock() - self._refreshed_at < CACHE_TTL_SECONDS

    def _refresh(self, client_id):
        try:
            entries = _entries_from_snapshot(self._reader(self._database))
        except Exception:
            entries = None
        if entries is None:
            if self._entries is not None:
                return self._entries.get(client_id)
            return None
        self._entries = entries
        self._refreshed_at = self._clock()
        self._persist()
        return entries.get(client_id)

    def _load_cached(self) -> None:
        try:
            details = self._cache_path.lstat()
        except OSError:
            return
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != self._expected_uid
            or stat.S_IMODE(details.st_mode) != _CACHE_MODE
        ):
            return
        try:
            payload = json.loads(self._cache_path.read_bytes().decode("utf-8"))
            loaded = _entries_from_payload(payload)
        except (OSError, UnicodeError, ValueError):
            return
        if loaded is None:
            return
        self._entries, self._refreshed_at = loaded

    def _persist(self) -> None:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "refreshed_at": self._refreshed_at,
            "clients": {
                str(client_id): {
                    "upload": traffic.upload,
                    "download": traffic.download,
                    "total": traffic.total,
                    "expire": traffic.expiry_ms,
                }
                for client_id, traffic in sorted(self._entries.items())
            },
        }
        try:
            document = (
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError):
            return
        temporary = None
        try:
            try:
                descriptor, name = tempfile.mkstemp(
                    prefix=_CANDIDATE_PREFIX, dir=str(self._private_root)
                )
            except OSError:
                return
            temporary = Path(name)
            try:
                try:
                    os.fchmod(descriptor, _CACHE_MODE)
                    os.fchown(descriptor, self._expected_uid, -1)
                    _write_all_and_fsync(descriptor, document)
                finally:
                    os.close(descriptor)
                _os_replace(temporary, self._cache_path)
                temporary = None
                _fsync_directory(self._private_root)
            except OSError:
                pass
        finally:
            if temporary is not None:
                _remove_quietly(temporary)


def _entries_from_snapshot(snapshot):
    if not isinstance(snapshot, XuiSnapshot):
        return None
    entries = {}
    for client in snapshot.clients:
        if not isinstance(client, XuiClient) or _invalid_client(client):
            return None
        if not client.enabled:
            continue
        if client.client_id in entries:
            return None
        entries[client.client_id] = Traffic(
            upload=client.upload,
            download=client.download,
            total=client.total,
            expiry_ms=client.expiry_ms,
        )
    return entries


def _entries_from_payload(payload):
    if not isinstance(payload, dict) or set(payload) != _CACHE_KEYS:
        return None
    schema_version = payload["schema_version"]
    refreshed_at = payload["refreshed_at"]
    raw_clients = payload["clients"]
    if _bad_int(schema_version) or schema_version != CACHE_SCHEMA_VERSION:
        return None
    if _bad_timestamp(refreshed_at):
        return None
    if not isinstance(raw_clients, dict):
        return None
    entries = {}
    for key, raw_traffic in raw_clients.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or len(key) > _MAX_CLIENT_ID_DIGITS
        ):
            return None
        client_id = int(key)
        if client_id <= 0 or key != str(client_id):
            return None
        traffic = _traffic_from_payload(raw_traffic)
        if traffic is None:
            return None
        entries[client_id] = traffic
    return entries, refreshed_at


def _traffic_from_payload(raw_traffic):
    if not isinstance(raw_traffic, dict) or set(raw_traffic) != _TRAFFIC_KEYS:
        return None
    values = {}
    for key in sorted(_TRAFFIC_KEYS):
        value = raw_traffic[key]
        if _bad_int(value) or value < 0:
            return None
        values[key] = value
    return Traffic(
        upload=values["upload"],
        download=values["download"],
        total=values["total"],
        expiry_ms=values["expire"],
    )


def _invalid_traffic(traffic) -> bool:
    return any(_bad_int(value) or value < 0 for value in (
        traffic.upload,
        traffic.download,
        traffic.total,
        traffic.expiry_ms,
    ))


def _invalid_client(client) -> bool:
    return (
        _bad_int(client.client_id)
        or client.client_id <= 0
        or _invalid_traffic(client)
    )


def _bad_int(value) -> bool:
    return isinstance(value, bool) or not isinstance(value, int)


def _bad_timestamp(value) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return value < 0
    if isinstance(value, float):
        return not math.isfinite(value) or value < 0
    return True


def _expected_uid() -> int:
    return 0 if os.geteuid() == 0 else os.geteuid()


def _os_replace(source, target):
    os.replace(source, target)


def _os_write(descriptor, data):
    return os.write(descriptor, data)


def _os_fsync(descriptor):
    os.fsync(descriptor)


def _os_unlink(path):
    os.unlink(path)


def _write_all_and_fsync(descriptor, document):
    view = memoryview(document)
    while view:
        written = _os_write(descriptor, view)
        if written <= 0:
            raise OSError("incomplete cache write")
        view = view[written:]
    _os_fsync(descriptor)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        _os_fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_quietly(path):
    try:
        _os_unlink(path)
    except OSError:
        pass
