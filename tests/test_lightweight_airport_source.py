"""Airport source record model, serialization, and safe reading."""

import json
import os
import stat
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory

from clash_sub.airport_source import (
    AIRPORT_SOURCE_FILENAME,
    AirportSource,
    AirportSourceError,
    parse_source,
    read_source_file,
    serialize_source,
)
from clash_sub.domain import Traffic

SOURCE_URL = "https://example.invalid/subscription"
ACTIVATION_URL = "https://example.invalid/Subscription/index?sid=placeholder&token=placeholder"
SOURCE_TRAFFIC = Traffic(upload=1, download=2, total=3, expiry_ms=4)
LAST_SUCCESS = 1788192000


def sample_source():
    return AirportSource(
        source_url=SOURCE_URL,
        traffic=SOURCE_TRAFFIC,
        last_success=LAST_SUCCESS,
        activation_url=ACTIVATION_URL,
    )


class AirportSourceModelTests(unittest.TestCase):
    def test_fields_follow_the_fixed_interface(self):
        source = sample_source()
        self.assertEqual(source.source_url, SOURCE_URL)
        self.assertEqual(source.traffic, SOURCE_TRAFFIC)
        self.assertEqual(source.last_success, LAST_SUCCESS)
        self.assertEqual(source.activation_url, ACTIVATION_URL)

    def test_model_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            sample_source().source_url = "https://airport.example/other"

    def test_traffic_may_be_none(self):
        source = AirportSource(
            source_url="https://airport.example/other",
            traffic=None,
            last_success=7,
            activation_url=None,
        )
        self.assertIsNone(source.traffic)

    def test_activation_url_may_be_none(self):
        source = AirportSource(
            source_url="https://airport.example/other",
            traffic=None,
            last_success=7,
            activation_url=None,
        )
        self.assertIsNone(source.activation_url)


class SerializationTests(unittest.TestCase):
    def test_serialize_uses_the_exact_fixed_schema(self):
        payload = json.loads(serialize_source(sample_source()))
        self.assertEqual(
            payload,
            {
                "schema_version": 2,
                "activation_url": ACTIVATION_URL,
                "source_url": SOURCE_URL,
                "traffic": {"upload": 1, "download": 2, "total": 3, "expire": 4},
                "last_success": LAST_SUCCESS,
            },
        )

    def test_serialized_traffic_may_be_null(self):
        payload = json.loads(
            serialize_source(
                AirportSource(
                    source_url="https://airport.example/other",
                    traffic=None,
                    last_success=7,
                    activation_url=None,
                )
            )
        )
        self.assertIsNone(payload["traffic"])
        self.assertIsNone(payload["activation_url"])

    def test_round_trip_preserves_the_record(self):
        records = (
            sample_source(),
            AirportSource(
                source_url="https://airport.example/other",
                traffic=None,
                last_success=0,
                activation_url=None,
            ),
        )
        for record in records:
            self.assertEqual(parse_source(serialize_source(record)), record)

    def test_serialization_rejects_invalid_records(self):
        invalid = (
            "not a source",
            None,
            AirportSource(source_url="", traffic=SOURCE_TRAFFIC, last_success=1, activation_url=None),
            AirportSource(source_url=123, traffic=SOURCE_TRAFFIC, last_success=1, activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic="traffic", last_success=1, activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic=Traffic(True, 1, 1, 1), last_success=1, activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic=Traffic(-1, 1, 1, 1), last_success=1, activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic=Traffic(1.5, 1, 1, 1), last_success=1, activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic=None, last_success=True, activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic=None, last_success=-1, activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic=None, last_success="1", activation_url=None),
            AirportSource(source_url="https://airport.example/other", traffic=None, last_success=1, activation_url=""),
            AirportSource(source_url="https://airport.example/other", traffic=None, last_success=1, activation_url=123),
            AirportSource(source_url="https://airport.example/other", traffic=None, last_success=1, activation_url=True),
        )
        for record in invalid:
            with self.assertRaises(AirportSourceError) as caught:
                serialize_source(record)
            self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_parse_rejects_invalid_payloads(self):
        valid = json.loads(serialize_source(sample_source()))

        def variant(**changes):
            payload = dict(valid)
            payload["traffic"] = dict(payload["traffic"])
            payload.update(changes)
            return json.dumps(payload).encode("utf-8")

        corrupt = [
            b"{not json",
            b"",
            b"[]",
            variant(schema_version=1),
            variant(schema_version=3),
            variant(schema_version=True),
            variant(source_url=123),
            variant(source_url=""),
            variant(last_success=-1),
            variant(last_success=True),
            variant(last_success="1788192000"),
            variant(activation_url=""),
            variant(activation_url=123),
            variant(activation_url=True),
            variant(traffic={"upload": 1, "download": 2, "total": 3}),
            variant(
                traffic={
                    "upload": 1,
                    "download": 2,
                    "total": 3,
                    "expire": 4,
                    "extra": 5,
                }
            ),
            variant(traffic={"upload": -1, "download": 2, "total": 3, "expire": 4}),
            variant(traffic={"upload": True, "download": 2, "total": 3, "expire": 4}),
            variant(traffic={"upload": "1", "download": 2, "total": 3, "expire": 4}),
            variant(traffic=[]),
            variant(extra=1),
            json.dumps(
                {key: value for key, value in valid.items() if key != "source_url"}
            ).encode("utf-8"),
            json.dumps(
                {key: value for key, value in valid.items() if key != "activation_url"}
            ).encode("utf-8"),
        ]
        for payload in corrupt:
            with self.assertRaises(AirportSourceError) as caught:
                parse_source(payload)
            self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_parse_accepts_null_traffic(self):
        source = parse_source(
            json.dumps(
                {
                    "schema_version": 2,
                    "activation_url": None,
                    "source_url": "https://airport.example/other",
                    "traffic": None,
                    "last_success": 5,
                }
            ).encode("utf-8")
        )
        self.assertEqual(
            source,
            AirportSource(
                source_url="https://airport.example/other",
                traffic=None,
                last_success=5,
                activation_url=None,
            ),
        )


class ReadSourceFileTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        os.chmod(self.root, 0o700)
        self.path = self.root / AIRPORT_SOURCE_FILENAME
        self.path.write_bytes(serialize_source(sample_source()))
        os.chmod(self.path, 0o600)

    def test_filename_is_fixed(self):
        self.assertEqual(AIRPORT_SOURCE_FILENAME, "airport-source.json")

    def test_read_returns_the_stored_record(self):
        self.assertEqual(read_source_file(self.path), sample_source())

    def test_missing_file_reports_the_stable_missing_code(self):
        self.path.unlink()
        with self.assertRaises(AirportSourceError) as caught:
            read_source_file(self.path)
        self.assertEqual(caught.exception.code, "airport_source_missing")

    def test_stored_file_satisfies_the_ownership_contract(self):
        expected_uid = 0 if os.geteuid() == 0 else os.geteuid()
        details = self.path.lstat()
        self.assertFalse(self.path.is_symlink())
        self.assertTrue(stat.S_ISREG(details.st_mode))
        self.assertEqual(stat.S_IMODE(details.st_mode), 0o600)
        self.assertEqual(details.st_nlink, 1)
        self.assertEqual(details.st_uid, expected_uid)

    def test_hard_linked_record_is_rejected(self):
        link = self.root / "hardlink.json"
        os.link(self.path, link)
        with self.assertRaises(AirportSourceError) as caught:
            read_source_file(self.path)
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_wrong_mode_record_is_rejected(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(AirportSourceError) as caught:
            read_source_file(self.path)
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_symlinked_record_is_rejected(self):
        self.path.unlink()
        outside = self.root.parent / "outside-source.json"
        outside.write_bytes(serialize_source(sample_source()))
        self.path.symlink_to(outside)
        with self.assertRaises(AirportSourceError) as caught:
            read_source_file(self.path)
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_directory_record_is_rejected(self):
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(AirportSourceError) as caught:
            read_source_file(self.path)
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_invalid_content_is_rejected(self):
        self.path.write_bytes(b"{not json")
        with self.assertRaises(AirportSourceError) as caught:
            read_source_file(self.path)
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_wrong_schema_content_is_rejected(self):
        self.path.write_bytes(
            json.dumps(
                {
                    "schema_version": 3,
                    "activation_url": None,
                    "source_url": "https://example.invalid/subscription",
                    "traffic": None,
                    "last_success": 1,
                }
            ).encode("utf-8")
        )
        with self.assertRaises(AirportSourceError) as caught:
            read_source_file(self.path)
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_errors_never_expose_paths_urls_or_traffic_values(self):
        self.path.write_bytes(b"{not json")
        messages = []
        try:
            read_source_file(self.path)
        except AirportSourceError as error:
            messages.append(str(error))
        self.path.unlink()
        try:
            read_source_file(self.path)
        except AirportSourceError as error:
            messages.append(str(error))
        for message in messages:
            self.assertNotIn("airport-source.json", message)
            self.assertNotIn("example.invalid", message)
            self.assertNotIn("1788192000", message)


if __name__ == "__main__":
    unittest.main()
