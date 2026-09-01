"""On-demand subscription traffic metadata cache."""

import contextlib
import http.client
import io
import itertools
import json
import os
import socket
import stat
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from clash_sub import metadata
from clash_sub import metadata_server
from clash_sub.airport_source import (
    AIRPORT_SOURCE_FILENAME,
    AirportSource,
    serialize_source,
)
from clash_sub.domain import Traffic, XuiClient, XuiSnapshot
from clash_sub.metadata import (
    CACHE_FILENAME,
    CACHE_TTL_SECONDS,
    TrafficMetadataStore,
    render_subscription_userinfo,
)
from clash_sub.xui import XuiCompatibilityError

SOURCE_URL = "https://example.invalid/subscription"
LAST_SUCCESS = 1788192000


def make_client(client_id, *, enabled=True, upload=0, download=0, total=0, expiry_ms=0):
    return XuiClient(
        client_id=client_id,
        email="user%d@example.invalid" % client_id,
        sub_id="SUBID%d" % client_id,
        enabled=enabled,
        upload=upload,
        download=download,
        total=total,
        expiry_ms=expiry_ms,
    )


def make_snapshot(*clients):
    return XuiSnapshot(
        clients=tuple(clients),
        listen="127.0.0.1",
        port=2096,
        clash_path="/xui-sub/",
    )


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingReader:
    """Database reader stub returning queued outcomes and recording calls."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if not self.outcomes:
            raise AssertionError("unexpected database read")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        os.chmod(self.root, 0o700)
        self.database = self.root / "xui.db"
        self.clock = FakeClock()
        self.cache_path = self.root / CACHE_FILENAME

    def make_store(self, reader):
        return TrafficMetadataStore(
            SimpleNamespace(xui_database=self.database, private_root=self.root),
            reader=reader,
            clock=self.clock,
        )


class RenderSubscriptionUserinfoTests(unittest.TestCase):
    def test_renders_the_exact_canonical_header(self):
        traffic = Traffic(upload=1, download=2, total=3, expiry_ms=4)
        self.assertEqual(
            render_subscription_userinfo(traffic),
            "upload=1; download=2; total=3; expire=4",
        )

    def test_renders_the_traffic_values_verbatim(self):
        traffic = Traffic(upload=112233, download=99887766, total=123456789, expiry_ms=55)
        self.assertEqual(
            render_subscription_userinfo(traffic),
            "upload=112233; download=99887766; total=123456789; expire=55",
        )

    def test_rejects_invalid_traffic(self):
        invalid = (
            "traffic",
            None,
            Traffic(upload=-1, download=2, total=3, expiry_ms=4),
            Traffic(upload=True, download=2, total=3, expiry_ms=4),
            Traffic(upload=1.5, download=2, total=3, expiry_ms=4),
            Traffic(upload=1, download=2, total=3, expiry_ms=-4),
        )
        for traffic in invalid:
            with self.assertRaises(ValueError):
                render_subscription_userinfo(traffic)


class CacheSemanticsTests(StoreTestCase):
    def test_first_query_refreshes_and_returns_traffic(self):
        reader = RecordingReader(
            make_snapshot(make_client(7, upload=10, download=20, total=30, expiry_ms=40000))
        )
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(7), Traffic(upload=10, download=20, total=30, expiry_ms=40)
        )
        self.assertEqual(reader.calls, [self.database])

    def test_xui_expiry_is_converted_from_milliseconds_to_seconds(self):
        reader = RecordingReader(
            make_snapshot(
                make_client(6, upload=7, download=8, total=9, expiry_ms=1893456000000)
            )
        )
        store = self.make_store(reader)
        traffic = store.traffic_for(6)
        self.assertEqual(
            traffic, Traffic(upload=7, download=8, total=9, expiry_ms=1893456000)
        )
        self.assertEqual(
            render_subscription_userinfo(traffic),
            "upload=7; download=8; total=9; expire=1893456000",
        )
        self.assertEqual(
            json.loads(self.cache_path.read_bytes())["clients"]["6"]["expire"],
            1893456000,
        )

    def test_query_within_ttl_hits_the_cache(self):
        reader = RecordingReader(make_snapshot(make_client(7, upload=1, download=2, total=3, expiry_ms=4000)))
        store = self.make_store(reader)
        store.traffic_for(7)
        self.clock.advance(CACHE_TTL_SECONDS - 1)
        self.assertEqual(
            store.traffic_for(7), Traffic(upload=1, download=2, total=3, expiry_ms=4)
        )
        self.assertEqual(len(reader.calls), 1)

    def test_query_after_ttl_refreshes(self):
        reader = RecordingReader(
            make_snapshot(make_client(7, upload=1, download=2, total=3, expiry_ms=4000)),
            make_snapshot(make_client(7, upload=5, download=6, total=7, expiry_ms=8000)),
        )
        store = self.make_store(reader)
        store.traffic_for(7)
        self.clock.advance(CACHE_TTL_SECONDS + 1)
        self.assertEqual(
            store.traffic_for(7), Traffic(upload=5, download=6, total=7, expiry_ms=8)
        )
        self.assertEqual(len(reader.calls), 2)

    def test_one_refresh_populates_all_clients(self):
        reader = RecordingReader(
            make_snapshot(
                make_client(1, upload=11, download=12, total=13, expiry_ms=14000),
                make_client(2, upload=21, download=22, total=23, expiry_ms=24000),
                make_client(3, upload=31, download=32, total=33, expiry_ms=34000),
            )
        )
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(1), Traffic(upload=11, download=12, total=13, expiry_ms=14)
        )
        self.assertEqual(
            store.traffic_for(2), Traffic(upload=21, download=22, total=23, expiry_ms=24)
        )
        self.assertEqual(
            store.traffic_for(3), Traffic(upload=31, download=32, total=33, expiry_ms=34)
        )
        self.assertEqual(len(reader.calls), 1)

    def test_unknown_client_returns_none_without_extra_reads(self):
        reader = RecordingReader(make_snapshot(make_client(1, upload=1, download=1, total=1, expiry_ms=1)))
        store = self.make_store(reader)
        self.assertIsNone(store.traffic_for(999))
        self.assertIsNone(store.traffic_for(999))
        self.assertEqual(len(reader.calls), 1)

    def test_non_integer_client_id_returns_none(self):
        store = self.make_store(RecordingReader())
        self.assertIsNone(store.traffic_for("7"))
        self.assertIsNone(store.traffic_for(True))

    def test_variant_queries_share_one_client_cache(self):
        # The cache key is the client id alone, so the same owner's
        # Clash-Compat.yaml and Clash-Balance.yaml requests share it.
        reader = RecordingReader(
            make_snapshot(make_client(9, upload=1, download=2, total=3, expiry_ms=4))
        )
        store = self.make_store(reader)
        compat = store.traffic_for(9)
        balance = store.traffic_for(9)
        self.assertEqual(compat, balance)
        self.assertEqual(len(reader.calls), 1)

    def test_concurrent_misses_execute_one_reader_call(self):
        entered = threading.Event()
        release = threading.Event()
        snapshot = make_snapshot(
            make_client(5, upload=10, download=20, total=30, expiry_ms=40000)
        )
        calls = []

        def blocking_reader(path):
            calls.append(path)
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return snapshot

        store = self.make_store(blocking_reader)
        results = []
        results_lock = threading.Lock()

        def query():
            value = store.traffic_for(5)
            with results_lock:
                results.append(value)

        threads = [threading.Thread(target=query) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(timeout=5))
        time.sleep(0.05)
        release.set()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            results,
            [Traffic(upload=10, download=20, total=30, expiry_ms=40)] * 2,
        )

    def test_reader_failure_serves_the_stale_cache_without_overwriting(self):
        reader = RecordingReader(
            make_snapshot(make_client(4, upload=1, download=2, total=3, expiry_ms=4000)),
            XuiCompatibilityError("3x-ui database compatibility check failed"),
        )
        store = self.make_store(reader)
        store.traffic_for(4)
        persisted = self.cache_path.read_bytes()
        self.clock.advance(CACHE_TTL_SECONDS + 1)
        self.assertEqual(
            store.traffic_for(4), Traffic(upload=1, download=2, total=3, expiry_ms=4)
        )
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(self.cache_path.read_bytes(), persisted)
        self.assertEqual(json.loads(persisted)["refreshed_at"], 1000.0)

    def test_reader_failure_without_cache_returns_none(self):
        reader = RecordingReader(XuiCompatibilityError("3x-ui database compatibility check failed"))
        store = self.make_store(reader)
        self.assertIsNone(store.traffic_for(4))
        self.assertEqual(len(reader.calls), 1)
        self.assertFalse(self.cache_path.exists())

    def test_new_instance_serves_stale_file_when_reader_fails(self):
        reader = RecordingReader(
            make_snapshot(make_client(4, upload=9, download=8, total=7, expiry_ms=6000))
        )
        self.make_store(reader).traffic_for(4)
        self.clock.advance(CACHE_TTL_SECONDS + 1)
        failing = RecordingReader(
            XuiCompatibilityError("3x-ui database compatibility check failed")
        )
        store = self.make_store(failing)
        self.assertEqual(
            store.traffic_for(4), Traffic(upload=9, download=8, total=7, expiry_ms=6)
        )
        self.assertEqual(len(failing.calls), 1)

    def test_disabled_client_is_not_cached(self):
        reader = RecordingReader(
            make_snapshot(
                make_client(1, upload=1, download=1, total=1, expiry_ms=1000),
                make_client(2, enabled=False, upload=2, download=2, total=2, expiry_ms=2000),
            )
        )
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(1), Traffic(upload=1, download=1, total=1, expiry_ms=1)
        )
        self.assertIsNone(store.traffic_for(2))
        self.assertEqual(
            set(json.loads(self.cache_path.read_bytes())["clients"]), {"1"}
        )


class CacheFileSafetyTests(StoreTestCase):
    def test_cache_file_lives_at_the_fixed_private_path(self):
        config = SimpleNamespace(
            xui_database=Path("/var/lib/clash-sub/xui.db"),
            private_root=Path("/var/lib/clash-sub/private"),
        )
        store = TrafficMetadataStore(config, reader=RecordingReader(), clock=FakeClock())
        self.assertEqual(CACHE_FILENAME, "traffic-cache.json")
        self.assertEqual(
            store.cache_path, Path("/var/lib/clash-sub/private/traffic-cache.json")
        )

    def test_written_file_satisfies_the_ownership_contract(self):
        reader = RecordingReader(make_snapshot(make_client(1, upload=1, download=2, total=3, expiry_ms=4)))
        self.make_store(reader).traffic_for(1)
        expected_uid = 0 if os.geteuid() == 0 else os.geteuid()
        details = self.cache_path.lstat()
        self.assertFalse(self.cache_path.is_symlink())
        self.assertTrue(stat.S_ISREG(details.st_mode))
        self.assertEqual(stat.S_IMODE(details.st_mode), 0o600)
        self.assertEqual(details.st_nlink, 1)
        self.assertEqual(details.st_uid, expected_uid)

    def test_file_contains_only_version_timestamp_and_traffic_numbers(self):
        reader = RecordingReader(
            make_snapshot(
                make_client(1, upload=100, download=200, total=300, expiry_ms=400000),
                make_client(2, upload=101, download=201, total=301, expiry_ms=401000),
            )
        )
        self.make_store(reader).traffic_for(1)
        data = self.cache_path.read_bytes()
        for forbidden in (
            b"example.invalid",
            b"SUBID",
            b"127.0.0.1",
            b"http",
            b"xui-sub",
            b"token",
        ):
            self.assertNotIn(forbidden, data)
        payload = json.loads(data)
        self.assertEqual(set(payload), {"schema_version", "refreshed_at", "clients"})
        self.assertEqual(
            payload["clients"],
            {
                "1": {"upload": 100, "download": 200, "total": 300, "expire": 400},
                "2": {"upload": 101, "download": 201, "total": 301, "expire": 401},
            },
        )

    def test_corrupt_cache_file_is_ignored_and_rebuilt(self):
        self.cache_path.write_bytes(b"{not json")
        os.chmod(self.cache_path, 0o600)
        reader = RecordingReader(make_snapshot(make_client(3, upload=1, download=1, total=1, expiry_ms=1000)))
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(3), Traffic(upload=1, download=1, total=1, expiry_ms=1)
        )
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(json.loads(self.cache_path.read_bytes())["schema_version"], 1)

    def test_wrong_schema_cache_is_ignored_and_rebuilt(self):
        def valid_payload():
            return {
                "schema_version": 1,
                "refreshed_at": 1000.0,
                "clients": {"3": {"upload": 0, "download": 0, "total": 0, "expire": 0}},
            }

        def variant(**changes):
            payload = valid_payload()
            payload.update(changes)
            return payload

        corrupt = [
            b"[]",
            variant(schema_version=2),
            variant(schema_version=True),
            variant(refreshed_at=-1),
            variant(refreshed_at=True),
            variant(refreshed_at="1000"),
            variant(refreshed_at=float("nan")),
            variant(refreshed_at=float("inf")),
            variant(clients=[]),
            variant(clients={"3": {"upload": -1, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"3": {"upload": True, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"3": {"upload": 1.5, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"3": {"upload": 0, "download": 0, "total": 0}}),
            variant(clients={"3": "traffic"}),
            variant(clients={"abc": {"upload": 0, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"03": {"upload": 0, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"0": {"upload": 0, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"-1": {"upload": 0, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"9" * 20: {"upload": 0, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"9" * 5000: {"upload": 0, "download": 0, "total": 0, "expire": 0}}),
            variant(clients={"3": {"upload": 0, "download": 0, "total": 0, "expire": 0}, "4": None}),
            variant(extra=1),
            json.dumps(
                {key: value for key, value in valid_payload().items() if key != "clients"}
            ).encode("utf-8"),
        ]
        for payload in corrupt:
            with self.subTest(payload=payload):
                self.cache_path.write_bytes(
                    payload if isinstance(payload, bytes)
                    else json.dumps(payload).encode("utf-8")
                )
                os.chmod(self.cache_path, 0o600)
                reader = RecordingReader(
                    make_snapshot(make_client(3, upload=1, download=1, total=1, expiry_ms=1000))
                )
                store = self.make_store(reader)
                self.assertEqual(
                    store.traffic_for(3),
                    Traffic(upload=1, download=1, total=1, expiry_ms=1),
                )
                self.assertEqual(len(reader.calls), 1)
                self.assertEqual(
                    json.loads(self.cache_path.read_bytes())["clients"]["3"]["upload"], 1
                )

    def test_wrong_mode_cache_is_ignored_and_rebuilt(self):
        self.cache_path.write_bytes(b'{"schema_version":1}')
        os.chmod(self.cache_path, 0o644)
        reader = RecordingReader(make_snapshot(make_client(2, upload=1, download=1, total=1, expiry_ms=1000)))
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(2), Traffic(upload=1, download=1, total=1, expiry_ms=1)
        )
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(stat.S_IMODE(self.cache_path.lstat().st_mode), 0o600)

    def test_hard_linked_cache_is_ignored_and_rebuilt(self):
        self.cache_path.write_bytes(b'{"schema_version":1}')
        os.chmod(self.cache_path, 0o600)
        os.link(self.cache_path, self.root / "hardlink.json")
        reader = RecordingReader(make_snapshot(make_client(2, upload=1, download=1, total=1, expiry_ms=1000)))
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(2), Traffic(upload=1, download=1, total=1, expiry_ms=1)
        )
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(self.cache_path.lstat().st_nlink, 1)

    def test_symlink_cache_is_ignored_and_rebuilt(self):
        outside = self.root.parent / ("outside-%d.json" % os.getpid())
        outside.write_bytes(b'{"schema_version":1}')
        self.addCleanup(outside.unlink, missing_ok=True)
        self.cache_path.symlink_to(outside)
        reader = RecordingReader(make_snapshot(make_client(2, upload=1, download=1, total=1, expiry_ms=1000)))
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(2), Traffic(upload=1, download=1, total=1, expiry_ms=1)
        )
        self.assertEqual(len(reader.calls), 1)
        self.assertFalse(self.cache_path.is_symlink())
        self.assertTrue(stat.S_ISREG(self.cache_path.lstat().st_mode))

    def test_refresh_replaces_atomically_and_leaves_no_temporaries(self):
        reader = RecordingReader(make_snapshot(make_client(1, upload=1, download=1, total=1, expiry_ms=1)))
        store = self.make_store(reader)
        replaced = []
        original = metadata._os_replace

        def observing_replace(source, target):
            replaced.append((Path(source), Path(target)))
            return original(source, target)

        with patch.object(metadata, "_os_replace", side_effect=observing_replace):
            store.traffic_for(1)
        self.assertEqual(len(replaced), 1)
        source, target = replaced[0]
        self.assertEqual(target, self.cache_path)
        self.assertEqual(source.parent, self.root)
        self.assertNotEqual(source, target)
        self.assertFalse(source.exists())
        leftovers = [
            entry.name for entry in self.root.iterdir() if entry.name != CACHE_FILENAME
        ]
        self.assertEqual(leftovers, [])

    def test_persistence_failure_does_not_block_the_query(self):
        reader = RecordingReader(
            make_snapshot(make_client(1, upload=5, download=6, total=7, expiry_ms=8000)),
            make_snapshot(make_client(1, upload=5, download=6, total=7, expiry_ms=8000)),
        )
        store = self.make_store(reader)
        with patch.object(metadata, "_os_replace", side_effect=OSError("injected")):
            self.assertEqual(
                store.traffic_for(1),
                Traffic(upload=5, download=6, total=7, expiry_ms=8),
            )
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        self.clock.advance(CACHE_TTL_SECONDS + 1)
        self.assertEqual(
            store.traffic_for(1), Traffic(upload=5, download=6, total=7, expiry_ms=8)
        )
        self.assertTrue(self.cache_path.exists())
        self.assertEqual(
            [entry.name for entry in self.root.iterdir()], [CACHE_FILENAME]
        )


class AirportTrafficTests(StoreTestCase):
    def write_source(self, source):
        path = self.root / AIRPORT_SOURCE_FILENAME
        path.write_bytes(serialize_source(source))
        os.chmod(path, 0o600)
        return path

    def test_returns_the_saved_source_traffic_without_database_access(self):
        def forbidden(path):
            raise AssertionError("airport query must not read the 3x-ui database")

        traffic = Traffic(upload=1, download=2, total=3, expiry_ms=4)
        self.write_source(AirportSource(SOURCE_URL, traffic, LAST_SUCCESS))
        store = self.make_store(forbidden)
        self.assertEqual(store.airport_traffic(), traffic)

    def test_source_without_traffic_returns_none(self):
        self.write_source(AirportSource(SOURCE_URL, None, LAST_SUCCESS))
        self.assertIsNone(self.make_store(RecordingReader()).airport_traffic())

    def test_missing_source_returns_none(self):
        self.assertIsNone(self.make_store(RecordingReader()).airport_traffic())

    def test_invalid_source_returns_none_without_raising(self):
        path = self.root / AIRPORT_SOURCE_FILENAME
        path.write_bytes(b"{not json")
        os.chmod(path, 0o600)
        self.assertIsNone(self.make_store(RecordingReader()).airport_traffic())


class FakeMetadataStore:
    """Store stub returning fixed traffic and recording every query."""

    def __init__(self, profile_traffic=None, airport_traffic=None):
        self.profile_traffic = profile_traffic
        self.airport_traffic_value = airport_traffic
        self.calls = []

    def traffic_for(self, client_id):
        self.calls.append(("profile", client_id))
        return self.profile_traffic

    def airport_traffic(self):
        self.calls.append(("airport",))
        return self.airport_traffic_value


class RaisingMetadataStore:
    def traffic_for(self, client_id):
        raise RuntimeError("metadata store failure")

    def airport_traffic(self):
        raise RuntimeError("metadata store failure")


class _BytesSocket:
    """Minimal socket stand-in feeding canned bytes to HTTPResponse."""

    def __init__(self, data):
        self._file = io.BytesIO(data)

    def makefile(self, *args, **kwargs):
        return self._file


def parse_response(raw, read_body=True):
    response = http.client.HTTPResponse(_BytesSocket(raw))
    response.begin()
    return response, (response.read() if read_body else b"")


class MetadataServerTestCase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._socket_count = itertools.count()

    def start_server(self, store):
        self.socket_path = self.root / ("metadata-%d.sock" % next(self._socket_count))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(16)
        server = metadata_server.MetadataSocketServer(store, listener)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def exchange(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(self.socket_path))
            client.sendall(payload)
            chunks = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def send(self, method, target, headers=()):
        lines = ["%s %s HTTP/1.1" % (method, target)]
        lines.extend(headers)
        return self.exchange(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))


class ProfileMetadataTests(MetadataServerTestCase):
    def setUp(self):
        super().setUp()
        self.traffic = Traffic(upload=112233, download=99887766, total=123456789, expiry_ms=55)
        self.store = FakeMetadataStore(profile_traffic=self.traffic)
        self.start_server(self.store)

    def test_both_profile_files_map_to_their_fixed_internal_locations(self):
        cases = (
            ("/profile/7/Clash-Compat.yaml", "/protected/Clash-Compat.yaml"),
            ("/profile/7/Clash-Balance.yaml", "/protected/Clash-Balance.yaml"),
        )
        for target, internal in cases:
            with self.subTest(target=target):
                response, body = parse_response(
                    self.send("GET", target, ("Host: nginx",))
                )

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("X-Accel-Redirect"), internal)
                self.assertEqual(
                    response.getheader("Subscription-Userinfo"),
                    "upload=112233; download=99887766; total=123456789; expire=55",
                )
                self.assertEqual(body, b"")
        self.assertEqual(self.store.calls, [("profile", 7), ("profile", 7)])

    def test_the_client_id_reaches_the_store_as_a_canonical_integer(self):
        parse_response(self.send("GET", "/profile/42/Clash-Compat.yaml", ("Host: nginx",)))

        self.assertEqual(self.store.calls, [("profile", 42)])


class AirportMetadataTests(MetadataServerTestCase):
    def test_the_airport_file_maps_to_the_provider_location(self):
        traffic = Traffic(upload=1, download=2, total=3, expiry_ms=4)
        store = FakeMetadataStore(airport_traffic=traffic)
        self.start_server(store)

        response, body = parse_response(
            self.send("GET", "/airport/AmyTelecom.yaml", ("Host: nginx",))
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("X-Accel-Redirect"), "/protected/provider/AmyTelecom.yaml"
        )
        self.assertEqual(
            response.getheader("Subscription-Userinfo"),
            "upload=1; download=2; total=3; expire=4",
        )
        self.assertEqual(body, b"")
        self.assertEqual(store.calls, [("airport",)])


class MetadataWithoutTrafficTests(MetadataServerTestCase):
    CASES = (
        ("/profile/3/Clash-Compat.yaml", "/protected/Clash-Compat.yaml"),
        ("/airport/AmyTelecom.yaml", "/protected/provider/AmyTelecom.yaml"),
    )

    def test_missing_traffic_still_redirects_without_the_userinfo_header(self):
        store = FakeMetadataStore()
        self.start_server(store)

        for target, internal in self.CASES:
            with self.subTest(target=target):
                response, body = parse_response(
                    self.send("GET", target, ("Host: nginx",))
                )

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("X-Accel-Redirect"), internal)
                self.assertIsNone(response.getheader("Subscription-Userinfo"))
                self.assertEqual(body, b"")
        self.assertEqual(
            store.calls, [("profile", 3), ("airport",)]
        )

    def test_store_failure_degrades_to_a_redirect_without_failing_the_file(self):
        self.start_server(RaisingMetadataStore())

        for target, internal in self.CASES:
            with self.subTest(target=target):
                response, body = parse_response(
                    self.send("GET", target, ("Host: nginx",))
                )

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("X-Accel-Redirect"), internal)
                self.assertIsNone(response.getheader("Subscription-Userinfo"))
                self.assertEqual(body, b"")


class MetadataRejectionTests(MetadataServerTestCase):
    def setUp(self):
        super().setUp()
        self.store = FakeMetadataStore(
            profile_traffic=Traffic(upload=1, download=2, total=3, expiry_ms=4),
            airport_traffic=Traffic(upload=1, download=2, total=3, expiry_ms=4),
        )
        self.start_server(self.store)

    def test_every_unrecognized_request_gets_the_same_fixed_404(self):
        cases = (
            # (method, target, headers, fragments that must never leak back)
            ("GET", "/profile/3/Clash-Compat.yaml?token=SECRET", ("Host: nginx",), ("SECRET", "token=")),
            ("GET", "/profile/../../etc/passwd", ("Host: nginx",), ("etc/passwd", "../")),
            ("GET", "/profile/%2e%2e/Clash-Compat.yaml", (), ("%2e",)),
            ("GET", "/profile/3/Clash-Comp%0aat.yaml", (), ("%0a", "Comp%0a")),
            ("POST", "/profile/3/Clash-Compat.yaml", ("Host: nginx", "Content-Length: 0"), ("POST",)),
            ("HEAD", "/airport/AmyTelecom.yaml", ("Host: nginx",), ("HEAD",)),
            ("PUT", "/profile/3/Clash-Compat.yaml", ("Host: nginx", "Content-Length: 0"), ("PUT",)),
            ("GET", "/profile/3/Clash-Meta.yaml", (), ("Clash-Meta",)),
            ("GET", "/profile/three/Clash-Compat.yaml", (), ("three",)),
            ("GET", "/profile/03/Clash-Compat.yaml", (), ("03/Clash",)),
            ("GET", "/profile/0/Clash-Compat.yaml", (), ("0/Clash",)),
            ("GET", "/profile/%s/Clash-Compat.yaml" % ("9" * 20), (), ("9" * 20,)),
            ("GET", "/profile/-3/Clash-Compat.yaml", (), ("-3",)),
            ("GET", "/profile/3/Clash-Compat.yaml/extra", (), ("extra", "/Clash-Compat.yaml/")),
            ("GET", "http://127.0.0.1:9/profile/3/Clash-Compat.yaml", ("Host: nginx",), ("http", "127.0.0.1")),
            ("GET", "/Profile/3/Clash-Compat.yaml", (), ("Profile",)),
            ("GET", "/airport/Other.yaml", (), ("Other",)),
            ("GET", "/airport/AmyTelecom.yaml/extra", (), ("extra",)),
            ("GET", "/airport/amytelecom.yaml", (), ("amytelecom",)),
            ("GET", "/profile/3/clash-compat.yaml", (), ("clash-compat",)),
            ("GET", "/", (), ("/profile",)),
        )
        for method, target, headers, fragments in cases:
            with self.subTest(target=target, method=method):
                raw = self.send(method, target, headers)
                # HEAD responses carry the length a GET would send, but
                # never the body itself.
                response, body = parse_response(raw, read_body=method != "HEAD")

                self.assertEqual(response.status, 404)
                if method == "HEAD":
                    self.assertNotIn(b"not found\n", raw)
                else:
                    self.assertEqual(body, b"not found\n")
                self.assertIsNone(response.getheader("X-Accel-Redirect"))
                self.assertIsNone(response.getheader("Subscription-Userinfo"))
                for fragment in fragments:
                    self.assertNotIn(fragment.encode("utf-8"), raw)
        self.assertEqual(self.store.calls, [])

    def test_an_oversized_request_line_is_rejected_with_the_fixed_404(self):
        raw = self.exchange(b"GET /" + b"A" * 9000 + b" HTTP/1.1\r\nHost: nginx\r\n\r\n")
        response, body = parse_response(raw)

        self.assertEqual(response.status, 404)
        self.assertEqual(body, b"not found\n")
        self.assertNotIn(b"AAAAAA", raw)
        self.assertEqual(self.store.calls, [])

    def test_a_request_line_beyond_the_stdlib_limit_is_rejected_with_the_fixed_404(self):
        # Over 65536 bytes the stdlib rejects the request line itself —
        # the only path where the rejection runs before the request is
        # parsed — and the server stops reading before the client finishes
        # sending, so the send may hit a closed socket.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(self.socket_path))
            with contextlib.suppress(OSError):
                client.sendall(b"GET /" + b"A" * 70000 + b" HTTP/1.1\r\nHost: nginx\r\n\r\n")
            chunks = []
            while True:
                try:
                    chunk = client.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)

        response, body = parse_response(raw)
        self.assertEqual(response.status, 404)
        self.assertEqual(body, b"not found\n")
        self.assertNotIn(b"AAAAAA", raw)
        self.assertEqual(self.store.calls, [])

    def test_a_truncated_request_line_is_rejected_without_echoing_it(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(self.socket_path))
            client.sendall(b"GET /profile/3/Clash-Compat.yaml")
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)

        self.assertEqual(parse_response(raw)[0].status, 404)
        self.assertNotIn(b"Clash-Compat", raw)
        self.assertEqual(self.store.calls, [])


class MetadataLoggingTests(MetadataServerTestCase):
    def test_requests_and_connection_failures_write_nothing_to_stderr(self):
        server = self.start_server(
            FakeMetadataStore(profile_traffic=Traffic(upload=1, download=2, total=3, expiry_ms=4))
        )
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            response, _ = parse_response(
                self.send("GET", "/profile/7/Clash-Compat.yaml", ("Host: nginx",))
            )
            self.assertEqual(response.status, 200)
            response, _ = parse_response(self.send("GET", "/profile/../../etc/passwd"))
            self.assertEqual(response.status, 404)
            # A half-sent request line and an abruptly dropped connection
            # must both stay quiet too.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(str(self.socket_path))
                client.sendall(b"GET /profile/7/Clash")
                client.shutdown(socket.SHUT_WR)
                while client.recv(4096):
                    pass
            dropped = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            dropped.connect(str(self.socket_path))
            dropped.sendall(b"GET /profile/7/Cla")
            dropped.close()
            server.shutdown()
            server.server_close()

        self.assertEqual(captured.getvalue(), "")


class MetadataServerContractTests(unittest.TestCase):
    def test_handler_enforces_a_short_connection_timeout(self):
        timeout = metadata_server.MetadataRequestHandler.timeout
        self.assertGreaterEqual(timeout, 1)
        self.assertLessEqual(timeout, 10)

    def test_listener_helper_requires_the_exact_sd_listen_fds_contract(self):
        received = object()
        seen = {}

        def fake_fromfd(descriptor, family, kind):
            seen["call"] = (descriptor, family, kind)
            return received

        valid = {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid())}
        listener = metadata_server.listener_from_environment(valid, fromfd=fake_fromfd)

        self.assertIs(listener, received)
        self.assertEqual(seen["call"], (3, socket.AF_UNIX, socket.SOCK_STREAM))
        for bad in (
            {},
            {"LISTEN_FDS": "1"},
            {"LISTEN_PID": str(os.getpid())},
            {"LISTEN_FDS": "2", "LISTEN_PID": str(os.getpid())},
            {"LISTEN_FDS": "01", "LISTEN_PID": str(os.getpid())},
            {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid() + 1)},
            {"LISTEN_FDS": "1 ", "LISTEN_PID": str(os.getpid())},
        ):
            with self.subTest(environ=bad):
                self.assertRaises(
                    metadata_server.MetadataServerError,
                    metadata_server.listener_from_environment,
                    bad,
                    fromfd=fake_fromfd,
                )


if __name__ == "__main__":
    unittest.main()
