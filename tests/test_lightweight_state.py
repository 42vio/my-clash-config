import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clash_sub.domain import RuntimeState, UserState, XuiClient
from clash_sub.state import (
    StateError,
    generate_token,
    load_state,
    reconcile_state,
    reinitialize_owner,
    rotate_user_token,
    save_state,
)


def client(client_id, email, sub_id=None, enabled=True):
    return XuiClient(
        client_id=client_id,
        email=email,
        sub_id=sub_id or "sub-%s" % client_id,
        enabled=enabled,
        upload=0,
        download=0,
        total=0,
        expiry_ms=0,
    )


class LightweightStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "state.json"
        owner_token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=") + "-ABCDEF"
        member_token = base64.urlsafe_b64encode(b"y" * 32).decode().rstrip("=") + "-GHJKMN"
        self.state = RuntimeState(
            1,
            7,
            {
                7: UserState(7, "old-owner", owner_token, "ABCDEF", True, None),
                8: UserState(8, "member", member_token, "GHJKMN", True, None),
            },
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_token_has_random_core_and_readable_suffix(self):
        token, code = generate_token(
            set(), random_bytes=lambda size: b"x" * size, choose=lambda alphabet: "K"
        )

        core, suffix = token.rsplit("-", 1)
        self.assertEqual(len(base64.urlsafe_b64decode(core + "=")), 32)
        self.assertEqual(suffix, "KKKKKK")
        self.assertEqual(code, suffix)

    def test_token_retries_readable_code_collisions(self):
        choices = iter("AAAAAABBBBBB")

        token, code = generate_token(
            {"AAAAAA"},
            random_bytes=lambda size: b"x" * size,
            choose=lambda alphabet: next(choices),
        )

        self.assertTrue(token.endswith("-BBBBBB"))
        self.assertEqual(code, "BBBBBB")

    def test_load_rejects_a_token_that_is_not_exactly_valid(self):
        token = base64.urlsafe_b64encode(b"z" * 32).decode().rstrip("=")
        payload = {
            "schema_version": 1,
            "owner_client_id": 7,
            "users": [
                {
                    "client_id": 7,
                    "email": "owner",
                    "token": token + "-ABCDEF-extra",
                    "readable_code": "ABCDEF",
                    "active": True,
                    "current_release": None,
                }
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(self.path, 0o600)

        with self.assertRaisesRegex(StateError, "invalid state") as error:
            load_state(self.path)

        self.assertEqual(str(error.exception), "invalid state")

    def test_save_round_trip_uses_mode_0600_and_immutable_users(self):
        save_state(self.path, self.state)

        loaded = load_state(self.path)
        self.assertEqual(loaded.schema_version, self.state.schema_version)
        self.assertEqual(loaded.owner_client_id, self.state.owner_client_id)
        self.assertEqual(set(loaded.users), set(self.state.users))
        self.assertTrue(loaded.users[7].token == self.state.users[7].token)
        self.assertTrue(loaded.users[8].token == self.state.users[8].token)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaises(TypeError):
            loaded.users[9] = self.state.users[8]

    def test_load_rejects_a_hard_linked_state_file(self):
        save_state(self.path, self.state)
        os.link(self.path, self.path.with_name("state-linked.json"))

        with self.assertRaisesRegex(StateError, "invalid state"):
            load_state(self.path)

    def test_load_rejects_a_broken_state_symlink_instead_of_treating_it_as_missing(self):
        self.path.symlink_to(self.path.with_name("missing-state.json"))

        with self.assertRaisesRegex(StateError, "invalid state"):
            load_state(self.path)

    def test_failed_atomic_replacement_preserves_existing_state(self):
        save_state(self.path, self.state)
        updated = rotate_user_token(self.state, 8)

        with patch("clash_sub.state.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(StateError, "state write failed"):
                save_state(self.path, updated)

        self.assertTrue(load_state(self.path) == self.state)

    def test_first_run_matches_owner_email_and_assigns_each_client_identity(self):
        updated = reconcile_state(None, [client(7, "owner"), client(8, "member")], "owner")

        self.assertEqual(updated.owner_client_id, 7)
        self.assertEqual(set(updated.users), {7, 8})
        self.assertTrue(updated.users[7].active)
        self.assertTrue(updated.users[8].active)

    def test_email_rename_does_not_rotate_identity(self):
        updated = reconcile_state(self.state, [client(7, "renamed"), client(8, "member")], "old-owner")

        self.assertEqual(updated.owner_client_id, 7)
        self.assertTrue(updated.users[7].token == self.state.users[7].token)
        self.assertEqual(updated.users[7].email, "renamed")

    def test_disable_then_reenable_retains_token(self):
        disabled = reconcile_state(
            self.state, [client(7, "old-owner"), client(8, "member", enabled=False)], "old-owner"
        )
        reenabled = reconcile_state(
            disabled, [client(7, "old-owner"), client(8, "member", enabled=True)], "old-owner"
        )

        self.assertFalse(disabled.users[8].active)
        self.assertTrue(reenabled.users[8].active)
        self.assertTrue(reenabled.users[8].token == self.state.users[8].token)

    def test_recreated_client_id_receives_new_token(self):
        old_token = self.state.users[8].token
        updated = reconcile_state(
            self.state,
            [client(7, "old-owner"), client(9, "member", sub_id="sub-8")],
            "old-owner",
        )

        self.assertFalse(updated.users[8].active)
        self.assertTrue(updated.users[9].active)
        self.assertTrue(updated.users[9].token != old_token)

    def test_duplicate_email_or_subscription_id_fails_closed(self):
        with self.assertRaisesRegex(StateError, "duplicate client"):
            reconcile_state(None, [client(7, "owner"), client(8, "owner")], "owner")
        with self.assertRaisesRegex(StateError, "duplicate client"):
            reconcile_state(None, [client(7, "owner", "shared"), client(8, "member", "shared")], "owner")

    def test_missing_persisted_owner_requires_explicit_reinitialization(self):
        with self.assertRaisesRegex(StateError, "owner_reinitialization_required"):
            reconcile_state(self.state, [client(8, "member")], "old-owner")

    def test_explicit_owner_reinitialization_revokes_missing_owner_and_preserves_unchanged_ids(self):
        replacement_owner = client(9, "old-owner", "owner-recreated")
        member = client(8, "member")

        updated = reinitialize_owner(
            self.state,
            [replacement_owner, member],
            "old-owner",
            9,
        )

        self.assertEqual(updated.owner_client_id, 9)
        self.assertNotIn(7, updated.users)
        self.assertEqual(updated.users[8], self.state.users[8])
        self.assertTrue(updated.users[9].active)
        self.assertIsNone(updated.users[9].current_release)
        self.assertNotEqual(updated.users[9].token, self.state.users[7].token)

    def test_explicit_owner_reinitialization_requires_the_exact_configured_owner_email(self):
        with self.assertRaisesRegex(StateError, "owner_reinitialization_required"):
            reinitialize_owner(
                self.state,
                [client(9, "other-owner"), client(8, "member")],
                "old-owner",
                9,
            )

    def test_rotate_user_token_changes_only_requested_identity(self):
        rotated = rotate_user_token(self.state, 8)

        self.assertTrue(rotated.users[8].token != self.state.users[8].token)
        self.assertTrue(rotated.users[7].token == self.state.users[7].token)
        self.assertEqual(rotated.users[7].email, self.state.users[7].email)
        self.assertEqual(rotated.users[8].client_id, 8)
        with self.assertRaises(TypeError):
            rotated.users[9] = self.state.users[8]


if __name__ == "__main__":
    unittest.main()
