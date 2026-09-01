"""Atomic storage for the single stable airport provider file."""

import grp
import os
import stat
import tempfile
from pathlib import Path

from clash_sub.domain import AIRPORT_FILENAME

MAX_PROVIDER_BYTES = 5 * 1024 * 1024
_PROVIDER_MODE = 0o640
_PROVIDER_DIRECTORY = "provider"


class AirportStoreError(RuntimeError):
    """A redacted, stable airport provider failure."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _os_replace(source, target):
    os.replace(source, target)


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


class AirportStore:
    """Publish the owner-only airport provider through one stable file."""

    def __init__(self, public_root, *, expected_uid=None, expected_public_gid=None):
        self._public_root = Path(public_root)
        self._expected_uid = _expected_uid(expected_uid)
        self._expected_gid = _expected_public_gid(expected_public_gid)

    @property
    def path(self) -> Path:
        return self._public_root / _PROVIDER_DIRECTORY / AIRPORT_FILENAME

    def read(self) -> bytes:
        """Return the exact bytes of the current provider."""
        self._require_provider_file(required=True)
        try:
            payload = self.path.read_bytes()
        except OSError:
            raise AirportStoreError("airport_provider_invalid") from None
        if not payload or len(payload) > MAX_PROVIDER_BYTES:
            raise AirportStoreError("airport_provider_invalid")
        return payload

    def replace(self, document) -> Path:
        """Atomically publish one provider document without inspecting it."""
        if (
            not isinstance(document, bytes)
            or not document
            or len(document) > MAX_PROVIDER_BYTES
        ):
            raise AirportStoreError("airport_provider_invalid")
        directory = self._require_provider_directory()
        self._require_provider_file(required=False)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % AIRPORT_FILENAME, dir=str(directory)
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(descriptor, _PROVIDER_MODE)
                os.fchown(descriptor, self._expected_uid, self._expected_gid)
                _write_all_and_fsync(descriptor, document)
            finally:
                os.close(descriptor)
        except OSError:
            _remove_quietly(temporary)
            raise AirportStoreError("airport_provider_write_failed") from None
        try:
            _os_replace(temporary, self.path)
        except OSError:
            _remove_quietly(temporary)
            raise AirportStoreError("airport_provider_write_failed") from None
        try:
            _fsync_directory(directory)
        except OSError:
            raise AirportStoreError("airport_provider_write_failed") from None
        return self.path

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


def _write_all_and_fsync(descriptor, document):
    view = memoryview(document)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("incomplete provider write")
        view = view[written:]
    os.fsync(descriptor)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass
