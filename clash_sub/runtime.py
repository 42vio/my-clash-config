"""Shared service construction for the CLI and the installer."""

import subprocess
from pathlib import Path

from clash_sub.checks import MihomoValidator, validate_clash
from clash_sub.config import load_config
from clash_sub.generator import render_user_bundle
from clash_sub.nginx import activate_runtime, recover_runtime, render_routes
from clash_sub.release_store import ReleaseStore
from clash_sub.service import ClashSubService
from clash_sub.sources import (
    download_airport_document,
    fetch_xui_proxies,
    load_proxy_snapshot,
)
from clash_sub.state import (
    load_state,
    reconcile_state,
    reinitialize_owner,
    rotate_user_token,
)
from clash_sub.xui import read_xui_snapshot


def repo_root():
    return Path(__file__).resolve().parents[1]


def config_path(root=None):
    root = Path(root) if root else repo_root()
    return root / "private" / "config" / "service.yaml"


def build_service(root=None, runner=None):
    root = Path(root) if root else repo_root()
    config = load_config(config_path(root), root)
    runner = runner or subprocess.run
    return ClashSubService(
        config,
        read_snapshot=read_xui_snapshot,
        load_state=load_state,
        reconcile_state=reconcile_state,
        rotate_user_token=rotate_user_token,
        reinitialize_owner=reinitialize_owner,
        fetch_xui_proxies=fetch_xui_proxies,
        download_airport_document=download_airport_document,
        load_proxy_snapshot=load_proxy_snapshot,
        render_user_bundle=render_user_bundle,
        validate_clash=validate_clash,
        mihomo_validator=MihomoValidator(config.mihomo_binary, runner=runner),
        release_store=ReleaseStore(
            config.private_root,
            config.public_root,
            activation_paths=(config.nginx_routes,),
        ),
        render_routes=render_routes,
        activate_runtime=activate_runtime,
        recover_runtime=recover_runtime,
        runner=runner,
    )
