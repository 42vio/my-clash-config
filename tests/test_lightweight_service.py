import base64
import unittest
from dataclasses import replace
from pathlib import Path

from clash_sub.domain import PreparedRelease, RuntimeState, ServiceConfig, UserState, XuiClient, XuiSnapshot

try:
    from clash_sub.service import ClashSubService, ServiceError
except ImportError:
    ClashSubService = None
    ServiceError = RuntimeError


def token(byte, code):
    return base64.urlsafe_b64encode(byte * 32).decode().rstrip("=") + "-" + code


def client(client_id, email, enabled=True, upload=1):
    return XuiClient(client_id, email, "sub-%s" % client_id, enabled, upload, 2, 3, 4000)


class FakeStore:
    def __init__(self):
        self.releases = {}
        self.prepared = []
        self.pruned = []
        self.counter = 0

    def prepare(self, client_id, bundle, input_hashes):
        current = self.releases.get(client_id, [])
        if current and current[-1].public_paths.keys() == bundle.keys() and getattr(current[-1], "bundle", None) == bundle:
            return None
        self.counter += 1
        release_id = "2026-08-23T12-00-%02dZ-000000%02x" % (self.counter, self.counter)
        release = PreparedRelease(release_id, {name: Path("/releases/%s/%s/clash-%s.yaml" % (client_id, release_id, name)) for name in bundle}, Path("/private/manifest"))
        object.__setattr__(release, "bundle", dict(bundle))
        self.releases.setdefault(client_id, []).append(release)
        self.prepared.append((client_id, release))
        return release

    def verify_release(self, client_id, release_id):
        for release in self.releases.get(client_id, ()):
            if release.release_id == release_id:
                return release
        raise ValueError("missing release")

    def history(self, client_id):
        return tuple(reversed(self.releases.get(client_id, ())))

    def mark_current(self, client_id, release_id):
        self.verify_release(client_id, release_id)

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
        root = Path("/tmp/service")
        self.config = ServiceConfig("owner@example.test", "sub.example.test:8443", root / "xui.db", root / "private", root / "public", root / "routes.conf", Path("/bin/mihomo"), Path("/bin/nginx"), Path("/bin/systemctl"), root / "templates")
        self.generator_calls = []
        self.mihomo_calls = []
        self.fetch_calls = []
        self.airport = [{"name": "Airport"}]
        self.home = [{"name": "Home"}]
        self.airport_write = []
        self.fail_client = None
        self.fail_owner_render = False
        self.service = ClashSubService(
            self.config,
            read_snapshot=lambda _: XuiSnapshot(tuple(self.clients), "127.0.0.1", 2096, "/clash/"),
            load_state=lambda _: self.state,
            reconcile_state=self._reconcile,
            rotate_user_token=self._rotate,
            fetch_xui_proxies=self._fetch,
            download_airport_proxies=lambda url, _: [{"name": "Airport candidate"}],
            load_proxy_snapshot=lambda path: self.airport if path.name == "airport.yaml" else self.home,
            render_user_bundle=self._render,
            validate_clash=lambda text, forbidden: None,
            mihomo_validator=type("Validator", (), {"validate": lambda _, path: self.mihomo_calls.append(path)})(),
            release_store=self.store,
            render_routes=self._routes,
            activate_runtime=self.activator,
            runner=lambda *args, **kwargs: None,
            snapshot_encoder=lambda proxies: ("snapshot:%s" % proxies[0]["name"]).encode(),
            state_sink=lambda state: setattr(self, "state", state),
        )

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
        return [{"name": "Node " + url.rsplit("/", 1)[-1]}]

    def _render(self, is_owner, xui, airport, home, template_root):
        self.generator_calls.append(is_owner)
        if is_owner and self.fail_owner_render:
            raise RuntimeError("owner private failure")
        return {"balanced": "owner balanced", "standard": "owner standard", "privacy": "owner privacy"} if is_owner else {"standard": "member standard"}

    def _routes(self, config, state, clients):
        if any(user.active and user.current_release is None for user in state.users.values()):
            raise ValueError("missing release route")
        return "routes:%s" % sorted(state.users)

    def test_first_sync_activates_owner_and_member_once_without_returning_tokens(self):
        self.assertIsNotNone(ClashSubService)
        result = self.service.sync_all()
        self.assertEqual(len(self.activator.calls), 1)
        self.assertEqual({item["client_id"] for item in result["updated"]}, {7, 8})
        self.assertEqual(self.state.users[7].current_release, self.store.prepared[0][1].release_id)
        self.assertEqual(tuple(self.store.prepared[0][1].public_paths), ("balanced", "standard", "privacy"))
        self.assertTrue(all("token" not in repr(item) for item in result["updated"]))

    def test_unchanged_sync_updates_routes_without_render_or_mihomo(self):
        self.service.sync_all()
        self.generator_calls.clear(); self.mihomo_calls.clear(); self.store.prepared.clear()
        self.service.sync_all()
        self.assertEqual(self.generator_calls, [])
        self.assertEqual(self.mihomo_calls, [])
        self.assertEqual(self.store.prepared, [])
        self.assertEqual(len(self.activator.calls), 2)

    def test_member_failure_isolated_while_other_member_updates(self):
        self.service.sync_all()
        self.clients.append(client(9, "other@example.test"))
        self.fail_client = "sub-8"
        result = self.service.sync_all()
        self.assertEqual({item["client_id"] for item in result["errors"]}, {8})
        self.assertEqual(self.state.users[8].current_release, self.store.history(8)[0].release_id)
        self.assertEqual(self.state.users[9].current_release, self.store.history(9)[0].release_id)

    def test_new_member_source_failure_persists_identity_without_an_invalid_route(self):
        self.service.sync_all()
        self.clients.append(client(9, "failed@example.test"))
        self.fail_client = "sub-9"
        result = self.service.sync_all()
        self.assertEqual(result["errors"], ({"client_id": 9, "email": "failed@example.test", "code": "member_update_failed"},))
        self.assertIsNone(self.state.users[9].current_release)
        self.assertEqual(len(self.activator.calls), 2)

    def test_owner_failure_keeps_all_old_owner_variants(self):
        self.service.sync_all()
        old = self.state.users[7].current_release
        self.service._input_cache.pop(7)
        self.fail_owner_render = True
        result = self.service.sync_all()
        self.assertEqual(self.state.users[7].current_release, old)
        self.assertEqual(result["errors"][0]["code"], "owner_update_failed")

    def test_global_snapshot_failure_is_fail_closed_before_mutation(self):
        self.service.sync_all()
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
        self.service.sync_all(); old = self.state.users[8].token
        self.clients[1] = replace(self.member, enabled=False); self.service.sync_all()
        self.assertFalse(self.state.users[8].active)
        self.clients[1] = self.member; self.service.sync_all(); self.assertEqual(self.state.users[8].token, old)
        self.clients = [self.owner, client(9, "member@example.test")]; self.service.sync_all()
        self.assertFalse(self.state.users[8].active)
        self.assertNotEqual(self.state.users[9].token, old)

    def test_airport_activation_uses_one_transaction_and_hides_url_on_failure(self):
        self.service.sync_all(); before = self.state
        self.activator.fail = True
        secret = "https://airport.example/temporary-secret"
        with self.assertRaises(ServiceError) as caught:
            self.service.update_airport(secret)
        self.assertEqual(caught.exception.code, "airport_activation_failed")
        self.assertEqual(self.state, before)
        self.assertNotIn(secret, str(caught.exception))
        self.assertTrue(self.activator.calls[-1][2])

    def test_traffic_update_never_generates_yaml_and_preserves_state_on_failure(self):
        self.service.sync_all(); before = self.state
        self.generator_calls.clear(); self.mihomo_calls.clear(); self.store.prepared.clear(); self.activator.fail = True
        with self.assertRaises(ServiceError) as caught:
            self.service.traffic_update()
        self.assertEqual(caught.exception.code, "traffic_activation_failed")
        self.assertEqual(self.state, before)
        self.assertEqual(self.generator_calls, [])
        self.assertEqual(self.mihomo_calls, [])
        self.assertEqual(self.store.prepared, [])

    def test_rollback_and_rotation_do_not_fetch_and_rotation_returns_all_urls(self):
        self.service.sync_all(); release = self.state.users[7].current_release
        self.fetch_calls.clear(); self.service.rollback(7, release)
        self.assertEqual(self.fetch_calls, [])
        old = self.state.users[7].token
        rotated = self.service.rotate_link(7)
        self.assertNotEqual(rotated["token"], old)
        self.assertEqual(len(rotated["urls"]), 3)
        self.assertEqual(self.state.users[7].current_release, release)
        self.assertEqual(len(self.service.links()[0]["urls"]), 3)


if __name__ == "__main__":
    unittest.main()
