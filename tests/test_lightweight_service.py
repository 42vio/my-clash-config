import base64
import contextlib
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from clash_sub.airport_store import AirportStore, AirportStoreError
from clash_sub.domain import (
    PROFILE_FILENAMES,
    PreparedRelease,
    RuntimeState,
    ServiceConfig,
    UserState,
    XuiClient,
    XuiSnapshot,
)
from clash_sub.state import StateError

try:
    from clash_sub.service import ClashSubService, ServiceError, _OperationLock
except ImportError:
    ClashSubService = None
    ServiceError = RuntimeError


PROVIDER_DOCUMENT = (
    b"proxies:\n"
    b"- {name: Airport candidate, type: ss, server: amy.example.test, port: 443}\n"
)


class OperationLockTests(unittest.TestCase):
    def test_default_lock_requires_root_only_real_directory_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "private"; root.mkdir(); os.chmod(root, 0o700)
            with _OperationLock(root / "operation.lock"):
                self.assertEqual(stat.S_IMODE((root / "operation.lock").stat().st_mode), 0o600)
            os.chmod(root, 0o755)
            with self.assertRaises(ServiceError) as caught: _OperationLock(root / "operation.lock").__enter__()
            self.assertEqual(caught.exception.code, "operation_lock_invalid")

    def test_default_lock_rejects_a_symlink_component_without_creating_outside_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory).resolve(); outside = real / "outside"; outside.mkdir()
            linked = real / "linked"; linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ServiceError) as caught: _OperationLock(linked / "operation.lock").__enter__()
            self.assertEqual(caught.exception.code, "operation_lock_invalid")
            self.assertFalse((outside / "operation.lock").exists())

    def test_busy_lock_sanitizes_close_failure_and_clears_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "private"; root.mkdir(); os.chmod(root, 0o700)
            lock = _OperationLock(root / "operation.lock")
            with patch("clash_sub.service.fcntl.flock", side_effect=OSError("busy secret")), patch("clash_sub.service.os.close", side_effect=OSError("close secret")):
                with self.assertRaises(ServiceError) as caught: lock.__enter__()
            self.assertEqual(caught.exception.code, "operation_busy")
            self.assertIsNone(lock.descriptor)
            self.assertNotIn("secret", str(caught.exception))


def token(byte, code):
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=") + "-" + code


def client(client_id, email, enabled=True, upload=1):
    return XuiClient(client_id, email, "sub-%s" % client_id, enabled, upload, 2, 3, 4000)


class FakeStore:
    def __init__(self):
        self.releases = {}
        self.prepared = []
        self.pruned = []
        self.discarded = []
        self.marked = []
        self.counter = 0

    def prepare(self, client_id, bundle, input_hashes):
        current = self.releases.get(client_id, [])
        if current and current[-1].public_paths.keys() == bundle.keys() and getattr(current[-1], "bundle", None) == bundle:
            return None
        self.counter += 1
        release_id = "2026-08-23T12-00-%02dZ-000000%02x" % (self.counter, self.counter)
        release = PreparedRelease(
            release_id,
            {name: Path("/releases/%s/%s/%s" % (client_id, release_id, PROFILE_FILENAMES[name])) for name in bundle},
            Path("/private/manifest"),
        )
        object.__setattr__(release, "bundle", dict(bundle))
        object.__setattr__(release, "inputs", dict(input_hashes))
        self.releases.setdefault(client_id, []).append(release)
        self.prepared.append((client_id, release, dict(input_hashes)))
        return release

    def verify_release(self, client_id, release_id):
        for release in self.releases.get(client_id, ()):
            if release.release_id == release_id:
                return release
        raise ValueError("missing release")

    def history(self, client_id):
        return tuple(reversed(self.releases.get(client_id, ())))

    def mark_current(self, client_id, release_id):
        self.marked.append((client_id, release_id))

    def current_artifact(self, client_id, release_id):
        self.verify_release(client_id, release_id)
        return Path("/private/current/%s" % client_id), (release_id + "\n").encode(), 0o600

    def discard_unreferenced(self, client_id, release_id):
        self.discarded.append((client_id, release_id))
        self.releases[client_id] = [item for item in self.releases[client_id] if item.release_id != release_id]

    def prune(self, client_id, keep=5):
        self.pruned.append(client_id)


