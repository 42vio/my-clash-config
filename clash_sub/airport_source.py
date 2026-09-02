"""Private airport source record: model, serialization, and safe reading."""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from clash_sub.domain import Traffic

AIRPORT_SOURCE_FILENAME = "airport-source.json"
SCHEMA_VERSION = 2
_SOURCE_MODE = 0o600
_RECORD_KEYS = frozenset({
    "activation_url", "last_success", "schema_version", "source_url", "traffic"
})
_TRAFFIC_KEYS = frozenset({"download", "expire", "total", "upload"})


class AirportSourceError(RuntimeError):
    """A redacted, stable airport source failure."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AirportSource:
    """The persisted provenance of the current airport provider file."""

    source_url: str
    traffic: Traffic | None
    last_success: int
    activation_url: str | None


def serialize_source(source) -> bytes:
    """Encode one record into the exact fixed JSON schema."""
    if not isinstance(source, AirportSource):
        _invalid()
    source_url = source.source_url
    traffic = source.traffic
    last_success = source.last_success
    activation_url = source.activation_url
    if not isinstance(source_url, str) or not source_url:
        _invalid()
    if _bad_int(last_success) or last_success < 0:
        _invalid()
    if activation_url is not None and (not isinstance(activation_url, str) or not activation_url):
        _invalid()
    traffic_payload = None
    if traffic is not None:
        if not isinstance(traffic, Traffic):
            _invalid()
        fields = {
            "upload": traffic.upload,
            "download": traffic.download,
            "total": traffic.total,
            "expire": traffic.expiry_ms,
        }
        if any(_bad_int(value) or value < 0 for value in fields.values()):
            _invalid()
        traffic_payload = fields
    payload = {
        "schema_version": SCHEMA_VERSION,
        "activation_url": activation_url,
        "source_url": source_url,
        "traffic": traffic_payload,
        "last_success": last_success,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def parse_source(payload) -> AirportSource:
    """Decode one record from the exact fixed JSON schema."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        _invalid()
    if not isinstance(document, dict) or set(document) != _RECORD_KEYS:
        _invalid()
    schema_version = document["schema_version"]
    activation_url = document["activation_url"]
    source_url = document["source_url"]
    traffic = document["traffic"]
    last_success = document["last_success"]
    if _bad_int(schema_version) or schema_version != SCHEMA_VERSION:
        _invalid()
    if activation_url is not None and (not isinstance(activation_url, str) or not activation_url):
        _invalid()
    if not isinstance(source_url, str) or not source_url:
        _invalid()
    if _bad_int(last_success) or last_success < 0:
        _invalid()
    parsed_traffic = None if traffic is None else _traffic_from_payload(traffic)
    return AirportSource(
        source_url=source_url,
        traffic=parsed_traffic,
        last_success=last_success,
        activation_url=activation_url,
    )


def read_source_file(path, *, expected_uid=None) -> AirportSource:
    """Read one record after enforcing the private file discipline."""
    path = Path(path)
    try:
        details = path.lstat()
    except FileNotFoundError:
        raise AirportSourceError("airport_source_missing") from None
    except OSError:
        raise AirportSourceError("airport_source_invalid") from None
    uid = _expected_uid(expected_uid)
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != uid
        or stat.S_IMODE(details.st_mode) != _SOURCE_MODE
    ):
        raise AirportSourceError("airport_source_invalid")
    try:
        payload = path.read_bytes()
    except OSError:
        raise AirportSourceError("airport_source_invalid") from None
    return parse_source(payload)


def _traffic_from_payload(traffic) -> Traffic:
    if not isinstance(traffic, dict) or set(traffic) != _TRAFFIC_KEYS:
        _invalid()
    values = {}
    for key in sorted(_TRAFFIC_KEYS):
        value = traffic[key]
        if _bad_int(value) or value < 0:
            _invalid()
        values[key] = value
    return Traffic(
        upload=values["upload"],
        download=values["download"],
        total=values["total"],
        expiry_ms=values["expire"],
    )


def _bad_int(value):
    return isinstance(value, bool) or not isinstance(value, int)


def _expected_uid(value=None):
    if value is None:
        return 0 if os.geteuid() == 0 else os.geteuid()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AirportSourceError("airport_source_invalid")
    return value


def _invalid():
    raise AirportSourceError("airport_source_invalid")
