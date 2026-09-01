"""On-demand subscription traffic metadata cache."""

import json
import os
import stat
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from clash_sub import metadata
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
            make_snapshot(make_client(7, upload=10, download=20, total=30, expiry_ms=40))
        )
        store = self.make_store(reader)
        self.assertEqual(
            store.traffic_for(7), Traffic(upload=10, download=20, total=30, expiry_ms=40)
        )
        self.assertEqual(reader.calls, [self.database])

    def test_query_within_ttl_hits_the_cache(self):
        reader = RecordingReader(make_snapshot(make_client(7, upload=1, download=2, total=3, expiry_ms=4)))
        store = self.make_store(reader)
        store.traffic_for(7)
        self.clock.advance(CACHE_TTL_SECONDS - 1)
        self.assertEqual(
            store.traffic_for(7), Traffic(upload=1, download=2, total=3, expiry_ms=4)
        )
        self.assertEqual(len(reader.calls), 1)

    def test_query_after_ttl_refreshes(self):
        reader = RecordingReader(
            make_snapshot(make_client(7, upload=1, download=2, total=3, expiry_ms=4)),
            make_snapshot(make_client(7, upload=5, download=6, total=7, expiry_ms=8)),
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
                make_client(1, upload=11, download=12, total=13, expiry_ms=14),
                make_client(2, upload=21, download=22, total=23, expiry_ms=24),
                make_client(3, upload=31, download=32, total=33, expiry_ms=34),
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
            make_client(5, upload=10, download=20, total=30, expiry_ms=40)
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
            make_snapshot(make_client(4, upload=1, download=2, total=3, expiry_ms=4)),
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
            make_snapshot(make_client(4, upload=9, download=8, total=7, expiry_ms=6))
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
                make_client(1, upload=1, download=1, total=1, expiry_ms=1),
                make_client(2, enabled=False, upload=2, download=2, total=2, expiry_ms=2),
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
                make_client(1, upload=100, download=200, total=300, expiry_ms=400),
                make_client(2, upload=101, download=201, total=301, expiry_ms=401),
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
        reader = RecordingReader(make_snapshot(make_client(3, upload=1, download=1, total=1, expiry_ms=1)))
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
                    make_snapshot(make_client(3, upload=1, download=1, total=1, expiry_ms=1))
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
        reader = RecordingReader(make_snapshot(make_client(2, upload=1, download=1, total=1, expiry_ms=1)))
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
        reader = RecordingReader(make_snapshot(make_client(2, upload=1, download=1, total=1, expiry_ms=1)))
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
        reader = RecordingReader(make_snapshot(make_client(2, upload=1, download=1, total=1, expiry_ms=1)))
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
            make_snapshot(make_client(1, upload=5, download=6, total=7, expiry_ms=8)),
            make_snapshot(make_client(1, upload=5, download=6, total=7, expiry_ms=8)),
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


if __name__ == "__main__":
    unittest.main()
