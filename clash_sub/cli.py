"""Small, secret-safe management interface for ``clash-sub``."""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from clash_sub.checks import MihomoValidator, validate_clash
from clash_sub.config import load_config
from clash_sub.generator import render_user_bundle
from clash_sub.nginx import activate_runtime, recover_runtime, render_routes
from clash_sub.release_store import ReleaseStore
from clash_sub.service import ClashSubService, ServiceError
from clash_sub.sources import (
    download_airport_proxies,
    fetch_xui_proxies,
    load_proxy_snapshot,
)
from clash_sub.state import load_state, reconcile_state, reinitialize_owner, rotate_user_token
from clash_sub.xui import read_xui_snapshot


MENU = (
    "1. 更新机场订阅\n"
    "2. 同步所有配置\n"
    "3. 查看订阅链接\n"
    "4. 查看状态和历史版本\n"
    "0. 退出\n"
)
_ERROR_TEMPLATE = "操作失败（错误代码：%s）\n"


class _CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None, stdin=None, stdout=None, stderr=None, service_factory=None) -> int:
    """Run the interactive menu or one documented maintenance command."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    arguments = list(sys.argv[1:] if argv is None and stdin is sys.stdin else (argv or ()))
    factory = service_factory or _default_service_factory

    if not arguments:
        return _menu(stdin, stdout, stderr, factory)
    if any(_looks_like_url(argument) for argument in arguments):
        return _error(stderr, "invalid_command", 2)
    try:
        parsed = _parser().parse_args(arguments)
    except (SystemExit, ValueError):
        return _error(stderr, "invalid_command", 2)
    return _run_command(parsed, stdout, stderr, factory)


def _menu(stdin, stdout, stderr, factory):
    stdout.write(MENU)
    try:
        choice = stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return 0
    if not choice:
        return 0
    choice = choice.strip()
    if choice == "0":
        return 0
    if choice == "1":
        try:
            airport_url = getpass("请输入机场订阅地址：")
        except (EOFError, KeyboardInterrupt):
            return 0
        if not isinstance(airport_url, str) or not airport_url.strip():
            return _error(stderr, "invalid_airport_url", 2)
        return _call("airport", airport_url, stdout, stderr, factory)
    if choice == "2":
        return _call("sync", None, stdout, stderr, factory)
    if choice == "3":
        return _call("links", None, stdout, stderr, factory)
    if choice == "4":
        return _call("status", None, stdout, stderr, factory, include_history=True)
    return _error(stderr, "invalid_menu_selection", 2)


def _parser():
    parser = _CommandParser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", add_help=False)
    commands.add_parser("traffic-update", add_help=False)
    commands.add_parser("status", add_help=False)
    commands.add_parser("links", add_help=False)
    history = commands.add_parser("history", add_help=False)
    history.add_argument("user")
    rollback = commands.add_parser("rollback", add_help=False)
    rollback.add_argument("user")
    rollback.add_argument("release")
    rotate = commands.add_parser("rotate-link", add_help=False)
    rotate.add_argument("user")
    reinitialize = commands.add_parser("reinitialize-owner", add_help=False)
    reinitialize.add_argument("user")
    commands.add_parser("recover", add_help=False)
    return parser


def _run_command(parsed, stdout, stderr, factory):
    command = parsed.command
    if command in {"history", "rollback", "rotate-link", "reinitialize-owner"}:
        user = _user_id(parsed.user)
        if user is None:
            return _error(stderr, "invalid_command", 2)
    else:
        user = None
    if command == "sync":
        return _call("sync", None, stdout, stderr, factory)
    if command == "traffic-update":
        return _call("traffic", None, stdout, stderr, factory)
    if command == "status":
        return _call("status", None, stdout, stderr, factory)
    if command == "links":
        return _call("links", None, stdout, stderr, factory)
    if command == "history":
        return _call("history", user, stdout, stderr, factory)
    if command == "rollback":
        return _call("rollback", (user, parsed.release), stdout, stderr, factory)
    if command == "rotate-link":
        return _call("rotate", user, stdout, stderr, factory)
    if command == "reinitialize-owner":
        return _call("reinitialize", user, stdout, stderr, factory)
    if command == "recover":
        return _recover(stdout, stderr)
    return _error(stderr, "invalid_command", 2)


def _call(operation, value, stdout, stderr, factory, *, include_history=False):
    try:
        service = factory()
        if operation == "airport":
            service.update_airport(value)
            stdout.write("机场订阅已更新。\n")
        elif operation == "sync":
            result = service.sync_all()
            errors = tuple(result["errors"])
            if errors:
                stdout.write("同步部分完成。\n")
                for error in errors:
                    stderr.write("客户端 ID %s（错误代码：%s）\n" % (error["client_id"], error["code"]))
                return 1
            stdout.write("同步已完成。\n")
        elif operation == "traffic":
            service.traffic_update()
            stdout.write("流量信息已更新。\n")
        elif operation == "links":
            _write_links(stdout, service.links())
        elif operation == "status":
            status = service.status()
            _write_status(stdout, status)
            if include_history:
                _write_all_history(stdout, service, status)
        elif operation == "history":
            _write_history(stdout, value, service.history(value))
        elif operation == "rollback":
            user, release = value
            service.rollback(user, release)
            stdout.write("已回滚用户 %s 到版本 %s。\n" % (user, release))
        elif operation == "rotate":
            result = service.rotate_link(value)
            stdout.write("订阅链接已轮换：\n")
            for url in result["urls"]:
                stdout.write("%s\n" % url)
        elif operation == "reinitialize":
            service.reinitialize_owner(value)
            stdout.write("所有者已重新初始化；请更新机场订阅后执行 sync。\n")
        else:
            return _error(stderr, "invalid_command", 2)
    except ServiceError as error:
        return _error(stderr, error.code, 1)
    except Exception:
        return _error(stderr, "service_unavailable", 1)
    return 0


def _write_links(stdout, links):
    for user in links:
        stdout.write("%s [%s]\n" % (user["email"], user["readable_code"]))
        for url in user["urls"]:
            stdout.write("%s\n" % url)


def _write_status(stdout, status):
    stdout.write("状态：\n")
    stdout.write("所有者客户端 ID：%s\n" % status["owner_client_id"])
    stdout.write("最后成功时间：%s\n" % _format_timestamp(status.get("last_success")))
    errors = status.get("last_errors") or ()
    stdout.write("最近错误：%s\n" % ("、".join(errors) if errors else "无"))
    pending = status.get("pending") or ()
    if pending:
        stdout.write("待同步：\n")
        for item in pending:
            stdout.write("ID %s（%s）\n" % (item["client_id"], item["email"]))
    else:
        stdout.write("待同步：无\n")
    users = sorted(status["users"], key=lambda user: user["client_id"])
    if not users:
        stdout.write("用户：无\n")
        return
    stdout.write("用户：\n")
    for user in users:
        state = "启用" if user["active"] else "停用"
        release = user["current_release"] or "无"
        stdout.write("ID %s：%s（%s，当前版本：%s）\n" % (user["client_id"], user["email"], state, release))


def _format_timestamp(value):
    if not isinstance(value, (int, float)):
        return "无"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _write_all_history(stdout, service, status):
    stdout.write("历史版本：\n")
    for user in sorted(status["users"], key=lambda item: item["client_id"]):
        _write_history(stdout, user["client_id"], service.history(user["client_id"]))


def _write_history(stdout, user, history):
    stdout.write("用户 %s 的历史版本：\n" % user)
    for release in history:
        stdout.write("%s（%s）\n" % (release["release_id"], ", ".join(release["variants"])))


def _user_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _looks_like_url(value):
    return isinstance(value, str) and bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value))


def _error(stderr, code, status):
    stderr.write(_ERROR_TEMPLATE % code)
    return status


def _recover(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "recovery_not_authorized", 1)
    try:
        repo_root = Path(__file__).resolve().parents[1]
        config = load_config(repo_root / "private" / "config" / "service.yaml", repo_root)
        recover_runtime(config, subprocess.run, reload=False)
    except Exception:
        return _error(stderr, "runtime_recovery_failed", 1)
    stdout.write("运行时恢复已完成。\n")
    return 0


def _default_service_factory():
    repo_root = Path(__file__).resolve().parents[1]
    config = load_config(repo_root / "private" / "config" / "service.yaml", repo_root)
    runner = subprocess.run
    return ClashSubService(
        config,
        read_snapshot=read_xui_snapshot,
        load_state=load_state,
        reconcile_state=reconcile_state,
        rotate_user_token=rotate_user_token,
        reinitialize_owner=reinitialize_owner,
        fetch_xui_proxies=fetch_xui_proxies,
        download_airport_proxies=download_airport_proxies,
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
