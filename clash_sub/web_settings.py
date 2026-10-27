"""Private web capabilities, independent of subscription capabilities."""

import hashlib
import json
import os
import secrets
import stat
import tempfile
import re
from pathlib import Path


def read_settings(root):
    path = Path(root) / "web-settings.json"
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "panel_public": True, "enabled": False, "pages": {}}
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600 or details.st_uid != os.geteuid():
        raise ValueError("web_settings_invalid")
    data = json.loads(path.read_text())
    if data.get("version") != 1 or type(data.get("panel_public")) is not bool or type(data.get("enabled")) is not bool or not isinstance(data.get("pages"), dict):
        raise ValueError("web_settings_invalid")
    for key, record in data["pages"].items():
        if not re.fullmatch(r"airport|[1-9][0-9]{0,18}", key) or not isinstance(record, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", record.get("token", "")) or not re.fullmatch(r"[0-9a-f]{64}", record.get("binding", "")):
            raise ValueError("web_settings_invalid")
    if data["enabled"] and not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{128}", data.get("password_hash", "")):
        raise ValueError("web_settings_invalid")
    return data


def write_settings(root, data):
    root = Path(root)
    descriptor, name = tempfile.mkstemp(prefix=".web-settings-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, root / "web-settings.json")
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def password_hash(password, salt=None):
    if not isinstance(password, str) or len(password) < 12 or len(password) > 256:
        raise ValueError("password_length_12_to_256")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def verify_password(password, encoded):
    try:
        salt, _ = encoded.split(":")
        return secrets.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError, AttributeError):
        return False


def provision_pages(data, state, *, rotate=None):
    """Keep stable random page capabilities; bind them to current subscription identity."""
    for client_id, user in state.users.items():
        keys = [str(client_id)]
        if client_id == state.owner_client_id:
            keys.append("airport")
        for key in keys:
            binding = hashlib.sha256(user.token.encode()).hexdigest()
            old = data["pages"].get(key, {})
            if old.get("binding") != binding or rotate == key or not old.get("token"):
                data["pages"][key] = {"token": secrets.token_urlsafe(32), "binding": binding}
    valid = {str(key) for key in state.users} | {"airport"}
    data["pages"] = {key: value for key, value in data["pages"].items() if key in valid}
    return data
