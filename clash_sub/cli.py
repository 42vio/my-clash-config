"""Small, secret-safe management interface for ``clash-sub``."""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from clash_sub import manage
from clash_sub import template_sync
from clash_sub.config import load_config
from clash_sub.installer import Installer, InstallerError
from clash_sub.nginx import recover_runtime
from clash_sub.runtime import build_service, repo_root as default_repo_root
from clash_sub.service import ServiceError, _OperationLock


MENU = (
    "================================\n"
    "      clash-sub 管理菜单\n"
    "================================\n"
    "配置管理\n"
    "  1. 更新机场订阅\n"
    "  2. 重新生成所有配置（不更新代码）\n"
    "  3. 查看订阅链接\n"
    "  4. 查看状态和历史版本\n"
    "\n"
    "程序维护\n"
    "  5. 更新仓库代码\n"
    "  6. 更新仓库代码并同步配置（推荐）\n"
    "  7. 升级 Mihomo 校验器\n"
    "\n"
    "证书与备份\n"
    "  8. 查看证书状态\n"
    "  9. 强制续期证书\n"
    " 10. 创建完整备份\n"
    "\n"
    "故障与用户管理\n"
    " 11. 恢复中断的配置发布\n"
    " 12. 用户历史/回退\n"
    " 13. 轮换用户订阅链接\n"
    " 14. 重新初始化 owner\n"
    " 15. 回滚整合安装\n"
    "\n"
    "  0. 退出\n"
    "================================\n"
)
_ERROR_TEMPLATE = "操作失败（错误代码：%s）\n"
_UPDATE_REMINDER = (
    "代码更新完成。\n"
    "如果本次修改涉及模板或生成逻辑，请继续执行：\n"
    "clash-sub sync\n"
    "\n"
    "也可以以后直接使用：\n"
    "clash-sub update && clash-sub sync\n"
)
_SYNC_SPAWN_TIMEOUT = 900


