import base64
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from clash_sub.domain import RuntimeState, UserState


READABLE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}$")


class StateError(ValueError):
    pass


def generate_token(existing_codes, *, random_bytes=secrets.token_bytes, choose=secrets.choice):
    try:
        core_bytes = random_bytes(32)
        if not isinstance(core_bytes, bytes) or len(core_bytes) != 32:
            raise ValueError
        core = base64.urlsafe_b64encode(core_bytes).decode("ascii").rstrip("=")
        while True:
            code = "".join(choose(READABLE_ALPHABET) for _ in range(6))
            if code not in existing_codes:
                return core + "-" + code, code
    except StateError:
        raise
    except Exception as error:
        raise StateError("token generation failed") from error


def load_state(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
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
            raise StateError("owner reinitialization required")
        owner_client_id = owner_ids[0]
        retained_users = {}
    else:
        _validate_runtime_state(previous)
        owner_client_id = previous.owner_client_id
        if owner_client_id not in clients_by_id:
            raise StateError("owner reinitialization required")
        retained_users = dict(previous.users)

    existing_codes = {user.readable_code for user in retained_users.values()}
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
            token, code = generate_token(existing_codes)
            existing_codes.add(code)
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
                email=client.email,
                token=existing.token,
                readable_code=existing.readable_code,
                active=client.enabled,
                current_release=existing.current_release,
            )
    return RuntimeState(1, owner_client_id, users)


def rotate_user_token(state, client_id):
    _validate_runtime_state(state)
    user = state.users.get(client_id)
    if user is None:
        raise StateError("unknown client")
    token, code = generate_token({item.readable_code for item in state.users.values()})
    users = dict(state.users)
    users[client_id] = UserState(
        client_id=user.client_id,
        email=user.email,
        token=token,
        readable_code=code,
        active=user.active,
        current_release=user.current_release,
    )
    return RuntimeState(state.schema_version, state.owner_client_id, users)


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
        "users": [
            {
                "client_id": user.client_id,
                "email": user.email,
                "token": user.token,
                "readable_code": user.readable_code,
                "active": user.active,
                "current_release": user.current_release,
            }
            for _, user in sorted(state.users.items())
        ],
    }


def _state_from_payload(payload):
    state = _state_from_payload_unowned(payload)
    if state.owner_client_id not in state.users:
        raise StateError("invalid state")
    return state


def _user_from_payload(payload):
    expected = {"client_id", "email", "token", "readable_code", "active", "current_release"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise StateError("invalid state")
    client_id = payload["client_id"]
    email = payload["email"]
    token = payload["token"]
    code = payload["readable_code"]
    active = payload["active"]
    release = payload["current_release"]
    if (
        not isinstance(client_id, int)
        or isinstance(client_id, bool)
        or not isinstance(email, str)
        or not email
        or not isinstance(token, str)
        or not TOKEN_RE.fullmatch(token)
        or not isinstance(code, str)
        or code != token.rsplit("-", 1)[1]
        or not isinstance(active, bool)
        or (release is not None and not isinstance(release, str))
    ):
        raise StateError("invalid state")
    try:
        if len(base64.urlsafe_b64decode(token[:43] + "=")) != 32:
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
        "users": [
            {
                "client_id": user.client_id,
                "email": user.email,
                "token": user.token,
                "readable_code": user.readable_code,
                "active": user.active,
                "current_release": user.current_release,
            }
            for user in state.users.values()
        ],
    }
    checked = _state_from_payload_unowned(payload)
    if checked.owner_client_id not in checked.users:
        raise StateError("invalid state")


def _state_from_payload_unowned(payload):
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "owner_client_id", "users"}:
        raise StateError("invalid state")
    schema_version = payload["schema_version"]
    owner_client_id = payload["owner_client_id"]
    raw_users = payload["users"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
        or not isinstance(owner_client_id, int)
        or isinstance(owner_client_id, bool)
        or not isinstance(raw_users, list)
    ):
        raise StateError("invalid state")
    users = {}
    codes = set()
    for raw_user in raw_users:
        user = _user_from_payload(raw_user)
        if user.client_id in users or user.readable_code in codes:
            raise StateError("invalid state")
        users[user.client_id] = user
        codes.add(user.readable_code)
    return RuntimeState(schema_version, owner_client_id, users)


def _fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
