"""Read-only running-version discovery and cached official stable releases."""

import http.client
import json
import logging
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path


PROJECTS = {'xui': 'MHSanaei/3x-ui', 'xray': 'XTLS/Xray-core'}
CACHE_NAME = 'software-updates.json'
CHECK_INTERVAL = 86400
RUNNING_INTERVAL = 60
_VERSION = re.compile(r'v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})\Z')


def version_tuple(value):
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('software_version_invalid')
    return tuple(int(n) for n in match.groups())


def query_release(project, *, connection_factory=http.client.HTTPSConnection):
    """Fixed public GitHub endpoint; no credentials, redirects or downloads."""
    if project not in PROJECTS:
        raise ValueError('software_project_invalid')
    connection = None
    try:
        connection = connection_factory('api.github.com', 443, timeout=5)
        connection.request('GET', '/repos/%s/releases/latest' % PROJECTS[project], headers={
            'Accept': 'application/vnd.github+json', 'User-Agent': 'clash-sub-version-check',
        })
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
            raise ValueError()
        version_tuple(data.get('tag_name'))
        return data['tag_name']
    except Exception:
        raise ValueError('software_release_check_failed') from None
    finally:
        if connection is not None:
            connection.close()


def _command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=1, check=True)
    return result.stdout[:4096].strip()


def read_running_versions(*, proc_root=Path('/proc'), runner=_command):
    """Inspect the service's live executables, including replaced/deleted ones.

    Reading installed files could report a new binary while the old process
    is still running. Execute only the live process's version-only commands.
    """
    versions = dict.fromkeys(PROJECTS)
    try:
        pid = runner(['/usr/bin/systemctl', 'show', 'x-ui', '--property=MainPID', '--value'])
        if not pid.isascii() or not pid.isdigit() or int(pid) <= 0:
            return versions
        executable = proc_root / pid / 'exe'
        if Path(os.readlink(executable).removesuffix(' (deleted)')).name != 'x-ui':
            return versions
        try:
            value = runner([str(executable), '-v']).splitlines()[0]
            version_tuple(value)
            versions['xui'] = value
        except Exception:
            pass
        # Go may spawn the core from any OS thread, not the main thread.
        children = set()
        for task in (proc_root / pid / 'task').iterdir():
            try:
                children.update((task / 'children').read_text().split())
            except OSError:
                continue
        candidates = []
        for child in sorted(children)[:64]:
            if not child.isascii() or not child.isdigit():
                continue
            binary = proc_root / child / 'exe'
            try:
                name = Path(os.readlink(binary).removesuffix(' (deleted)')).name
                if name.startswith('xray-linux-'):
                    candidates.append(binary)
            except OSError:
                continue
        if len(candidates) == 1:
            output = runner([str(candidates[0]), 'version']).splitlines()[0]
            match = re.match(r'Xray\s+(v?\d{1,4}\.\d{1,4}\.\d{1,4})(?:\s|$)', output)
            if match:
                versions['xray'] = match[1]
    except Exception:
        pass
    return versions


def _safe_cache(path):
    details = path.lstat()
    if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
            or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > 65536):
        raise ValueError('software_cache_invalid')


class SoftwareUpdates:
    """One lazy worker per portal; web requests never wait for GitHub.

    Only release checks are persisted, including failed attempts, to avoid
    repeated daily checks after restarts. Local running versions refresh once
    a minute while the admin page is visited. No background scheduler is added.
    """

    def __init__(self, private_root, *, clock=time.time, release_reader=query_release,
                 running_reader=read_running_versions):
        self.path = Path(private_root) / CACHE_NAME
        self.clock, self.release_reader, self.running_reader = clock, release_reader, running_reader
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.current = dict.fromkeys(PROJECTS)
        self.current_checked = None
        self.checked = None
        self.releases = {}
        self.failed = set()
        self._load()

    def _load(self):
        try:
            _safe_cache(self.path)
            data = json.loads(self.path.read_bytes())
            if (not isinstance(data, dict) or set(data) != {'schema_version', 'checked', 'releases', 'failed'}
                    or type(data['schema_version']) is not int or data['schema_version'] != 1
                    or type(data['checked']) is not int or data['checked'] < 0
                    or not isinstance(data['releases'], dict) or set(data['releases']) - PROJECTS.keys()
                    or not isinstance(data['failed'], list) or any(k not in PROJECTS for k in data['failed'])):
                return
            for entry in data['releases'].values():
                if (not isinstance(entry, dict) or set(entry) != {'version', 'updated'}
                        or type(entry['updated']) is not int or not 0 <= entry['updated'] <= data['checked']):
                    return
                version_tuple(entry['version'])
            self.checked, self.releases, self.failed = data['checked'], data['releases'], set(data['failed'])
        except (OSError, ValueError, TypeError):
            return

    def get(self):
        with self.lock:
            now = self.clock()
            local_due = self.current_checked is None or not 0 <= now - self.current_checked < RUNNING_INTERVAL
            remote_due = self.checked is None or not 0 <= now - self.checked < CHECK_INTERVAL
            if (local_due or remote_due) and not self.running:
                self.running = True
                self.thread = threading.Thread(target=self._refresh, args=(remote_due,), daemon=True)
                self.thread.start()
            return {'current': dict(self.current), 'releases': {k: dict(v) for k, v in self.releases.items()},
                    'failed': sorted(self.failed), 'checked': self.checked, 'checking': self.running}

    def _refresh(self, remote_due):
        try:
            try:
                current = self.running_reader()
                clean = {}
                for key in PROJECTS:
                    try:
                        version_tuple(current.get(key))
                        clean[key] = current[key]
                    except (TypeError, ValueError, AttributeError):
                        clean[key] = None
            except Exception:
                clean = dict.fromkeys(PROJECTS)
            with self.lock:
                self.current, self.current_checked = clean, self.clock()
            if remote_due:
                with self.lock:
                    releases = {k: dict(v) for k, v in self.releases.items()}
                failed = set()
                for key in PROJECTS:
                    try:
                        version = self.release_reader(key)
                        version_tuple(version)
                        releases[key] = {'version': version, 'updated': int(self.clock())}
                    except Exception:
                        failed.add(key)
                with self.lock:
                    self.releases, self.failed, self.checked = releases, failed, int(self.clock())
                    payload = {'schema_version': 1, 'checked': self.checked,
                               'releases': releases, 'failed': sorted(failed)}
                try:
                    self._save(payload)
                except (OSError, ValueError):
                    logging.getLogger(__name__).warning('software_update_cache_write_failed')
        finally:
            with self.lock:
                self.running = False

    def _save(self, payload):
        root = self.path.parent
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.geteuid():
            raise ValueError('software_cache_invalid')
        try:
            _safe_cache(self.path)
        except FileNotFoundError:
            pass
        fd, temporary = tempfile.mkstemp(prefix='.software-updates-', dir=root)
        try:
            with os.fdopen(fd, 'w') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