class _CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None, stdin=None, stdout=None, stderr=None, service_factory=None) -> int:
    """Run the interactive menu or one documented maintenance command."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    arguments = list(sys.argv[1:] if argv is None and stdin is sys.stdin else (argv or ()))
    factory = service_factory or (lambda: build_service())

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
    """Loop the full menu: invalid input stays, failures exit, 0/EOF leaves."""
    while True:
        stdout.write(MENU)
        try:
            choice = stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return 0
        choice = choice.strip()
        if not choice or choice == "0":
            return 0
        code, exit_menu = _menu_dispatch(choice, stdin, stdout, stderr, factory)
        if exit_menu or code != 0:
            return code


def _menu_dispatch(choice, stdin, stdout, stderr, factory):
    if choice == "1":
        return _menu_airport(stdout, stderr, factory)
    if choice == "2":
        return _call("sync", None, stdout, stderr, factory), False
    if choice == "3":
        return _call("links", None, stdout, stderr, factory), False
    if choice == "4":
        return _call("status", None, stdout, stderr, factory, include_history=True), False
    if choice == "5":
        return _menu_update(stdout, stderr)
    if choice == "6":
        return _menu_update_and_sync(stdout, stderr)
    if choice == "7":
        return _menu_mihomo_update(stdin, stdout, stderr)
    if choice == "8":
        return _menu_cert(stdin, stdout, stderr, renew=False)
    if choice == "9":
        return _menu_cert(stdin, stdout, stderr, renew=True)
    if choice == "10":
        return _managed(stdout, stderr, manage.create_backup), False
    if choice == "11":
        return _recover(stdout, stderr), False
    if choice == "12":
        return _menu_history_rollback(stdin, stdout, stderr, factory)
    if choice == "13":
        return _menu_single_user(stdin, stdout, stderr, factory, "rotate")
    if choice == "14":
        return _menu_single_user(stdin, stdout, stderr, factory, "reinitialize")
    if choice == "15":
        return _menu_install_rollback(stdin, stdout, stderr)
    _error(stderr, "invalid_menu_selection", 2)
    return 0, False


def _menu_airport(stdout, stderr, factory):
    try:
        airport_url = getpass("请输入机场订阅地址：")
    except (EOFError, KeyboardInterrupt):
        return 0, False
    if not isinstance(airport_url, str) or not airport_url.strip():
        return _error(stderr, "invalid_airport_url", 2), False
    return _call("airport", airport_url, stdout, stderr, factory), False


def _menu_update(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1), False
    try:
        manage.run_update(default_repo_root(), subprocess.run)
    except RuntimeError as error:
        return _error(stderr, str(error), 1), False
    stdout.write(_UPDATE_REMINDER)
    return 0, True


def _menu_update_and_sync(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1), False
    try:
        manage.run_update(default_repo_root(), subprocess.run)
    except RuntimeError as error:
        return _error(stderr, str(error), 1), False
    stdout.write(_UPDATE_REMINDER)
    root = default_repo_root()
    try:
        result = subprocess.run(
            [str(root / ".venv" / "bin" / "python"), str(root / "bin" / "clash-sub"), "sync"],
            stdin=subprocess.DEVNULL,
            timeout=_SYNC_SPAWN_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return _error(stderr, "menu_sync_failed", 1), True
    if result.returncode != 0:
        return _error(stderr, "menu_sync_failed", 1), True
    # The spawned process already printed its own completion output; the
    # parent must not duplicate it.
    return 0, True


def _menu_cert(stdin, stdout, stderr, *, renew):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1), False
    if renew and not _confirm(stdin, stdout, "确认强制续期证书？(y/N)："):
        stdout.write("已取消。\n")
        return 0, False
    try:
        if renew:
            manage.cert_renew(default_repo_root(), subprocess.run)
            stdout.write("证书续期已触发。\n")
        else:
            status = manage.cert_status(default_repo_root(), subprocess.run)
            stdout.write("证书存在：%s\n" % ("是" if status["present"] else "否"))
            stdout.write("到期时间：%s\n" % status["not_after"].split("=", 1)[-1])
    except RuntimeError as error:
        return _error(stderr, str(error), 1), False
    except Exception:
        return _error(stderr, "cert_command_failed", 1), False
    return 0, False


def _menu_mihomo_update(stdin, stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1), False
    if not _confirm(stdin, stdout, "确认检查并升级 Mihomo？(y/N)："):
        stdout.write("已取消。\n")
        return 0, False
    code = _mihomo_update(stdout, stderr)
    return code, False


def _menu_history_rollback(stdin, stdout, stderr, factory):
    user = _menu_user_id(stdin, stdout, stderr)
    if user is None:
        return 0, False
    try:
        service = factory()
        history = service.history(user)
    except Exception:
        return _error(stderr, "service_unavailable", 1), False
    _write_history(stdout, user, history)
    release = _prompt(stdin, stdout, "输入要回退的版本 ID（留空返回菜单）：")
    if not release:
        return 0, False
    if not _confirm(stdin, stdout, "确认回退用户 %d 到版本 %s？(y/N)：" % (user, release)):
        stdout.write("已取消。\n")
        return 0, False
    try:
        service.rollback(user, release)
    except ServiceError as error:
        return _error(stderr, error.code, 1), False
    except Exception:
        return _error(stderr, "service_unavailable", 1), False
    stdout.write("已回滚用户 %d 到版本 %s。\n" % (user, release))
    return 0, False


def _menu_single_user(stdin, stdout, stderr, factory, operation):
    user = _menu_user_id(stdin, stdout, stderr)
    if user is None:
        return 0, False
    if operation == "rotate":
        message = "将轮换用户 %d 的订阅链接，旧链接立即失效。确认执行？(y/N)：" % user
        success = "订阅链接已轮换：\n"
    else:
        message = "将重新初始化 owner 为用户 %d，旧 owner 路由立即撤销。确认执行？(y/N)：" % user
        success = "所有者已重新初始化；请更新机场订阅后执行 sync。\n"
    if not _confirm(stdin, stdout, message):
        stdout.write("已取消。\n")
        return 0, False
    try:
        if operation == "rotate":
            result = factory().rotate_link(user)
            stdout.write(success)
            for url in result["urls"]:
                stdout.write("%s\n" % url)
        else:
            factory().reinitialize_owner(user)
            stdout.write(success)
    except ServiceError as error:
        return _error(stderr, error.code, 1), False
    except Exception:
        return _error(stderr, "service_unavailable", 1), False
    return 0, False


def _menu_install_rollback(stdin, stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1), False
    stdout.write(
        "即将回滚整合安装：删除 clash-sub systemd unit、nginx 配置与发布目录\n"
        "（保留 3x-ui 数据库与已签发证书）。此操作不可自动撤销。\n"
        "输入 ROLLBACK 确认回滚安装：\n"
    )
    try:
        answer = stdin.readline()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip() != "ROLLBACK":
        stdout.write("已取消。\n")
        return 0, False
    try:
        Installer(default_repo_root()).rollback_install()
    except InstallerError as error:
        return _error(stderr, error.code, 1), False
    stdout.write("已回滚安装。若此前已收口（inbound listen=127.0.0.1），请在 3x-ui 面板把 Reality 入站 listen 改回 0.0.0.0 以恢复公网 10443 直连。\n")
    return 0, False


def _menu_user_id(stdin, stdout, stderr):
    raw = _prompt(stdin, stdout, "请输入用户 ID：")
    user = _user_id(raw) if raw is not None else None
    if user is None:
        _error(stderr, "invalid_command", 2)
        return None
    return user


def _prompt(stdin, stdout, message):
    stdout.write(message)
    try:
        line = stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return None
    return line.strip() if line else None


def _confirm(stdin, stdout, message):
    answer = _prompt(stdin, stdout, message)
    return answer is not None and answer.lower() == "y"


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
    rollback.add_argument("user", nargs="?")
    rollback.add_argument("release", nargs="?")
    rollback.add_argument("--install", action="store_true")
    rotate = commands.add_parser("rotate-link", add_help=False)
    rotate.add_argument("user")
    reinitialize = commands.add_parser("reinitialize-owner", add_help=False)
    reinitialize.add_argument("user")
    commands.add_parser("recover", add_help=False)
    commands.add_parser("install", add_help=False)
    commands.add_parser("backup", add_help=False)
    commands.add_parser("template-sync", add_help=False)
    commands.add_parser("mihomo-update", add_help=False)
    update = commands.add_parser("update", add_help=False)
    update.add_argument("--post-update", action="store_true")
    cert = commands.add_parser("cert", add_help=False)
    cert.add_argument("--renew", action="store_true")
    cert.add_argument("--domain")
    return parser


def _run_command(parsed, stdout, stderr, factory):
    command = parsed.command
    if command in {"history", "rollback", "rotate-link", "reinitialize-owner"}:
        user = _user_id(parsed.user) if parsed.user is not None else None
        if user is None and not (command == "rollback" and parsed.install):
            return _error(stderr, "invalid_command", 2)
    else:
        user = None
    if command == "rollback":
        if parsed.install:
            if parsed.user is not None or parsed.release is not None:
                return _error(stderr, "invalid_command", 2)
            return _rollback_install(stdout, stderr)
        if parsed.user is None or parsed.release is None:
            return _error(stderr, "invalid_command", 2)
    if command == "install":
        return _install(stdout, stderr)
    if command == "backup":
        return _managed(stdout, stderr, manage.create_backup)
    if command == "update":
        action = (
            manage.run_post_update
            if getattr(parsed, "post_update", False)
            else manage.run_update
        )
        return _managed(stdout, stderr, action, success_output=_UPDATE_REMINDER)
    if command == "template-sync":
        return _template_sync(stdout, stderr)
    if command == "mihomo-update":
        return _mihomo_update(stdout, stderr)
    if command == "cert":
        return _cert_command(parsed, stdout, stderr)
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
            try:
                report = manage.health_report(default_repo_root(), subprocess.run)
            except Exception:
                report = None
            if report:
                stdout.write("nginx：%s；x-ui：%s\n" % (report["units"]["nginx"], report["units"]["x-ui"]))
                days = report["certificate"]["days_left"]
                stdout.write("证书：%s（剩余 %s 天）\n" % (report["certificate"]["not_after"], days if days is not None else "未知"))
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


def _install(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    domain = os.environ.get("CLASH_SUB_DOMAIN", "")
    if not domain:
        try:
            stdout.write("请输入主域名（例如 example.com）：\n")
            domain = input().strip()
        except (EOFError, KeyboardInterrupt):
            return _error(stderr, "invalid_domain", 2)
    if not domain:
        return _error(stderr, "invalid_domain", 2)
    try:
        token = getpass("请输入 Cloudflare API Token：")
    except (EOFError, KeyboardInterrupt):
        return _error(stderr, "missing_cf_token", 2)
    try:
        swap_mb = int(os.environ.get("CLASH_SUB_SWAP_MB", "0"))
    except ValueError:
        return _error(stderr, "invalid_swap", 2)
    owner = os.environ.get("CLASH_SUB_OWNER_EMAIL", "")
    if not owner:
        suggested = _suggest_owner_email()
        try:
            stdout.write(
                "owner 的 3x-ui client email（回车使用 %s）：\n"
                % (suggested or "无建议（存在多个启用的 client，请明确输入）")
            )
            owner = input().strip() or suggested
        except (EOFError, KeyboardInterrupt):
            return _error(stderr, "owner_email_required", 2)
        if not owner:
            return _error(stderr, "owner_email_required", 2)
    node_host = os.environ.get("CLASH_SUB_NODE_HOST", "") or None
    try:
        installer = Installer(
            default_repo_root(), print_fn=lambda message: stdout.write("%s\n" % message)
        )
        report = installer.install(
            domain=domain,
            cf_token=token,
            swap_mb=swap_mb,
            owner_email=owner,
            node_host=node_host,
        )
    except InstallerError as error:
        return _error(stderr, error.code, 1)
    stdout.write("面板地址：%s\n" % report.get("panel_url", ""))
    stdout.write("%s\n" % report.get("gate_instruction", ""))
    return 0


def _suggest_owner_email():
    try:
        from clash_sub.installer import InstallPaths
        from clash_sub.xui import read_xui_snapshot

        snapshot = read_xui_snapshot(InstallPaths().xui_database)
        enabled = [client.email for client in snapshot.clients if client.enabled]
        return enabled[0] if len(enabled) == 1 else ""
    except Exception:
        return ""


def _rollback_install(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    try:
        Installer(default_repo_root()).rollback_install()
    except InstallerError as error:
        return _error(stderr, error.code, 1)
    stdout.write("已回滚安装。若此前已收口（inbound listen=127.0.0.1），请在 3x-ui 面板把 Reality 入站 listen 改回 0.0.0.0 以恢复公网 10443 直连。\n")
    return 0


def _recover(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "recovery_not_authorized", 1)
    try:
        root = default_repo_root()
        config = load_config(root / "private" / "config" / "service.yaml", root)
        with _OperationLock(Path(config.private_root) / "operation.lock"):
            recover_runtime(config, subprocess.run, reload=False)
    except ServiceError as error:
        return _error(stderr, error.code, 1)
    except Exception:
        return _error(stderr, "runtime_recovery_failed", 1)
    stdout.write("运行时恢复已完成。\n")
    return 0


def _managed(stdout, stderr, action, success_output=None):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    try:
        action(default_repo_root(), subprocess.run)
    except RuntimeError as error:
        return _error(stderr, str(error), 1)
    except Exception:
        return _error(stderr, "management_command_failed", 1)
    stdout.write(success_output if success_output is not None else "操作已完成。\n")
    return 0


def _mihomo_update(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    try:
        result = manage.update_mihomo(default_repo_root(), subprocess.run)
    except RuntimeError as error:
        return _error(stderr, str(error), 1)
    except Exception:
        return _error(stderr, "mihomo_update_failed", 1)
    if result["changed"]:
        stdout.write("Mihomo 已升级到 %s；建议执行 clash-sub sync。\n" % result["version"])
    else:
        stdout.write("Mihomo 已是最新稳定版 %s。\n" % result["version"])
    return 0


def _template_sync(stdout, stderr):
    try:
        result = template_sync.run_template_sync(default_repo_root())
    except template_sync.TemplateSyncError as error:
        return _error(stderr, error.code, 1)
    except Exception:
        return _error(stderr, "template_sync_failed", 1)
    for relative in result["changed"]:
        stdout.write("%s\n" % relative)
    stdout.write("模板已同步。请查看 git diff，运行测试后再提交。\n")
    return 0


def _cert_command(parsed, stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    try:
        if parsed.domain:
            return _error(stderr, "domain_change_unsupported", 2)
        if parsed.renew:
            manage.cert_renew(default_repo_root(), subprocess.run)
            stdout.write("证书续期已触发。\n")
        else:
            status = manage.cert_status(default_repo_root(), subprocess.run)
            stdout.write("证书存在：%s\n" % ("是" if status["present"] else "否"))
            stdout.write("到期时间：%s\n" % status["not_after"].split("=", 1)[-1])
    except RuntimeError as error:
        return _error(stderr, str(error), 1)
    except Exception:
        return _error(stderr, "cert_command_failed", 1)
    return 0
