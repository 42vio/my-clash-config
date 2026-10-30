import base64
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

from clash_sub.domain import RuntimeState, UserState


TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}-[A-Za-z0-9._@+-]{1,254}$")
LEGACY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,254}$")


class StateError(ValueError):
    pass


def validate_email(email: str) -> str:
    if not isinstance(email, str) or not EMAIL_RE.fullmatch(email):
        raise StateError("invalid client email")
    return email


def airport_only_token(owner_token, *, schema_version=2):
    """Derive a separate, non-reversible capability for the owner's airport profile."""
    if not isinstance(owner_token, str):
        raise StateError("invalid state")
    legacy = schema_version == 1
    pattern = LEGACY_TOKEN_RE if legacy else TOKEN_RE
    if schema_version not in (1, 2) or not pattern.fullmatch(owner_token):
        raise StateError("invalid state")
    digest = hashlib.sha256(b"clash-sub-airport-only-v1:" + owner_token.encode("ascii")).digest()
    core = base64.urlsafe_b64encode(digest if legacy else digest[:16]).decode("ascii").rstrip("=")
    return core + owner_token[43 if legacy else 22:]


def airport_access_token(state):
    """Use the independent capability, falling back to the existing derived address."""
    return state.airport_token or airport_only_token(state.users[state.owner_client_id].token,schema_version=state.schema_version)


def generate_token(email: str, *, random_bytes=secrets.token_bytes) -> str:
    validate_email(email)
    try:
        core_bytes = random_bytes(16)
        if not isinstance(core_bytes, bytes) or len(core_bytes) != 16:
            raise ValueError
        core = base64.urlsafe_b64encode(core_bytes).decode("ascii").rstrip("=")
        return core + "-" + email
    except StateError:
        raise
    except Exception as error:
        raise StateError("token generation failed") from error


def _generate_legacy_token(existing_codes):
    core = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    while True:
        code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
        if code not in existing_codes:
            return core + "-" + code, code


def load_state(path):
    path = Path(path)
    if path.is_symlink():
        raise StateError("invalid state")
    if not path.exists():
        return None
    try:
        _require_state_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _state_from_payload(payload)
    except StateError:
        raise
    except Exception as error:
        raise StateError("invalid state") from error


def save_state(path, state):
    path = Path(path)
    temporary_path = None
    try:
        payload = _state_to_payload(state)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            os.close(descriptor)
            raise
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
        _require_state_file(path)
    except StateError:
        raise
    except Exception as error:
        raise StateError("state write failed") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def reconcile_state(previous, clients, owner_email):
    clients_by_id = _validate_clients(clients)
    if previous is None:
        owner_ids = [client_id for client_id, client in clients_by_id.items() if client.email == owner_email]
        if len(owner_ids) != 1:
            raise StateError("owner_reinitialization_required")
        owner_client_id = owner_ids[0]
        retained_users = {}
    else:
        _validate_runtime_state(previous)
        owner_client_id = previous.owner_client_id
        if owner_client_id not in clients_by_id:
            raise StateError("owner_reinitialization_required")
        retained_users = dict(previous.users)

    version = previous.schema_version if previous else 2
    existing_codes = {user.readable_code for user in retained_users.values() if user.readable_code}
    users = {
        client_id: UserState(
            client_id=user.client_id,
            email=user.email,
            token=user.token,
            readable_code=user.readable_code,
            active=False,
            current_release=user.current_release,
        )
        for client_id, user in retained_users.items()
    }
    for client_id in sorted(clients_by_id):
        client = clients_by_id[client_id]
        existing = retained_users.get(client_id)
        if existing is None:
            if version == 1:
                token, code = _generate_legacy_token(existing_codes)
                existing_codes.add(code)
            else:
                token, code = generate_token(client.email), None
            users[client_id] = UserState(
                client_id=client_id,
                email=client.email,
                token=token,
                readable_code=code,
                active=client.enabled,
                current_release=None,
            )
        else:
            users[client_id] = UserState(
                client_id=client_id,
                email=existing.email,
                token=existing.token,
                readable_code=existing.readable_code,
                active=client.enabled,
                current_release=existing.current_release,
            )
    return RuntimeState(version, owner_client_id, users,previous.airport_token if previous else None)


