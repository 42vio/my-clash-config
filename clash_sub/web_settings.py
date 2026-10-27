"""Private web capabilities, independent of subscription capabilities."""

import hashlib
import json
import os
import secrets
import stat
import tempfile
import re
from pathlib import Path
from clash_sub.state import validate_email


def page_path(token, email):
    """Readable label alongside an independent page capability, without changing tokens."""
    validate_email(email)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('web_settings_invalid')
    return '/p/%s-%s/' % (token, email)


def admin_prefix(data, state=None):
    record = data.get('admin')
    if not isinstance(record, dict):
        return None
    if state is not None:
        user = state.users.get(state.owner_client_id)
        if not user or not user.active or record.get('client_id') != user.client_id or record.get('email') != user.email or record.get('binding') != hashlib.sha256(user.token.encode()).hexdigest():
            return None
    return '/admin/%s-%s' % (record['token'], record['email'])


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
    if 'admin' in data:
        record = data['admin']
        if not isinstance(record, dict) or type(record.get('client_id')) is not int or record['client_id'] <= 0 or not re.fullmatch(r'[A-Za-z0-9_-]{43}',record.get('token','')) or not re.fullmatch(r'[0-9a-f]{64}',record.get('binding','')):
            raise ValueError('web_settings_invalid')
        validate_email(record.get('email'))
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
    owner = state.users[state.owner_client_id]
    binding = hashlib.sha256(owner.token.encode()).hexdigest()
    previous = data.get('admin', {})
    if previous.get('client_id') != owner.client_id or previous.get('email') != owner.email or previous.get('binding') != binding or rotate == 'admin':
        data['admin'] = {'client_id':owner.client_id, 'email':owner.email, 'binding':binding, 'token':secrets.token_urlsafe(32)}
    return data


def rebind_subscription(root, before, after, client_id):
    """Called under the existing operation lock after an authorized rotation."""
    if before.owner_client_id != after.owner_client_id:
        return
    old, new = before.users[client_id], after.users[client_id]
    if old.email != new.email:
        return
    data = read_settings(root)
    old_hash, new_hash = (hashlib.sha256(user.token.encode()).hexdigest() for user in (old,new))
    changed = False
    keys = {str(client_id)} | ({'airport'} if client_id==after.owner_client_id else set())
    for key in keys:
        record = data['pages'].get(key)
        if record and record['binding']==old_hash:
            record['binding']=new_hash
            changed=True
    if client_id==after.owner_client_id and data.get('admin',{}).get('binding')==old_hash:
        data['admin']['binding']=new_hash
        changed=True
    if changed:write_settings(root,data)
