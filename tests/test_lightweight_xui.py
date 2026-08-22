import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from clash_sub.xui import XuiCompatibilityError, read_xui_snapshot


FIXTURE = Path(__file__).parent / "fixtures" / "xui-3.6.0.sql"
COMPATIBILITY_ERROR = "3x-ui database compatibility check failed"


class XuiSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._initialize_database()

    def tearDown(self):
        self.tempdir.cleanup()

    def assert_incompatible(self):
        with self.assertRaises(XuiCompatibilityError) as raised:
            read_xui_snapshot(self.database)
        self.assertEqual(str(raised.exception), COMPATIBILITY_ERROR)

    def test_constructs_only_loopback_clash_urls(self):
        snapshot = read_xui_snapshot(self.database)

        self.assertEqual(
            snapshot.source_url(snapshot.clients[0]),
            "http://127.0.0.1:2096/clash/member-sub-id",
        )

    def test_quotes_sub_id_in_clash_url(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE clients SET sub_id = ? WHERE id = ?", ("member/id value", 3)
            )

        snapshot = read_xui_snapshot(self.database)

        self.assertEqual(
            snapshot.source_url(snapshot.clients[0]),
            "http://127.0.0.1:2096/clash/member%2Fid%20value",
        )

    def test_orders_clients_by_database_id_and_left_joins_traffic(self):
        snapshot = read_xui_snapshot(self.database)

        self.assertEqual([client.client_id for client in snapshot.clients], [3, 9])
        self.assertEqual(
            [(client.upload, client.download) for client in snapshot.clients],
            [(12, 34), (0, 0)],
        )
        self.assertEqual([client.total for client in snapshot.clients], [1000, 2000])
        self.assertEqual([client.expiry_ms for client in snapshot.clients], [123456789, 0])

    def test_uses_read_only_uri_and_query_only_without_write_sql(self):
        real_connect = sqlite3.connect
        calls = []
        executed = []

        class TrackingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, parameters=()):
                executed.append(sql)
                return self.connection.execute(sql, parameters)

            def close(self):
                self.connection.close()

        def tracked_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return TrackingConnection(real_connect(*args, **kwargs))

        with patch("clash_sub.xui.sqlite3.connect", side_effect=tracked_connect):
            read_xui_snapshot(self.database)

        self.assertEqual(
            calls,
            [
                (
                    ("file:%s?mode=ro" % quote(str(self.database)),),
                    {"uri": True, "timeout": 1.0},
                )
            ],
        )
        self.assertIn("PRAGMA query_only=ON", executed)
        self.assertTrue(
            all(sql.lstrip().upper().startswith(("SELECT", "PRAGMA")) for sql in executed)
        )

    def test_rejects_missing_table_or_column(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP TABLE client_traffics")
        self.assert_incompatible()

        self._initialize_database()
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP TABLE clients")
            connection.execute(
                """
                CREATE TABLE clients (
                  id INTEGER PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  enable NUMERIC NOT NULL,
                  total_gb INTEGER NOT NULL,
                  expiry_time INTEGER NOT NULL
                )
                """
            )
        self.assert_incompatible()

    def test_rejects_duplicate_or_empty_client_identity(self):
        cases = (
            (
                "duplicate id",
                (
                    (3, "first@example.test", "first", 1, 1, 1),
                    (3, "second@example.test", "second", 1, 1, 1),
                ),
            ),
            (
                "duplicate email",
                (
                    (3, "same@example.test", "first", 1, 1, 1),
                    (4, "same@example.test", "second", 1, 1, 1),
                ),
            ),
            (
                "duplicate sub id",
                (
                    (3, "first@example.test", "same", 1, 1, 1),
                    (4, "second@example.test", "same", 1, 1, 1),
                ),
            ),
            (
                "empty sub id",
                ((3, "first@example.test", "", 1, 1, 1),),
            ),
        )
        for label, rows in cases:
            with self.subTest(label):
                self._initialize_database()
                self._replace_clients(rows)
                self.assert_incompatible()

    def test_rejects_invalid_or_disabled_settings(self):
        cases = (
            ("non-loopback listener", "subListen", "0.0.0.0"),
            ("invalid port", "subPort", "0"),
            ("invalid path", "subClashPath", "clash/"),
            ("disabled subscription", "subEnable", "false"),
            ("disabled clash", "subClashEnable", "false"),
        )
        for label, key, value in cases:
            with self.subTest(label):
                self._initialize_database()
                with sqlite3.connect(self.database) as connection:
                    connection.execute(
                        "UPDATE settings SET value = ? WHERE key = ?", (value, key)
                    )
                self.assert_incompatible()

    def _initialize_database(self):
        self.database = Path(self.tempdir.name) / ("x-ui-%s.db" % id(self))
        if self.database.exists():
            self.database.unlink()
        with sqlite3.connect(self.database) as connection:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
            connection.executemany(
                """
                INSERT INTO clients (id, email, sub_id, enable, total_gb, expiry_time)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (3, "member@example.test", "member-sub-id", 1, 1000, 123456789),
                    (9, "disabled@example.test", "disabled-sub-id", 0, 2000, -1),
                ),
            )
            connection.execute(
                "INSERT INTO client_traffics (email, up, down) VALUES (?, ?, ?)",
                ("member@example.test", 12, 34),
            )

    def _replace_clients(self, rows):
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP TABLE clients")
            connection.execute(
                """
                CREATE TABLE clients (
                  id INTEGER,
                  email TEXT,
                  sub_id TEXT,
                  enable NUMERIC,
                  total_gb INTEGER,
                  expiry_time INTEGER
                )
                """
            )
            connection.executemany("INSERT INTO clients VALUES (?, ?, ?, ?, ?, ?)", rows)


if __name__ == "__main__":
    unittest.main()
