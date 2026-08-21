"""Static security contract for the pinned loopback service stack.

The compose file is the deployment boundary of this repository: these tests
pin the properties that must never regress, including loopback-only binding,
digest-pinned images, hardened one-shot services, and a converter
configuration that never logs or serves private subscription targets.
"""

import configparser
import unittest
from pathlib import Path

import yaml

from clash_sub.models import VARIANTS


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "compose.yaml"
DOCKERFILE_PATH = ROOT / "Dockerfile"
DOCKERIGNORE_PATH = ROOT / ".dockerignore"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
PREF_PATH = ROOT / "config" / "subconverter" / "pref.ini"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "synthetic-users.yaml"

SUBCONVERTER_IMAGE = (
    "ghcr.io/metacubex/subconverter:0.9.2@"
    "sha256:58c26f49010c0c069a5b20c85e7f1ac909da8ef704650b34f5001dd84cb9f7b9"
)
MIHOMO_IMAGE = "docker.io/metacubex/mihomo:v1.19.30"
APP_IMAGE = "clash-sub/app:local"
PYTHON_BASE = "python:3.13.13-alpine3.22"
LONG_RUNNING_SERVICES = ("subconverter", "publisher")
ONE_SHOT_SERVICES = ("manager", "validator")


def load_compose():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def bind_mounts(service):
    """Return {source: target/read_only} for the short-syntax bind mounts."""
    binds = {}
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) >= 2:
                binds[parts[0]] = {
                    "target": parts[1],
                    "read_only": "ro" in parts[2:],
                }
    return binds


def tmpfs_mounts(service):
    """Return the long-syntax tmpfs volume entries."""
    return [
        volume
        for volume in service.get("volumes", [])
        if isinstance(volume, dict) and volume.get("type") == "tmpfs"
    ]