class FakeActivator:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, config, state, routes, runner, extra_replacements=()):
        self.calls.append((state, routes, tuple(extra_replacements)))
        if self.fail:
            raise RuntimeError("private nginx detail")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.owner = client(7, "owner@example.test", upload=11)
        self.member = client(8, "member@example.test", upload=22)
        self.clients = [self.owner, self.member]
        self.state = None
        self.store = FakeStore()
        self.activator = FakeActivator()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve() / "service"
        private_root = root / "private"
        private_root.mkdir(parents=True)
        os.chmod(private_root, 0o700)
        self.clock_value = 1750000000.0
        self.config = ServiceConfig("owner@example.test", "sub.example.test:8443", "example.com:443", root / "xui.db", private_root, root / "public", root / "routes.conf", Path("/bin/mihomo"), Path("/bin/nginx"), Path("/bin/systemctl"), root / "templates")
        self.airport_file = root / "public" / "provider" / "AmyTelecom-Provider.yaml"
        self.airport_file.parent.mkdir(parents=True)
        self.airport_file.write_bytes(PROVIDER_DOCUMENT)
        os.chmod(self.airport_file, 0o640)
        self.replaced_documents = []
        self.download_document = PROVIDER_DOCUMENT
        self.airport_store = MagicMock(spec=AirportStore)
        self.airport_store.path = self.airport_file
        self.airport_store.read.return_value = PROVIDER_DOCUMENT
        self.airport_store.replace.side_effect = self._replace_provider
        self.generator_calls = []
        self.mihomo_calls = []
        self.fetch_calls = []
        self.download_calls = []
        self.validation_calls = []
        self.member_render_text = "member compat"
        self.fail_client = None
        self.fail_owner_render = False
        self.service = ClashSubService(
            self.config,
            read_snapshot=lambda _: XuiSnapshot(tuple(self.clients), "127.0.0.1", 2096, "/clash/"),
            load_state=lambda _: self.state,
            reconcile_state=self._reconcile,
            rotate_user_token=self._rotate,
            fetch_xui_proxies=self._fetch,
            download_airport_document=self._download,
            airport_store=self.airport_store,
            render_user_bundle=self._render,
            validate_clash=self._validate,
            mihomo_validator=type("Validator", (), {"validate": lambda _, path: self.mihomo_calls.append(path)})(),
            release_store=self.store,
            render_routes=self._routes,
            activate_runtime=self.activator,
            runner=lambda *args, **kwargs: None,
            state_sink=lambda state: setattr(self, "state", state),
            lock_factory=lambda _: contextlib.nullcontext(),
            clock=lambda: self.clock_value,
        )

    def bootstrap(self):
        """One successful sync with the stable provider already in place."""
        return self.service.sync_all()

    def _replace_provider(self, document, validator):
        # Mirrors the real store: the validator sees a staged candidate and
        # the stable file only changes after validation succeeds.
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "AmyTelecom-Provider.yaml"
            candidate.write_bytes(document)
            validator(candidate)
        self.airport_file.write_bytes(document)
        self.replaced_documents.append(document)
        return self.airport_file

    def current_owner_release(self):
        return self.state.users[7].current_release

    def _reconcile(self, previous, clients, owner_email):
        users = {} if previous is None else dict(previous.users)
        owner_id = 7 if previous is None else previous.owner_client_id
        for current in clients:
            prior = users.get(current.client_id)
            users[current.client_id] = UserState(current.client_id, current.email, prior.token if prior else token(bytes([current.client_id]), "ABCDEF" if current.client_id == 7 else "GHJKMN"), prior.readable_code if prior else ("ABCDEF" if current.client_id == 7 else "GHJKMN"), current.enabled, prior.current_release if prior else None)
        for current_id, prior in list(users.items()):
            if current_id not in {item.client_id for item in clients}:
                users[current_id] = replace(prior, active=False)
        return RuntimeState(1, owner_id, users)

    def _rotate(self, state, client_id):
        user = state.users[client_id]
        users = dict(state.users)
        users[client_id] = replace(user, token=token(b"r", "PQRSTU"), readable_code="PQRSTU")
        return RuntimeState(1, state.owner_client_id, users)

    def _fetch(self, url, max_bytes):
        self.fetch_calls.append(url)
        if self.fail_client and url.endswith("/%s" % self.fail_client):
            raise RuntimeError("source URL should never escape")
        return [{"name": "Node " + url.rsplit("/", 1)[-1], "server": "panel.example.test", "port": 10443}]

    def _download(self, url, max_bytes):
        self.download_calls.append(url)
        return self.download_document

    def _validate(self, text, forbidden, allowed_provider_url=None):
        self.validation_calls.append((forbidden, allowed_provider_url))

    def _render(self, is_owner, xui, airport, template_root):
        self.generator_calls.append((is_owner, airport))
        if is_owner and self.fail_owner_render:
            raise RuntimeError("owner private failure")
        if is_owner:
            return {
                variant: (
                    "proxy-providers:\n  AmyTelecom:\n    type: http\n    url: %s\n"
                    "    path: ./proxy_providers/AmyTelecom-Provider.yaml\n    interval: 604800\n"
                    "proxies:\n- name: Owner %s\n" % (airport.url, variant)
                )
                for variant in ("compat", "balance")
            }
        return {"compat": self.member_render_text}

    def _routes(self, config, state, clients):
        if any(user.active and user.current_release is None for user in state.users.values()):
            raise ValueError("missing release route")
        return "routes:%s" % sorted(state.users)

    def test_first_sync_activates_owner_and_member_with_exact_profiles(self):
        self.assertIsNotNone(ClashSubService)
        result = self.bootstrap()
        self.assertEqual({item["client_id"] for item in result["updated"]}, {7, 8})
        self.assertEqual(tuple(self.store.prepared[0][1].public_paths), ("compat", "balance"))
        self.assertEqual(tuple(self.store.prepared[1][1].public_paths), ("compat",))
        self.assertTrue(all("token" not in repr(item) for item in result["updated"]))
        self.assertEqual(len(self.activator.calls[0][2]), 2)

    def test_release_input_hashes_contain_only_the_xui_source(self):
        self.bootstrap()
        for _client_id, _release, inputs in self.store.prepared:
            self.assertEqual(set(inputs), {"xui"})

    def test_sync_reads_and_validates_the_stable_provider_before_any_user(self):
        self.airport_store.read.side_effect = AirportStoreError("airport_provider_invalid")

        with self.assertRaisesRegex(ServiceError, "airport_provider_required") as caught:
            self.service.sync_all()

        self.assertNotIn("airport.example", str(caught.exception))
        self.assertEqual(self.generator_calls, [])
        self.assertEqual(self.store.prepared, [])
        self.assertEqual(self.activator.calls, [])

    def test_sync_rejects_an_invalid_stored_provider_document(self):
        self.airport_store.read.return_value = b"proxies: []\n"

        with self.assertRaisesRegex(ServiceError, "airport_provider_required"):
            self.service.sync_all()

    def test_member_render_receives_no_provider(self):
        self.bootstrap()

        member_calls = [call for call in self.generator_calls if not call[0]]
        owner_calls = [call for call in self.generator_calls if call[0]]
        self.assertTrue(member_calls)
        self.assertTrue(owner_calls)
        self.assertTrue(all(call[1] is None for call in member_calls))
        self.assertTrue(all(call[1] is not None for call in owner_calls))

    def test_owner_render_receives_the_stable_provider_url(self):
        self.bootstrap()

        provider_url = "https://sub.example.test:8443/s/%s/AmyTelecom-Provider.yaml" % token(bytes([7]), "ABCDEF")
        self.assertEqual(
            {call[1].url for call in self.generator_calls if call[0]},
            {provider_url},
        )

    def test_owner_validation_receives_the_stable_provider_url_and_full_forbidden_set(self):
        self.bootstrap()

        owner_calls = [call for call in self.validation_calls if call[1] is not None]
        member_calls = [call for call in self.validation_calls if call[1] is None]
        self.assertTrue(owner_calls)
        self.assertTrue(member_calls)
        provider_url = "https://sub.example.test:8443/s/%s/AmyTelecom-Provider.yaml" % token(bytes([7]), "ABCDEF")
        self.assertEqual({call[1] for call in owner_calls}, {provider_url})
        seen = set(owner_calls[0][0])
        self.assertIn(token(bytes([8]), "GHJKMN"), seen)
        self.assertIn("sub-7", seen)

    def test_owner_mihomo_validation_swaps_in_the_stable_local_file_provider(self):
        snapshots = []

        def snapshot(path):
            snapshots.append(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

        self.service._mihomo.validate = snapshot
        self.bootstrap()

        owner_snapshots = [document for document in snapshots if "proxy-providers" in document]
        self.assertTrue(owner_snapshots)
        for document in owner_snapshots:
            provider = document["proxy-providers"]["AmyTelecom"]
            self.assertEqual(provider["type"], "file")
            self.assertEqual(provider["path"], str(self.airport_file))

    def test_sync_all_reuses_the_release_artifact_without_touching_the_upstream(self):
        self.bootstrap()
        self.download_calls.clear()

        self.service.sync_all()

        self.assertEqual(self.download_calls, [])

    def test_provider_byte_change_cannot_create_a_main_release(self):
        self.bootstrap()
        first_release = self.current_owner_release()
        self.airport_store.read.return_value = PROVIDER_DOCUMENT.replace(b"amy.example.test", b"amy-2.example.test")

        result = self.service.sync_all()

        self.assertFalse(result["errors"])
        self.assertEqual(self.current_owner_release(), first_release)
        self.assertEqual(self.state.users[8].current_release, self.store.history(8)[0].release_id)

    def test_update_airport_does_not_read_xui_prepare_release_or_activate(self):
        self.service._read_snapshot = MagicMock()
        self.service._recover_runtime = MagicMock()

        result = self.service.update_airport("https://airport.example/sub")

        self.assertEqual(result, {"updated": True})
        self.airport_store.replace.assert_called_once()
        self.service._read_snapshot.assert_not_called()
        self.service._recover_runtime.assert_not_called()
        self.assertEqual(self.store.prepared, [])
        self.assertEqual(self.activator.calls, [])
        self.assertEqual(self.fetch_calls, [])

    def test_update_airport_only_replaces_the_provider_file(self):
        self.bootstrap()
        before_state = self.state
        before_activations = len(self.activator.calls)
        new_document = PROVIDER_DOCUMENT.replace(b"amy.example.test", b"amy-2.example.test")
        self.download_document = new_document

        result = self.service.update_airport("https://airport.example/new")

        self.assertEqual(result, {"updated": True})
        self.assertEqual(self.replaced_documents, [new_document])
        self.assertEqual(self.state, before_state)
        self.assertEqual(len(self.activator.calls), before_activations)
        self.assertEqual(len(self.store.prepared), 2)

    def test_update_airport_validates_the_candidate_with_mihomo_as_a_file_provider(self):
        def fail_first(candidate):
            raise RuntimeError("mihomo rejected the provider")

        original_validate = self.service._mihomo.validate
        seen = []

        def capture(path):
            document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            provider_path = Path(document["proxy-providers"]["AmyTelecom"]["path"])
            seen.append((document, provider_path.read_bytes()))

        self.service._mihomo.validate = capture

        self.service.update_airport("https://airport.example/sub")

        self.service._mihomo.validate = original_validate
        self.assertEqual(len(seen), 1)
        document, candidate_bytes = seen[0]
        provider = document["proxy-providers"]["AmyTelecom"]
        self.assertEqual(provider["type"], "file")
        self.assertTrue(provider["path"].endswith("AmyTelecom-Provider.yaml"))
        self.assertEqual(candidate_bytes, PROVIDER_DOCUMENT)
        self.assertEqual(document["rules"], ["MATCH,%s" % document["proxy-groups"][0]["name"]])
        self.assertEqual(document["proxy-groups"][0]["use"], ["AmyTelecom"])

    def test_update_airport_failure_keeps_the_previous_provider_and_emits_one_code(self):
        self.bootstrap()
        secret = "https://airport.example/temporary-secret"
        self.airport_store.replace.side_effect = lambda document, validator: (_ for _ in ()).throw(AirportStoreError("airport_provider_write_failed"))

        with self.assertRaises(ServiceError) as caught:
            self.service.update_airport(secret)

        self.assertEqual(caught.exception.code, "airport_update_failed")
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.airport_file.read_bytes(), PROVIDER_DOCUMENT)
        self.assertEqual(self.service.status()["last_errors"], ("airport_update_failed",))

    def test_update_airport_rejects_a_non_provider_document(self):
        self.download_document = b"proxies: []\n"

        with self.assertRaisesRegex(ServiceError, "airport_update_failed"):
            self.service.update_airport("https://airport.example/empty")

        self.assertEqual(self.airport_file.read_bytes(), PROVIDER_DOCUMENT)
        self.assertEqual(self.replaced_documents, [])

    def test_rotate_owner_rebuilds_the_release_with_the_new_token_url(self):
        self.bootstrap()
        self.download_calls.clear()
        first_release = self.state.users[7].current_release
        old_provider_urls = {call[1].url for call in self.generator_calls if call[0]}

        rotated = self.service.rotate_link(7)

        self.assertEqual(self.download_calls, [])
        self.assertNotEqual(self.state.users[7].current_release, first_release)
        new_provider_urls = {call[1].url for call in self.generator_calls if call[0]} - old_provider_urls
        self.assertEqual(len(new_provider_urls), 1)
        self.assertIn(token(b"r", "PQRSTU"), next(iter(new_provider_urls)))
        self.assertIn("AmyTelecom-Provider.yaml", next(iter(new_provider_urls)))
        self.assertEqual(len(rotated["urls"]), 2)

    def test_member_rotation_changes_routes_without_touching_provider_bytes(self):
        self.bootstrap()
        replaced_before = list(self.replaced_documents)

        rotated = self.service.rotate_link(8)

        self.airport_store.replace.assert_not_called()
        self.assertEqual(self.replaced_documents, replaced_before)
        self.assertEqual(self.airport_file.read_bytes(), PROVIDER_DOCUMENT)
        self.assertEqual(len(rotated["urls"]), 1)

    def test_rotation_activation_failure_returns_the_dedicated_code_and_keeps_the_old_link(self):
        self.bootstrap()
        old_token = self.state.users[7].token
        old_release = self.state.users[7].current_release
        releases_before = tuple(self.store.history(7))
        self.activator.fail = True

        with self.assertRaises(ServiceError) as caught:
            self.service.rotate_link(7)

        self.assertEqual(caught.exception.code, "rotation_activation_failed")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(old_token, str(caught.exception))
        self.assertEqual(self.state.users[7].token, old_token)
        self.assertEqual(self.state.users[7].current_release, old_release)
        self.assertEqual(self.store.history(7), releases_before)
        self.assertTrue(
            any(item[0] == 7 for item in self.store.discarded)
        )

    def test_member_rotation_activation_failure_returns_the_dedicated_code(self):
        self.bootstrap()
        old_token = self.state.users[8].token
        self.activator.fail = True

        with self.assertRaises(ServiceError) as caught:
            self.service.rotate_link(8)

        self.assertEqual(caught.exception.code, "rotation_activation_failed")
        self.assertEqual(self.state.users[8].token, old_token)

    def test_rollback_never_changes_the_provider(self):
        self.bootstrap()
        release = self.state.users[7].current_release
        self.service._releases.mark_current = lambda *_: None

        self.service.rollback(7, release)

        self.airport_store.replace.assert_not_called()
        self.assertEqual(self.airport_file.read_bytes(), PROVIDER_DOCUMENT)
        self.assertEqual(self.state.users[7].current_release, release)

    def test_sync_activation_failure_is_all_or_nothing(self):
        self.activator.fail = True
        with self.assertRaises(ServiceError):
            self.service.sync_all()
        self.assertEqual(self.store.history(7), ())
        self.assertEqual(self.store.history(8), ())
        self.assertEqual(len(self.store.discarded), 2)
        self.assertIsNone(self.state)

    def test_sync_retries_rendered_output_after_failed_activation_and_discards_candidates(self):
        self.activator.fail = True
        with self.assertRaises(ServiceError):
            self.service.sync_all()
        self.activator.fail = False
        self.bootstrap()
        self.assertTrue(self.mihomo_calls)

    def test_mihomo_failure_discards_owned_sync_candidate_before_route_activation(self):
        self.bootstrap()
        self.service._mihomo.validate = lambda _: (_ for _ in ()).throw(RuntimeError("private"))

        def changed_render(owner, xui, airport, root):
            if not owner:
                return {"compat": "member changed"}
            return {
                variant: ("proxy-providers:\n  AmyTelecom:\n    type: http\n    url: %s\nproxies:\n- name: Owner %s changed\n" % (airport.url, variant))
                for variant in ("compat", "balance")
            }

        self.service._render = changed_render
        result = self.service.sync_all()
        self.assertEqual(len(self.store.discarded), 2)
        self.assertEqual(len(self.store.history(7)), 1)
        self.assertEqual(len(self.store.history(8)), 1)
        self.assertEqual(len(self.activator.calls), 2)

    def test_traffic_rejects_new_clients_pending_manual_sync_without_mutation(self):
        self.bootstrap()
        self.clients.append(client(9, "new@example.test"))
        before = self.state
        calls = len(self.activator.calls)

        with self.assertRaises(ServiceError) as caught:
            self.service.traffic_update()

        self.assertEqual(caught.exception.code, "traffic_activation_failed")
        self.assertNotIn(9, self.state.users)
        self.assertEqual(self.state, before)
        self.assertEqual(len(self.activator.calls), calls)

    def test_traffic_rejects_identity_and_enabled_state_drift_pending_manual_sync(self):
        self.bootstrap()
        baseline_clients = tuple(self.clients)
        baseline_state = self.state
        inactive_users = dict(baseline_state.users)
        inactive_users[8] = replace(inactive_users[8], active=False)
        inactive_state = RuntimeState(1, baseline_state.owner_client_id, inactive_users)
        cases = (
            ("renamed", (self.owner, replace(self.member, email="renamed@example.test")), baseline_state),
            ("disabled", (self.owner, replace(self.member, enabled=False)), baseline_state),
            ("reenabled", baseline_clients, inactive_state),
            ("deleted active", (self.owner,), baseline_state),
        )

        for name, clients, state in cases:
            with self.subTest(name=name):
                self.clients = list(clients)
                self.state = state
                calls = len(self.activator.calls)

                with self.assertRaises(ServiceError) as caught:
                    self.service.traffic_update()

                self.assertEqual(caught.exception.code, "traffic_activation_failed")
                self.assertEqual(self.state, state)
                self.assertEqual(len(self.activator.calls), calls)

    def test_traffic_allows_a_missing_already_inactive_retained_user(self):
        self.bootstrap()
        users = dict(self.state.users)
        users[8] = replace(users[8], active=False)
        self.state = RuntimeState(1, 7, users)
        self.clients = [self.owner]

        result = self.service.traffic_update()

        self.assertEqual(result, {"updated": (), "errors": ()})
        self.assertFalse(self.state.users[8].active)

    def test_links_and_rotation_reject_release_less_or_inactive_users(self):
        self.bootstrap()
        users = dict(self.state.users)
        users[9] = UserState(9, "pending@example.test", token(b"p", "PQRSTU"), "PQRSTU", True, None)
        self.state = RuntimeState(1, 7, users)
        self.assertNotIn(9, [item["client_id"] for item in self.service.links()])
        with self.assertRaises(ServiceError) as caught:
            self.service.rotate_link(9)
        self.assertEqual(caught.exception.code, "rotation_not_allowed")

    def test_status_and_history_are_deterministically_sorted_without_secrets(self):
        self.bootstrap()
        users = dict(self.state.users)
        users[3] = replace(self.state.users[8], client_id=3, email="inactive@example.test", active=False)
        del users[8]
        self.state = RuntimeState(1, 7, users)
        self.assertEqual([item["client_id"] for item in self.service.status()["users"]], [3, 7, 8])
        self.assertNotIn("token", repr(self.service.status()))

    def test_status_reports_last_success_pending_sources_and_sanitized_errors(self):
        self.bootstrap()

        status = self.service.status()

        self.assertEqual(status["last_success"], self.clock_value)
        self.assertEqual(status["last_errors"], ())
        self.assertEqual(status["pending"], ())
        self.clients.append(client(9, "new@example.test"))

        status = self.service.status()

        self.assertEqual(
            [item["client_id"] for item in status["pending"]], [9]
        )
        self.assertEqual(
            [item["email"] for item in status["pending"]], ["new@example.test"]
        )
        self.assertNotIn("token", repr(status))

    def test_partial_sync_journals_success_with_member_error_codes(self):
        self.fail_client = "sub-8"

        result = self.bootstrap()

        self.assertEqual(result["errors"][0]["code"], "member_update_failed")
        status = self.service.status()
        self.assertEqual(status["last_success"], self.clock_value)
        self.assertEqual(status["last_errors"], ("member_update_failed",))

    def test_activation_failure_journals_error_and_preserves_last_success(self):
        self.bootstrap()
        self.assertEqual(self.service.status()["last_success"], self.clock_value)
        self.activator.fail = True

        with self.assertRaises(ServiceError):
            self.service.sync_all()

        status = self.service.status()
        self.assertEqual(status["last_success"], self.clock_value)
        self.assertEqual(status["last_errors"], ("sync_activation_failed",))

    def test_status_journal_is_a_root_only_atomic_file(self):
        self.bootstrap()

        journal = Path(self.config.private_root) / "status.json"

        self.assertTrue(journal.is_file())
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
        self.assertFalse((Path(self.config.private_root) / "status.json.tmp").exists())

    def test_status_journal_uses_unique_temp_without_clobbering_legacy_name(self):
        legacy_temporary = Path(self.config.private_root) / "status.json.tmp"
        legacy_temporary.write_bytes(b"unrelated journal file")

        self.service._journal(success=self.clock_value)

        journal = Path(self.config.private_root) / "status.json"
        self.assertTrue(legacy_temporary.exists())
        if legacy_temporary.exists():
            self.assertEqual(legacy_temporary.read_bytes(), b"unrelated journal file")
        self.assertEqual(
            json.loads(journal.read_text(encoding="utf-8")),
            {"last_errors": [], "last_success": self.clock_value},
        )

    def test_status_journal_completes_partial_writes_and_syncs_file_and_parent(self):
        journal = Path(self.config.private_root) / "status.json"
        original_write = os.write
        original_fsync = os.fsync
        writes = []
        synced_directories = []

        def partial_write(descriptor, data):
            size = max(1, len(data) // 2)
            written = original_write(descriptor, data[:size])
            writes.append(written)
            return written

        def capture_fsync(descriptor):
            synced_directories.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            return original_fsync(descriptor)

        with patch("clash_sub.service.os.write", side_effect=partial_write), patch(
            "clash_sub.service.os.fsync", side_effect=capture_fsync
        ):
            self.service._journal(success=self.clock_value)

        try:
            persisted = json.loads(journal.read_text(encoding="utf-8"))
        except ValueError:
            persisted = None
        self.assertEqual(
            persisted,
            {"last_errors": [], "last_success": self.clock_value},
        )
        self.assertGreater(len(writes), 1)
        self.assertIn(False, synced_directories)
        self.assertIn(True, synced_directories)

    def test_status_journal_cleans_temporary_file_after_replace_failure(self):
        journal = Path(self.config.private_root) / "status.json"

        with patch("clash_sub.service.os.replace", side_effect=OSError("replace failed")):
            self.service._journal(success=self.clock_value)

        self.assertFalse(journal.exists())
        self.assertEqual(tuple(Path(self.config.private_root).iterdir()), ())

    def test_busy_lock_blocks_mutation_without_state_change(self):
        before = self.state
        self.service._lock_factory = lambda _: (_ for _ in ()).throw(ServiceError("operation_busy"))
        with self.assertRaises(ServiceError) as caught:
            self.service.sync_all()
        self.assertEqual(caught.exception.code, "operation_busy")
        self.assertEqual(self.state, before)

    def test_observer_and_prune_failures_are_sanitized_after_activation(self):
        self.bootstrap()
        # Best-effort failures after a committed activation are reported as
        # stable codes without leaking the underlying private error.
        self.service._sink = lambda _: (_ for _ in ()).throw(RuntimeError("secret"))
        self.store.prune = lambda _: (_ for _ in ()).throw(RuntimeError("secret"))
        self.member_render_text = "member changed for prune"

        result = self.service.sync_all()

        self.assertEqual({item["code"] for item in result["errors"]}, {"release_cleanup_failed"})
        self.assertNotIn("secret", repr(result))

    def test_unchanged_sync_updates_routes_without_render_or_mihomo(self):
        self.bootstrap()
        self.generator_calls.clear(); self.mihomo_calls.clear(); self.store.prepared.clear()
        self.service.sync_all()
        self.assertEqual([call[0] for call in self.generator_calls], [True, False])
        self.assertEqual(self.mihomo_calls, [])
        self.assertEqual(self.store.prepared, [])
        self.assertEqual(len(self.activator.calls), 2)

    def test_member_failure_isolated_while_other_member_updates(self):
        self.bootstrap()
        self.clients.append(client(9, "other@example.test"))
        self.fail_client = "sub-8"
        result = self.service.sync_all()
        self.assertEqual({item["client_id"] for item in result["errors"]}, {8})
        self.assertEqual(self.state.users[8].current_release, self.store.history(8)[0].release_id)
        self.assertEqual(self.state.users[9].current_release, self.store.history(9)[0].release_id)

    def test_new_member_source_failure_persists_identity_without_an_invalid_route(self):
        self.bootstrap()
        self.clients.append(client(9, "failed@example.test"))
        self.fail_client = "sub-9"
        result = self.service.sync_all()
        self.assertEqual(result["errors"], ({"client_id": 9, "code": "member_update_failed"},))
        self.assertIsNone(self.state.users[9].current_release)
        self.assertEqual(len(self.activator.calls), 2)

    def test_owner_failure_keeps_all_old_owner_variants(self):
        self.bootstrap()
        old = self.state.users[7].current_release
        self.fail_owner_render = True
        result = self.service.sync_all()
        self.assertEqual(self.state.users[7].current_release, old)
        self.assertEqual(result["errors"][0]["code"], "owner_update_failed")

    def test_global_snapshot_failure_is_fail_closed_before_mutation(self):
        self.bootstrap()
        before = self.state
        calls = len(self.activator.calls)
        self.service._read_snapshot = lambda _: (_ for _ in ()).throw(RuntimeError("sub-id secret"))
        with self.assertRaises(ServiceError) as caught:
            self.service.sync_all()
        self.assertEqual(caught.exception.code, "xui_snapshot_failed")
        self.assertEqual(self.state, before)
        self.assertEqual(len(self.activator.calls), calls)
        self.assertNotIn("secret", str(caught.exception))

    def test_disable_reenable_and_recreate_follow_route_identity_rules(self):
        self.bootstrap()
        self.service.sync_all(); old = self.state.users[8].token
        self.clients[1] = replace(self.member, enabled=False); self.service.sync_all()
        self.assertFalse(self.state.users[8].active)
        self.clients[1] = self.member; self.service.sync_all(); self.assertEqual(self.state.users[8].token, old)
        self.clients = [self.owner, client(9, "member@example.test")]; self.service.sync_all()
        self.assertFalse(self.state.users[8].active)
        self.assertNotEqual(self.state.users[9].token, old)

    def test_missing_persisted_owner_surfaces_a_distinct_sanitized_reinitialization_code(self):
        self.service._reconcile = lambda *_: (_ for _ in ()).throw(
            StateError("owner_reinitialization_required")
        )

        with self.assertRaises(ServiceError) as caught:
            self.service.status()

        self.assertEqual(caught.exception.code, "owner_reinitialization_required")
        self.assertNotIn("owner@example.test", str(caught.exception))

    def test_reinitialize_owner_revokes_missing_mapping_and_leaves_the_new_owner_pending(self):
        self.bootstrap()
        old_state = self.state
        self.clients = [client(9, "owner@example.test"), self.member]

        def reinitialize(previous, clients, owner_email, client_id):
            self.assertEqual(previous, old_state)
            self.assertEqual(client_id, 9)
            self.assertEqual(owner_email, "owner@example.test")
            return RuntimeState(
                1,
                9,
                {
                    8: previous.users[8],
                    9: UserState(9, "owner@example.test", token(b"n", "PQRSTU"), "PQRSTU", True, None),
                },
            )

        self.service._reinitialize = reinitialize
        before_prepared = tuple(self.store.prepared)

        result = self.service.reinitialize_owner(9)

        self.assertEqual(result, {"owner_client_id": 9})
        self.assertEqual(self.state.owner_client_id, 9)
        self.assertNotIn(7, self.state.users)
        self.assertEqual(self.state.users[8], old_state.users[8])
        self.assertIsNone(self.state.users[9].current_release)
        self.assertEqual(tuple(self.store.prepared), before_prepared)
        self.assertEqual(self.activator.calls[-1][0], self.state)

    def test_rollback_rejects_invalid_current_users_before_release_verification_or_activation(self):
        self.bootstrap()
        release = self.state.users[8].current_release
        baseline_state = self.state
        inactive_users = dict(baseline_state.users)
        inactive_users[8] = replace(inactive_users[8], active=False)
        inactive_state = RuntimeState(1, 7, inactive_users)
        release_less_users = dict(baseline_state.users)
        release_less_users[8] = replace(release_less_users[8], current_release=None)
        release_less_state = RuntimeState(1, 7, release_less_users)
        cases = (
            ("deleted", (self.owner,), baseline_state),
            ("disabled", (self.owner, replace(self.member, enabled=False)), baseline_state),
            ("inactive", (self.owner, self.member), inactive_state),
            ("release-less", (self.owner, self.member), release_less_state),
        )
        verify_release = self.store.verify_release
        verified = []
        self.store.verify_release = lambda *arguments: verified.append(arguments) or verify_release(*arguments)

        for name, clients, state in cases:
            with self.subTest(name=name):
                self.clients = list(clients)
                self.state = state
                self.service._reconcile = lambda *_, expected=state: expected
                calls = len(self.activator.calls)
                verified.clear()

                with self.assertRaises(ServiceError) as caught:
                    self.service.rollback(8, release)

                self.assertEqual(caught.exception.code, "rollback_release_invalid")
                self.assertEqual(verified, [])
                self.assertEqual(self.state, state)
                self.assertEqual(len(self.activator.calls), calls)


if __name__ == "__main__":
    unittest.main()
