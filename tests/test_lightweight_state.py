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
    airport_only_token,
    generate_token,
    load_state,
    prepare_link_update,
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
    def test_airport_only_capability_is_distinct_and_has_token_shape(self):
        owner = "A" * 22 + "-42vio"
        airport = airport_only_token(owner)
        self.assertNotEqual(airport, owner)
        self.assertEqual(airport, airport_only_token(owner))
        self.assertEqual(airport[22], "-")
        self.assertTrue(airport.endswith("-42vio"))
        with self.assertRaises(StateError):
            airport_only_token("invalid")

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
        token = generate_token("42vio", random_bytes=lambda size: b"x" * size)

        core, suffix = token[:22], token[23:]
        self.assertEqual(len(core), 22)
        self.assertEqual(len(base64.urlsafe_b64decode(core + "==")), 16)
        self.assertEqual(suffix, "42vio")

    def test_email_suffix_keeps_hyphens_and_case(self):
        token = generate_token("Alice-Dev@Example.com", random_bytes=lambda size: b"x" * size)
        self.assertEqual(token[22:], "-Alice-Dev@Example.com")
        self.assertEqual(airport_only_token(token)[22:], "-Alice-Dev@Example.com")

    def test_airport_token_uses_explicit_version_for_ambiguous_token(self):
        token = "A" * 22 + "-" + "A" * 20 + "-ABCDEF"
        current = airport_only_token(token)
        self.assertEqual(current[22:], "-" + "A" * 20 + "-ABCDEF")
        legacy = airport_only_token(token, schema_version=1)
        self.assertEqual(len(legacy), 50)
        self.assertEqual(legacy[43:], "-ABCDEF")

    def test_invalid_email_characters_and_length_fail_before_generation(self):
        for email in ("", "a b", "a/b", "a%b", "用户", "a\nb", "x" * 255):
            with self.subTest(email=email), self.assertRaisesRegex(StateError, "invalid client email"):
                generate_token(email, random_bytes=lambda size: b"x" * size)

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
        current = prepare_link_update(self.state, (client(7, "old-owner"), client(8, "member")))
        save_state(self.path, current)

        loaded = load_state(self.path)
        self.assertEqual(loaded.schema_version, 2)
        self.assertEqual(loaded.owner_client_id, current.owner_client_id)
        self.assertEqual(set(loaded.users), set(current.users))
        self.assertEqual(loaded.users[7].token, current.users[7].token)
        self.assertEqual(loaded.users[8].token, current.users[8].token)
        self.assertNotIn("readable_code", json.loads(self.path.read_text())["users"][0])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaises(TypeError):
            loaded.users[9] = self.state.users[8]

    def test_v1_state_loads_for_migration_and_v2_round_trips(self):
        payload = {
            "schema_version": 1,
            "owner_client_id": 7,
            "users": [
                {"client_id": user.client_id, "email": user.email, "token": user.token,
                 "readable_code": user.readable_code, "active": user.active,
                 "current_release": user.current_release}
                for user in self.state.users.values()
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(self.path, 0o600)
        loaded = load_state(self.path)
        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.users[7].token, self.state.users[7].token)
        migrated = prepare_link_update(loaded, (client(7, "old-owner"), client(8, "member")))
        save_state(self.path, migrated)
        self.assertEqual(load_state(self.path).schema_version, 2)

    def test_prepare_link_update_reissues_v1_and_renames_v2_without_persisting(self):
        migrated = prepare_link_update(self.state, (client(7, "old-owner"), client(8, "member")))
        self.assertEqual(migrated.schema_version, 2)
        self.assertEqual(migrated.users[7].token[22:], "-old-owner")
        self.assertNotEqual(migrated.users[7].token, self.state.users[7].token)
        renamed = prepare_link_update(migrated, (client(7, "New-Owner"), client(8, "member")))
        self.assertEqual(renamed.users[7].token[:22], migrated.users[7].token[:22])
        self.assertEqual(renamed.users[7].token[22:], "-New-Owner")
        self.assertEqual(migrated.users[7].email, "old-owner")

    def test_migration_drops_deleted_legacy_client_with_invalid_new_email(self):
        member = self.state.users[8]
        previous = RuntimeState(1, 7, {
            7: self.state.users[7],
            8: UserState(8, "old member", member.token, member.readable_code, True, None),
        })
        clients = (client(7, "old-owner"),)
        reconciled = reconcile_state(previous, clients, "old-owner")
        candidate = prepare_link_update(reconciled, clients)
        self.assertEqual(candidate.schema_version, 2)
        self.assertEqual(set(candidate.users), {7})
        self.assertIn(8, previous.users)
        save_state(self.path, candidate)
        self.assertEqual(load_state(self.path), candidate)

    def test_v2_rejects_mismatched_token_email(self):
        migrated = prepare_link_update(self.state, (client(7, "old-owner"), client(8, "member")))
        owner = migrated.users[7]
        broken = RuntimeState(2, 7, {7: UserState(7, "different", owner.token, None, True, None), 8: migrated.users[8]})
        with self.assertRaisesRegex(StateError, "invalid state"):
            save_state(self.path, broken)

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
        self.assertEqual(updated.users[7].email, "old-owner")

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

    def test_v2_owner_reinitialization_preserves_schema_and_published_member_email(self):
        previous = prepare_link_update(self.state, (client(7, "old-owner"), client(8, "member")))
        result = reinitialize_owner(previous, (client(9, "old-owner"), client(8, "renamed")), "old-owner", 9)
        self.assertEqual(result.schema_version, 2)
        self.assertEqual(result.users[8].email, "member")
        save_state(self.path, result)
        self.assertEqual(load_state(self.path).owner_client_id, 9)

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