def reinitialize_owner(previous, clients, owner_email, owner_client_id):
    """Replace a vanished owner only after an administrator names its new ID.

    This intentionally does not infer identity from a subscription ID or a
    display name.  Existing database IDs retain their mappings; the replaced
    owner gets a fresh token and must be synchronised before it becomes routable.
    """
    _validate_runtime_state(previous)
    if isinstance(owner_client_id, bool) or not isinstance(owner_client_id, int) or owner_client_id < 1:
        raise StateError("owner_reinitialization_required")
    clients_by_id = _validate_clients(clients)
    if previous.owner_client_id in clients_by_id:
        raise StateError("owner_reinitialization_required")
    owner = clients_by_id.get(owner_client_id)
    if owner is None or owner.email != owner_email:
        raise StateError("owner_reinitialization_required")

    retained_users = {
        client_id: user
        for client_id, user in previous.users.items()
        if client_id in clients_by_id and client_id != owner_client_id
    }
    existing_codes = {user.readable_code for user in retained_users.values() if user.readable_code}
    users = {}
    for client_id in sorted(clients_by_id):
        client = clients_by_id[client_id]
        existing = retained_users.get(client_id)
        if existing is None:
            if previous.schema_version == 1:
                token, code = _generate_legacy_token(existing_codes)
                existing_codes.add(code)
            else:
                token, code = generate_token(client.email), None
            users[client_id] = UserState(
                client_id=client_id,
                email=client.email,
                token=token,
                readable_code=code,
                active=client.enabled,
                current_release=None,
            )
        else:
            users[client_id] = UserState(
                client_id=client_id,
                email=existing.email,
                token=existing.token,
                readable_code=existing.readable_code,
                active=client.enabled,
                current_release=existing.current_release,
            )
    return RuntimeState(previous.schema_version, owner_client_id, users,previous.airport_token or (airport_access_token(previous) if previous.schema_version==2 else None))


def rotate_user_token(state, client_id):
    _validate_runtime_state(state)
    user = state.users.get(client_id)
    if user is None:
        raise StateError("unknown client")
    if state.schema_version == 1:
        token, code = _generate_legacy_token({item.readable_code for item in state.users.values()})
    else:
        token, code = generate_token(user.email), None
    users = dict(state.users)
    users[client_id] = UserState(
        client_id=user.client_id,
        email=user.email,
        token=token,
        readable_code=code,
        active=user.active,
        current_release=user.current_release,
    )
    return RuntimeState(state.schema_version, state.owner_client_id, users,state.airport_token or (airport_access_token(state) if client_id==state.owner_client_id and state.schema_version==2 else None))


def prepare_link_update(state: RuntimeState, clients: tuple) -> RuntimeState:
    _validate_runtime_state(state)
    clients_by_id = _validate_clients(clients)
    users = {}
    for client_id, user in state.users.items():
        client = clients_by_id.get(client_id)
        # Deleted legacy records have no route and need no new capability.
        if state.schema_version == 1 and client is None:
            continue
        email = validate_email(client.email if client else user.email)
        if state.schema_version == 1:
            token = generate_token(email)
        elif email != user.email:
            token = user.token[:22] + "-" + email
        else:
            token = user.token
        users[client_id] = UserState(
            user.client_id, email, token, None, user.active, user.current_release
        )
    return RuntimeState(2, state.owner_client_id, users,state.airport_token or (airport_access_token(state) if state.schema_version==2 and users[state.owner_client_id].token!=state.users[state.owner_client_id].token else None))


def _validate_clients(clients):
    clients_by_id = {}
    emails = set()
    sub_ids = set()
    try:
        for client in clients:
            if (
                not isinstance(client.client_id, int)
                or isinstance(client.client_id, bool)
                or not isinstance(client.email, str)
                or not client.email
                or not isinstance(client.sub_id, str)
                or not client.sub_id
                or not isinstance(client.enabled, bool)
                or client.client_id in clients_by_id
                or client.email in emails
                or client.sub_id in sub_ids
            ):
                raise StateError("duplicate client")
            clients_by_id[client.client_id] = client
            emails.add(client.email)
            sub_ids.add(client.sub_id)
    except StateError:
        raise
    except Exception as error:
        raise StateError("invalid client") from error
    return clients_by_id


