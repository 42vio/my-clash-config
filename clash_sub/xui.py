import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from clash_sub.domain import Traffic, XuiClient, XuiSnapshot


class XuiCompatibilityError(RuntimeError):
    """Raised when the pinned 3x-ui database schema is incompatible."""


_ERROR = "3x-ui database compatibility check failed"
_REALITY_INBOUND_PORT = 10443
_REQUIRED_COLUMNS = {
    "clients": {
        "id",
        "email",
        "sub_id",
        "enable",
        "total_gb",
        "expiry_time",
    },
    "client_traffics": {"id", "email", "up", "down"},
    "settings": {"key", "value"},
    "inbounds": {"id", "port", "protocol", "enable", "stream_settings"},
}
_REQUIRED_SETTINGS = (
    "subListen",
    "subPort",
    "subEnable",
    "subClashEnable",
    "subClashPath",
)


def read_xui_snapshot(path: Path, now_ms: int | None = None) -> XuiSnapshot:
    """Read the supported 3x-ui client and Clash subscription snapshot."""
    try:
        current_time_ms = _current_time_ms(now_ms)
        connection = sqlite3.connect(
            "file:%s?mode=ro" % quote(str(path)), uri=True, timeout=1.0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            _validate_schema(connection)
            listen, port, clash_path = _read_settings(connection)
            clients = _read_clients(connection, current_time_ms)
            _validate_reality_inbound(connection)
        finally:
            connection.close()
    except XuiCompatibilityError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        raise XuiCompatibilityError(_ERROR) from None
    return XuiSnapshot(
        clients=tuple(clients), listen=listen, port=port, clash_path=clash_path
    )


def _validate_schema(connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not set(_REQUIRED_COLUMNS).issubset(tables):
        _fail()
    for table, expected_columns in _REQUIRED_COLUMNS.items():
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)
        }
        if not expected_columns.issubset(columns):
            _fail()


def _read_settings(connection) -> tuple[str, int, str]:
    placeholders = ", ".join("?" for _ in _REQUIRED_SETTINGS)
    rows = connection.execute(
        "SELECT key, value FROM settings WHERE key IN (%s)" % placeholders,
        _REQUIRED_SETTINGS,
    ).fetchall()
    values = {}
    for key, value in rows:
        if key in values:
            _fail()
        values[key] = value
    if set(values) != set(_REQUIRED_SETTINGS):
        _fail()

    listen = values["subListen"]
    if listen != "127.0.0.1":
        _fail()
    port = _port(values["subPort"])
    if values["subEnable"] != "true" or values["subClashEnable"] != "true":
        _fail()
    return listen, port, _clash_path(values["subClashPath"])


def _read_clients(connection, current_time_ms: int) -> list[XuiClient]:
    rows = connection.execute(
        """
        SELECT clients.id, clients.email, clients.sub_id, clients.enable,
               clients.total_gb, clients.expiry_time,
               COALESCE(client_traffics.up, 0),
               COALESCE(client_traffics.down, 0)
        FROM clients
        LEFT JOIN client_traffics ON client_traffics.email = clients.email
        ORDER BY clients.id
        """
    ).fetchall()
    client_ids = set()
    emails = set()
    sub_ids = set()
    clients = []
    for row in rows:
        client_id, email, sub_id, enabled, total, expiry_ms, upload, download = row
        if (
            type(client_id) is not int
            or client_id <= 0
            or not _identity(email)
            or not _identity(sub_id)
            or enabled not in (0, 1)
            or not _nonnegative_integer(total)
            or type(expiry_ms) is not int
            or not _nonnegative_integer(upload)
            or not _nonnegative_integer(download)
            or client_id in client_ids
            or email in emails
            or sub_id in sub_ids
        ):
            _fail()
        client_ids.add(client_id)
        emails.add(email)
        sub_ids.add(sub_id)
        traffic = Traffic(
            upload=upload,
            download=download,
            total=total,
            expiry_ms=_normalized_expiry(expiry_ms, current_time_ms),
        )
        clients.append(
            XuiClient(
                client_id=client_id,
                email=email,
                sub_id=sub_id,
                enabled=bool(enabled),
                upload=traffic.upload,
                download=traffic.download,
                total=traffic.total,
                expiry_ms=traffic.expiry_ms,
            )
        )
    return clients


def _validate_reality_inbound(connection) -> None:
    rows = connection.execute(
        "SELECT port, protocol, enable, stream_settings FROM inbounds"
    ).fetchall()
    reality = [
        row for row in rows if row[1] == "vless" and "reality" in str(row[3]).lower()
    ]
    if (
        len(reality) != 1
        or reality[0][2] != 1
        or reality[0][0] != _REALITY_INBOUND_PORT
    ):
        _fail()


def read_panel_port(path: Path) -> int:
    """Read the 3x-ui web panel port from the settings table."""
    try:
        connection = sqlite3.connect(
            "file:%s?mode=ro" % quote(str(path)), uri=True, timeout=1.0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            _validate_schema(connection)
            rows = connection.execute(
                "SELECT value FROM settings WHERE key = 'port'"
            ).fetchall()
        finally:
            connection.close()
    except XuiCompatibilityError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        raise XuiCompatibilityError(_ERROR) from None
    if len(rows) != 1:
        _fail()
    return _port(rows[0][0])


def _port(value) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= int(value) <= 65535
    ):
        _fail()
    return int(value)


def _current_time_ms(now_ms: int | None) -> int:
    if now_ms is None:
        return time.time_ns() // 1_000_000
    if not _nonnegative_integer(now_ms):
        _fail()
    return now_ms


def _normalized_expiry(expiry_ms: int, current_time_ms: int) -> int:
    if expiry_ms < 0:
        return current_time_ms + -expiry_ms
    return expiry_ms


def _clash_path(value) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~/"
    if (
        not isinstance(value, str)
        or len(value) < 3
        or not value.startswith("/")
        or not value.endswith("/")
        or "//" in value
        or any(character not in allowed for character in value)
    ):
        _fail()
    segments = value.strip("/").split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        _fail()
    return value


def _identity(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonnegative_integer(value) -> bool:
    return type(value) is int and value >= 0


def _fail() -> None:
    raise XuiCompatibilityError(_ERROR)