class ComposeSecurityTests(unittest.TestCase):
    def test_no_service_publishes_a_port_or_mounts_docker_socket(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        for name, service in compose["services"].items():
            self.assertNotIn("ports", service, name)
            for mount in service.get("volumes", []):
                self.assertNotIn("/var/run/docker.sock", str(mount), name)

    def test_host_network_http_services_bind_loopback(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        self.assertEqual(compose["services"]["subconverter"]["network_mode"], "host")
        self.assertEqual(compose["services"]["publisher"]["network_mode"], "host")
        self.assertEqual(
            compose["services"]["publisher"]["environment"]["PUBLISHER_LISTEN"],
            "127.0.0.1",
        )

    def test_images_are_version_pinned(self):
        compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn(":latest", compose_text)
        self.assertIn("metacubex/subconverter:0.9.2@", compose_text)
        self.assertIn("metacubex/mihomo:v1.19.30", compose_text)

    def test_the_stack_defines_exactly_the_four_pinned_services(self):
        compose = load_compose()
        self.assertEqual(
            set(compose["services"]),
            {"subconverter", "publisher", "manager", "validator"},
        )
        self.assertEqual(compose["services"]["subconverter"]["image"], SUBCONVERTER_IMAGE)
        self.assertEqual(compose["services"]["validator"]["image"], MIHOMO_IMAGE)
        self.assertEqual(compose["services"]["publisher"]["image"], APP_IMAGE)
        self.assertEqual(compose["services"]["manager"]["image"], APP_IMAGE)

    def test_subweb_frontend_is_gone(self):
        compose_text = COMPOSE_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn("subweb", compose_text)
        self.assertNotIn("sub-web", compose_text)

    def test_every_service_is_read_only_without_caps_or_privilege_gain(self):
        compose = load_compose()
        for name, service in compose["services"].items():
            self.assertTrue(service.get("read_only"), name)
            self.assertEqual(service.get("cap_drop"), ["ALL"], name)
            self.assertIn("no-new-privileges:true", service.get("security_opt", []), name)
            mounts = tmpfs_mounts(service)
            self.assertTrue(mounts, "%s needs a tmpfs for runtime writes" % name)
            for mount in mounts:
                size = (mount.get("tmpfs") or {}).get("size")
                self.assertTrue(
                    size, "%s tmpfs %s needs an explicit size cap" % (name, mount.get("target"))
                )

    def test_application_services_run_as_a_non_root_user(self):
        compose = load_compose()
        self.assertEqual(compose["services"]["subconverter"]["user"], "65534:65534")
        # The validator must match the uid that owns the 0700/0600 staging
        # files: cap_drop removes DAC overrides, so even root could not
        # read them otherwise.
        self.assertEqual(compose["services"]["validator"]["user"], "10001:10001")
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertIn("adduser -D -H -u 10001 -G clash-sub clash-sub", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)

    def test_long_running_services_restart_but_one_shot_services_never_do(self):
        compose = load_compose()
        for name in LONG_RUNNING_SERVICES:
            self.assertEqual(compose["services"][name]["restart"], "unless-stopped", name)
        for name in ONE_SHOT_SERVICES:
            self.assertNotIn("restart", compose["services"][name], name)
        for service in compose["services"].values():
            self.assertNotEqual(service.get("restart"), "always")

    def test_one_shot_services_start_only_when_targeted_directly(self):
        compose = load_compose()
        for name in ONE_SHOT_SERVICES:
            self.assertEqual(compose["services"][name].get("profiles"), ["manual"], name)
        for name in LONG_RUNNING_SERVICES:
            self.assertNotIn("profiles", compose["services"][name], name)

    def test_converter_request_logging_is_disabled(self):
        compose = load_compose()
        self.assertEqual(
            compose["services"]["subconverter"]["logging"]["driver"], "none"
        )

    def test_publisher_logs_are_bounded_and_rotated(self):
        compose = load_compose()
        logging_config = compose["services"]["publisher"]["logging"]
        self.assertEqual(logging_config["driver"], "json-file")
        self.assertEqual(logging_config["options"]["max-size"], "10m")
        self.assertEqual(int(logging_config["options"]["max-file"]), 2)

    def test_converter_pref_is_mounted_read_only_over_the_image_default(self):
        compose = load_compose()
        volumes = compose["services"]["subconverter"]["volumes"]
        self.assertIn("./config/subconverter/pref.ini:/base/pref.ini:ro", volumes)

    def test_http_services_have_loopback_health_checks(self):
        compose = load_compose()
        for name, port in (("subconverter", 25500), ("publisher", 25501)):
            check = compose["services"][name]["healthcheck"]["test"]
            joined = " ".join(str(part) for part in check)
            self.assertIn("http://127.0.0.1:%d/" % port, joined, name)


class ComposeServiceBoundaryTests(unittest.TestCase):
    def test_manager_passes_compose_run_arguments_to_the_module_cli(self):
        compose = load_compose()
        self.assertEqual(
            compose["services"]["manager"]["entrypoint"],
            ["python", "-m", "clash_sub.manager"],
        )
        self.assertEqual(compose["services"]["manager"]["network_mode"], "host")

    def test_validator_appends_test_arguments_to_the_mihomo_binary(self):
        compose = load_compose()
        self.assertEqual(compose["services"]["validator"]["entrypoint"], ["/mihomo"])
        self.assertEqual(compose["services"]["validator"]["network_mode"], "none")

    def test_publisher_runs_the_module_server(self):
        compose = load_compose()
        self.assertEqual(
            compose["services"]["publisher"]["command"], ["clash_sub.publisher"]
        )

    def test_validator_mounts_only_the_staging_tree_the_manager_reports(self):
        compose = load_compose()
        validator_binds = bind_mounts(compose["services"]["validator"])
        self.assertEqual(set(validator_binds), {"./private/staging"})
        staging = validator_binds["./private/staging"]
        self.assertTrue(staging["read_only"])
        manager_binds = bind_mounts(compose["services"]["manager"])
        private = manager_binds["./private"]
        self.assertFalse(private["read_only"])
        # The candidate path the manager reports must resolve to the same
        # file inside the validator, so the staging mount has to sit under
        # the manager's container-side private path.
        self.assertTrue(staging["target"].startswith(private["target"] + "/"))

    def test_publisher_mounts_are_read_only_and_exclude_sources_and_staging(self):
        compose = load_compose()
        service = compose["services"]["publisher"]
        binds = bind_mounts(service)
        self.assertTrue(binds, "publisher mounts its read-only inputs")
        for source, mount in binds.items():
            self.assertTrue(mount["read_only"], source)
            lowered = source.lower()
            self.assertNotIn("staging", lowered, source)
            self.assertNotIn("sources", lowered, source)
        self.assertEqual(
            set(binds), {"./private/config", "./private/current", "./private/releases"}
        )


class ServiceExampleConsistencyTests(unittest.TestCase):
    def load_service_example(self):
        document = yaml.safe_load(
            (ROOT / "config" / "service.example.yaml").read_text(encoding="utf-8")
        )
        self.assertIsInstance(document, dict)
        return document

    def test_example_private_root_matches_the_compose_private_mount(self):
        example = self.load_service_example()
        compose = load_compose()
        manager_binds = bind_mounts(compose["services"]["manager"])
        self.assertEqual(example["private-root"], manager_binds["./private"]["target"])

    def test_example_publisher_port_matches_the_compose_healthcheck(self):
        example = self.load_service_example()
        port = example["publication"]["publisher-port"]
        self.assertEqual(port, 25501)
        compose = load_compose()
        joined = " ".join(
            str(part)
            for part in compose["services"]["publisher"]["healthcheck"]["test"]
        )
        self.assertIn("http://127.0.0.1:%d/" % port, joined)


class ApplicationImageTests(unittest.TestCase):
    def test_dockerfile_pins_the_python_base(self):
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertIn("FROM %s" % PYTHON_BASE, dockerfile)

    def test_dockerfile_copies_only_application_inputs(self):
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        copy_sources = []
        for line in dockerfile.splitlines():
            if line.startswith("COPY "):
                copy_sources.extend(line.split()[1:-1])
        self.assertEqual(
            sorted(copy_sources), ["clash_sub", "requirements.txt", "templates"]
        )

    def test_dockerignore_excludes_private_data_and_local_state(self):
        entries = set()
        for line in DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                entries.add(stripped.rstrip("/"))
        for required in (
            ".env",
            ".env.*",
            ".git",
            ".venv",
            "1",
            "__pycache__",
            "docs",
            "generated",
            "private",
            "tests",
        ):
            self.assertIn(required, entries, "dockerignore must exclude %s" % required)


class SubconverterPrefTests(unittest.TestCase):
    def load_pref(self):
        parser = configparser.ConfigParser()
        parser.read_string(PREF_PATH.read_text(encoding="utf-8"))
        return parser

    def test_listener_binds_loopback_port_25500_only(self):
        parser = self.load_pref()
        self.assertEqual(parser.get("server", "listen"), "127.0.0.1")
        self.assertEqual(parser.getint("server", "port"), 25500)
        self.assertEqual(parser.get("server", "serve_file_root"), "")

    def test_api_mode_has_no_default_source_and_no_insertion(self):
        parser = self.load_pref()
        self.assertTrue(parser.getboolean("common", "api_mode"))
        self.assertEqual(parser.get("common", "default_url"), "")
        self.assertFalse(parser.getboolean("common", "enable_insert"))
        self.assertFalse(parser.getboolean("common", "reload_conf_on_request"))

    def test_update_checks_and_managed_config_writes_are_disabled(self):
        parser = self.load_pref()
        self.assertFalse(parser.getboolean("rulesets", "update_ruleset_on_request"))
        self.assertFalse(parser.getboolean("managed_config", "write_managed_config"))


class EnvExampleTests(unittest.TestCase):
    def test_env_example_contains_placeholders_only(self):
        text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        active_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                active_lines.append(stripped)
        self.assertTrue(active_lines, "env example documents at least one variable")
        assignments = {}
        for line in active_lines:
            self.assertRegex(line, r"^[A-Za-z_][A-Za-z0-9_]*=[^\s]+$", line)
            key, value = line.split("=", 1)
            assignments[key] = value
        self.assertNotIn("SUBWEB_PORT", assignments)
        lowered = "\n".join(active_lines).lower()
        for marker in ("token", "secret", "password", "api-key", "api_key"):
            self.assertNotIn(marker, lowered)
        for value in assignments.values():
            self.assertLessEqual(len(value), 64, value)


class SyntheticUsersFixtureTests(unittest.TestCase):
    def load_fixture(self):
        document = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(document, dict)
        return document

    def test_fixture_matches_the_users_settings_contract(self):
        document = self.load_fixture()
        self.assertEqual(document.get("schema-version"), 1)
        users = document.get("users")
        self.assertIsInstance(users, dict)
        self.assertTrue(users)
        owners = 0
        for user_id, user in users.items():
            self.assertIsInstance(user_id, str)
            self.assertIsInstance(user, dict)
            self.assertIn(user["role"], ("owner", "member"))
            self.assertRegex(user["token-sha256"], r"^[0-9a-f]{64}$")
            variants = user["variants"]
            self.assertIsInstance(variants, list)
            self.assertTrue(variants)
            self.assertEqual(len(variants), len(set(variants)))
            for variant in variants:
                self.assertIn(variant, VARIANTS)
            self.assertTrue(user["xui-subscription-url"].startswith("http://127.0.0.1:"))
            sources = user.get("local-sources", {})
            if user["role"] == "owner":
                self.assertIn("airport", sources)
                self.assertIn("home", sources)
                for path in sources.values():
                    self.assertTrue(str(path).startswith("sources/"), path)
            else:
                self.assertEqual(sources, {})
            owners += 1 if user["role"] == "owner" else 0
        self.assertEqual(owners, 1)

    def test_fixture_carries_only_documentation_values(self):
        lowered = FIXTURE_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn("https://", lowered)
        self.assertNotIn("example.com", lowered)
        self.assertNotIn("password", lowered)
        for forbidden_uuid in ("22222222-", "33333333-"):
            self.assertNotIn(forbidden_uuid, lowered)


if __name__ == "__main__":
    unittest.main()