def _state_to_payload(state):
    _validate_runtime_state(state)
    return {
        "schema_version": state.schema_version,
        "owner_client_id": state.owner_client_id,
        **({"airport_token":state.airport_token} if state.airport_token is not None else {}),
        "users": [
            ({
                "client_id": user.client_id,
                "email": user.email,
                "token": user.token,
                "active": user.active,
                "current_release": user.current_release,
            } | ({"readable_code": user.readable_code} if state.schema_version == 1 else {}))
            for _, user in sorted(state.users.items())
        ],
    }


def _state_from_payload(payload):
    state = _state_from_payload_unowned(payload)
    if state.owner_client_id not in state.users:
        raise StateError("invalid state")
    return state


def _user_from_payload(payload, schema_version):
    expected = {"client_id", "email", "token", "active", "current_release"}
    if schema_version == 1:
        expected.add("readable_code")
    if not isinstance(payload, dict) or set(payload) != expected:
        raise StateError("invalid state")
    client_id = payload["client_id"]
    email = payload["email"]
    token = payload["token"]
    code = payload.get("readable_code")
    active = payload["active"]
    release = payload["current_release"]
    if (
        not isinstance(client_id, int)
        or isinstance(client_id, bool)
        or not isinstance(email, str)
        or not email
        or not isinstance(token, str)
        or (schema_version == 1 and (not LEGACY_TOKEN_RE.fullmatch(token) or not isinstance(code, str) or code != token[44:]))
        or (schema_version == 2 and (not TOKEN_RE.fullmatch(token) or token[23:] != email or code is not None or not EMAIL_RE.fullmatch(email)))
        or not isinstance(active, bool)
        or (release is not None and not isinstance(release, str))
    ):
        raise StateError("invalid state")
    try:
        core = token[:43] if schema_version == 1 else token[:22]
        raw = base64.urlsafe_b64decode(core + ("=" if schema_version == 1 else "=="))
        if len(raw) != (32 if schema_version == 1 else 16) or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != core:
            raise ValueError
    except Exception as error:
        raise StateError("invalid state") from error
    return UserState(client_id, email, token, code, active, release)


def _validate_runtime_state(state):
    if not isinstance(state, RuntimeState):
        raise StateError("invalid state")
    payload = {
        "schema_version": state.schema_version,
        "owner_client_id": state.owner_client_id,
        **({"airport_token":state.airport_token} if state.airport_token is not None else {}),
        "users": [
            ({
                "client_id": user.client_id,
                "email": user.email,
                "token": user.token,
                "active": user.active,
                "current_release": user.current_release,
            } | ({"readable_code": user.readable_code} if state.schema_version == 1 else {}))
            for user in state.users.values()
        ],
    }
    checked = _state_from_payload_unowned(payload)
    if checked.owner_client_id not in checked.users:
        raise StateError("invalid state")


def _state_from_payload_unowned(payload):
    if not isinstance(payload, dict) or not {"schema_version", "owner_client_id", "users"}.issubset(payload) or set(payload)-{"schema_version", "owner_client_id", "users", "airport_token"}:
        raise StateError("invalid state")
    schema_version = payload["schema_version"]
    owner_client_id = payload["owner_client_id"]
    raw_users = payload["users"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in (1, 2)
        or not isinstance(owner_client_id, int)
        or isinstance(owner_client_id, bool)
        or not isinstance(raw_users, list)
    ):
        raise StateError("invalid state")
    users = {}
    codes = set()
    for raw_user in raw_users:
        user = _user_from_payload(raw_user, schema_version)
        if user.client_id in users or (user.readable_code is not None and user.readable_code in codes):
            raise StateError("invalid state")
        users[user.client_id] = user
        if user.readable_code is not None:
            codes.add(user.readable_code)
    airport_token=payload.get('airport_token')
    if airport_token is not None:
        if schema_version!=2 or not isinstance(airport_token,str):raise StateError('invalid state')
        _user_from_payload({'client_id':1,'email':airport_token[23:],'token':airport_token,'active':True,'current_release':None},2)
        if airport_token in {u.token for u in users.values()}:raise StateError('invalid state')
    return RuntimeState(schema_version, owner_client_id, users,airport_token)


def _fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_state_file(path):
    try:
        details = Path(path).stat()
    except OSError:
        raise StateError("invalid state") from None
    expected_uid = 0 if os.geteuid() == 0 else os.geteuid()
    if (
        Path(path).is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or (details.st_mode & 0o777) != 0o600
        or details.st_uid != expected_uid
        or details.st_nlink != 1
    ):
        raise StateError("invalid state")
