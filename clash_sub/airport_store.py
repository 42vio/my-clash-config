"""Atomic storage for the single stable airport provider file."""

import grp
import json
import os
import stat
import tempfile
from pathlib import Path

from clash_sub.airport_source import (
    AIRPORT_SOURCE_FILENAME,
    AirportSource,
    AirportSourceError,
    read_source_file,
    serialize_source,
)
from clash_sub.domain import AIRPORT_FILENAME

MAX_PROVIDER_BYTES = 5 * 1024 * 1024
_PROVIDER_MODE = 0o640
_PROVIDER_DIRECTORY = "provider"
_SOURCE_MODE = 0o600
AIRPORT_TRANSACTION_FILENAME = "airport-transaction.json"
_JOURNAL_MODE = 0o600
_JOURNAL_SCHEMA_VERSION = 1
_PROVIDER_CANDIDATE_PREFIX = ".%s." % AIRPORT_FILENAME
_SOURCE_CANDIDATE_PREFIX = ".%s." % AIRPORT_SOURCE_FILENAME
_JOURNAL_CANDIDATE_PREFIX = ".%s." % AIRPORT_TRANSACTION_FILENAME


class AirportStoreError(RuntimeError):
    """A redacted, stable airport provider failure."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _os_replace(source, target):
    os.replace(source, target)


def _os_write(descriptor, data):
    return os.write(descriptor, data)


def _os_fsync(descriptor):
    os.fsync(descriptor)


def _os_unlink(path):
    os.unlink(path)


def _expected_uid(value=None):
    if value is None:
        return 0 if os.geteuid() == 0 else os.geteuid()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AirportStoreError("airport_provider_invalid")
    return value


def _expected_public_gid(value=None):
    if value is None:
        if os.geteuid() != 0:
            return os.getegid()
        try:
            return grp.getgrnam("www-data").gr_gid
        except KeyError:
            raise AirportStoreError("airport_provider_invalid") from None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AirportStoreError("airport_provider_invalid")
    return value


def _source_payload(source):
    if not isinstance(source, AirportSource):
        raise AirportStoreError("airport_source_invalid")
    try:
        return serialize_source(source)
    except AirportSourceError:
        raise AirportStoreError("airport_source_invalid") from None


class AirportStore:
    """Publish the owner-only airport provider through one stable file.

    Every access must be serialized by the service operation lock
    (private/operation.lock): read(), read_source(), and replace() all run
    recover() first, and recovery may WRITE when it rolls a pending
    transaction forward.  A second process (for example the metadata
    server) must therefore not call these unsynchronized; it needs to take
    the lock or use a non-recovering read instead.
    """

    def __init__(self, private_root, public_root, *, expected_uid=None, expected_public_gid=None):
        self._private_root = Path(private_root)
        self._public_root = Path(public_root)
        self._source_path = self._private_root / AIRPORT_SOURCE_FILENAME
        self._journal_path = self._private_root / AIRPORT_TRANSACTION_FILENAME
        self._expected_uid = _expected_uid(expected_uid)
        self._expected_gid = _expected_public_gid(expected_public_gid)

    @property
    def path(self) -> Path:
        return self._public_root / _PROVIDER_DIRECTORY / AIRPORT_FILENAME

    def read(self) -> bytes:
        """Return the exact bytes of the current provider."""
        self.recover()
        self._require_provider_file(required=True)
        try:
            payload = self.path.read_bytes()
        except OSError:
            raise AirportStoreError("airport_provider_invalid") from None
        if not payload or len(payload) > MAX_PROVIDER_BYTES:
            raise AirportStoreError("airport_provider_invalid")
        return payload

    def read_source(self) -> AirportSource:
        """Return the provenance record of the current provider."""
        self.recover()
        self._require_private_root()
        return self._read_source_record()

    def replace(self, document, source) -> Path:
        """Atomically switch the provider document and its source record."""
        self.recover()
        if (
            not isinstance(document, bytes)
            or not document
            or len(document) > MAX_PROVIDER_BYTES
        ):
            raise AirportStoreError("airport_provider_invalid")
        payload = _source_payload(source)
        provider_directory = self._require_provider_directory()
        private_root = self._require_private_root()
        self._require_provider_file(required=False)
        self._require_source_file()
        provider_candidate = _new_candidate(
            provider_directory,
            _PROVIDER_CANDIDATE_PREFIX,
            _PROVIDER_MODE,
            self._expected_uid,
            self._expected_gid,
            document,
        )
        source_candidate = None
        journal_ready = False
        try:
            source_candidate = _new_candidate(
                private_root,
                _SOURCE_CANDIDATE_PREFIX,
                _SOURCE_MODE,
                self._expected_uid,
                None,
                payload,
            )
            _write_journal(private_root, self._journal_path, self._expected_uid, provider_candidate, source_candidate)
            journal_ready = True
            _os_replace(provider_candidate, self.path)
            _os_replace(source_candidate, self._source_path)
            _fsync_directory(provider_directory)
            _fsync_directory(private_root)
            _remove_journal(self._journal_path, private_root)
        except AirportStoreError:
            if not journal_ready:
                _remove_quietly(provider_candidate)
                _remove_quietly(source_candidate)
            raise
        except OSError:
            if not journal_ready:
                _remove_quietly(provider_candidate)
                _remove_quietly(source_candidate)
            raise AirportStoreError("airport_provider_write_failed") from None
        return self.path

    def recover(self):
        """Finish or discard a pending airport transaction; idempotent."""
        try:
            self._journal_path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise AirportStoreError("airport_source_invalid") from None
        provider_directory = self._require_provider_directory()
        private_root = self._require_private_root()
        journal = self._read_journal()
        if journal is None:
            self._discard_corrupt_journal(provider_directory, private_root)
            return
        self._apply_or_verify(
            provider_directory / journal["provider"],
            self.path,
            mode=_PROVIDER_MODE,
            gid=self._expected_gid,
            invalid_code="airport_provider_invalid",
        )
        self._apply_or_verify(
            private_root / journal["source"],
            self._source_path,
            mode=_SOURCE_MODE,
            gid=None,
            invalid_code="airport_source_invalid",
        )
        _fsync_directory(provider_directory)
        _fsync_directory(private_root)
        _remove_journal(self._journal_path, private_root)

    def _apply_or_verify(self, candidate, target, *, mode, gid, invalid_code):
        try:
            details = candidate.lstat()
        except FileNotFoundError:
            self._require_final_file(target, mode=mode, gid=gid, invalid_code=invalid_code)
            return
        except OSError:
            raise AirportStoreError(invalid_code) from None
        _require_file_shape(
            details, mode=mode, gid=gid, uid=self._expected_uid, invalid_code=invalid_code
        )
        try:
            _os_replace(candidate, target)
        except OSError:
            raise AirportStoreError("airport_provider_write_failed") from None

    def _require_final_file(self, target, *, mode, gid, invalid_code):
        try:
            details = target.lstat()
        except OSError:
            raise AirportStoreError("airport_provider_write_failed") from None
        _require_file_shape(
            details, mode=mode, gid=gid, uid=self._expected_uid, invalid_code=invalid_code
        )

    def _read_journal(self):
        try:
            details = self._journal_path.lstat()
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != self._expected_uid
                or stat.S_IMODE(details.st_mode) != _JOURNAL_MODE
            ):
                return None
            payload = json.loads(self._journal_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "provider", "source"}
            or isinstance(payload.get("schema_version"), bool)
            or payload["schema_version"] != _JOURNAL_SCHEMA_VERSION
        ):
            return None
        names = {}
        for key, prefix in (
            ("provider", _PROVIDER_CANDIDATE_PREFIX),
            ("source", _SOURCE_CANDIDATE_PREFIX),
        ):
            name = payload[key]
            if (
                not isinstance(name, str)
                or not name.startswith(prefix)
                or Path(name).name != name
                or name in {".", ".."}
            ):
                return None
            names[key] = name
        return names

    def _discard_corrupt_journal(self, provider_directory, private_root):
        if self._candidates_pending(provider_directory, private_root):
            raise AirportStoreError("airport_source_invalid")
        try:
            _os_unlink(self._journal_path)
        except OSError:
            raise AirportStoreError("airport_provider_write_failed") from None
        try:
            _fsync_directory(private_root)
        except OSError:
            raise AirportStoreError("airport_provider_write_failed") from None

    def _candidates_pending(self, provider_directory, private_root):
        prefixes = (_PROVIDER_CANDIDATE_PREFIX, _SOURCE_CANDIDATE_PREFIX)
        for directory in (provider_directory, private_root):
            try:
                names = [entry.name for entry in os.scandir(directory)]
            except OSError:
                return True
            if any(name.startswith(prefixes) for name in names):
                return True
        return False

    def _require_provider_directory(self) -> Path:
        directory = self._public_root / _PROVIDER_DIRECTORY
        try:
            details = directory.lstat()
        except OSError:
            raise AirportStoreError("airport_provider_invalid") from None
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != self._expected_uid
            or details.st_gid != self._expected_gid
        ):
            raise AirportStoreError("airport_provider_invalid")
        return directory

    def _require_private_root(self) -> Path:
        try:
            details = self._private_root.lstat()
        except OSError:
            raise AirportStoreError("airport_source_invalid") from None
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != self._expected_uid
        ):
            raise AirportStoreError("airport_source_invalid")
        return self._private_root

    def _require_provider_file(self, *, required):
        target = self.path
        try:
            details = target.lstat()
        except FileNotFoundError:
            if required:
                raise AirportStoreError("airport_provider_invalid") from None
            return
        except OSError:
            raise AirportStoreError("airport_provider_invalid") from None
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != self._expected_uid
            or details.st_gid != self._expected_gid
            or stat.S_IMODE(details.st_mode) != _PROVIDER_MODE
        ):
            raise AirportStoreError("airport_provider_invalid")

    def _require_source_file(self):
        try:
            self._source_path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise AirportStoreError("airport_source_invalid") from None
        self._read_source_record()

    def _read_source_record(self):
        try:
            return read_source_file(self._source_path, expected_uid=self._expected_uid)
        except AirportSourceError as error:
            raise AirportStoreError(error.code) from None


def _require_file_shape(details, *, mode, gid, uid, invalid_code):
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != uid
        or stat.S_IMODE(details.st_mode) != mode
        or (gid is not None and details.st_gid != gid)
    ):
        raise AirportStoreError(invalid_code)


def _new_candidate(directory, prefix, mode, uid, gid, payload):
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, dir=str(directory))
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, uid, -1 if gid is None else gid)
            _write_all_and_fsync(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError:
        _remove_quietly(temporary)
        raise AirportStoreError("airport_provider_write_failed") from None
    return temporary


def _write_journal(private_root, journal_path, uid, provider_candidate, source_candidate):
    payload = json.dumps(
        {
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "provider": provider_candidate.name,
            "source": source_candidate.name,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = _new_candidate(
        private_root,
        _JOURNAL_CANDIDATE_PREFIX,
        _JOURNAL_MODE,
        uid,
        None,
        payload,
    )
    try:
        _os_replace(temporary, journal_path)
    except OSError:
        _remove_quietly(temporary)
        raise AirportStoreError("airport_provider_write_failed") from None
    try:
        _fsync_directory(private_root)
    except OSError:
        _remove_quietly(journal_path)
        raise AirportStoreError("airport_provider_write_failed") from None


def _remove_journal(journal_path, private_root):
    try:
        _os_unlink(journal_path)
    except OSError:
        raise AirportStoreError("airport_provider_write_failed") from None
    try:
        _fsync_directory(private_root)
    except OSError:
        raise AirportStoreError("airport_provider_write_failed") from None


def _write_all_and_fsync(descriptor, document):
    view = memoryview(document)
    while view:
        written = _os_write(descriptor, view)
        if written <= 0:
            raise OSError("incomplete provider write")
        view = view[written:]
    _os_fsync(descriptor)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        _os_fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_quietly(path):
    if path is None:
        return
    try:
        _os_unlink(path)
    except OSError:
        pass
