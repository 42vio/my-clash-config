"""Small, secret-safe management interface for ``clash-sub``."""

import argparse
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from clash_sub import manage
from clash_sub import metadata_server
from clash_sub import template_sync
from clash_sub.config import load_config
from clash_sub.domain import AIRPORT_FILENAME
from clash_sub.installer import Installer, InstallerError
from clash_sub.metadata import TrafficMetadataStore
from clash_sub.nginx import recover_runtime
from clash_sub.runtime import build_service, config_path, repo_root as default_repo_root
from clash_sub.service import ServiceError, _OperationLock


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
_MENU_BOX_WIDTH = 46

_GREEN = "\033[0;32m"
_YELLOW = "\033[0;33m"
_RED = "\033[0;31m"
_RESET = "\033[0m"


class _Colors:
    """Terminal palette that degrades to plain text when disabled."""

    def __init__(self, enabled):
        self.enabled = enabled

    def _paint(self, text, code):
        return code + text + _RESET if self.enabled else text

    def green(self, text):
        return self._paint(text, _GREEN)

    def yellow(self, text):
        return self._paint(text, _YELLOW)

    def gray(self, text):
        return self._paint(text, "\033[0;90m")

    def red(self, text):
        return self._paint(text, _RED)


def _colors_for(stream):
    try:
        return _Colors(bool(stream.isatty()))
    except (AttributeError, ValueError):
        return _Colors(False)


# 每行是 ("title", 文本) / ("divider",) / ("option", 编号, ((文本, 颜色), ...))
_MAIN_MENU_ROWS = (
    ("title", "clash-sub 管理脚本"),
    ("option", "0", (("退出", None),)),
    ("divider",),
    ("option", "1", (("机场订阅", None),)),
    ("option", "2", (("重新生成所有配置", None),)),
    ("option", "3", (("查看用户链接", None),)),
    ("option", "4", (("订阅页面与 3x-ui 管理面板", None),)),
    ("option", "5", (("查看运行状态", None),)),
    ("divider",),
    ("option", "6", (("程序维护", None),)),
    ("option", "7", (("证书管理", None),)),
    ("option", "8", (("备份与恢复", None),)),
    ("option", "9", (("用户与版本", None),)),
)

_AIRPORT_MENU_ROWS = (
    ("title", "机场订阅"),
    ("option", "1", (("设置订阅开关页面", None),)),
    ("option", "2", (("自动开启订阅并刷新", None), ("（推荐）", "green"))),
    ("option", "3", (("手动开启订阅后刷新", None),)),
    ("option", "4", (("使用新订阅链接更新", None),)),
    ("option", "5", (("查看机场状态", None),)),
    ("option", "6", (("自建备用订阅", None),)),
    ("option", "0", (("返回", None),)),
)

_BACKUP_SUBSCRIPTION_ROWS = (
    ("title", "自建备用订阅"),
    ("option", "1", (("添加或更换备用订阅链接", None),)),
    ("option", "2", (("刷新备用订阅并生成配置", None),)),
    ("option", "3", (("删除备用订阅并生成配置", None),)),
    ("option", "4", (("查看备用状态", None),)),
    ("option", "5", (("导入或替换手动节点链接", None),)),
    ("option", "6", (("删除手动节点并生成配置", None),)),
    ("option", "0", (("返回", None),)),
)

_MAINTENANCE_MENU_ROWS = (
    ("option", "1", (("更新代码并同步配置", None), ("（推荐）", "green"))),
    ("option", "2", (("仅更新仓库代码", None),)),
    ("option", "3", (("升级 Mihomo 校验器", None),)),
    ("option", "0", (("返回主菜单", None),)),
)

_CERT_MENU_ROWS = (
    ("option", "1", (("查看证书状态", None),)),
    ("option", "2", (("强制续期", "red"), ("证书", None))),
    ("option", "0", (("返回主菜单", None),)),
)

_BACKUP_MENU_ROWS = (
    ("option", "1", (("创建完整备份", None),)),
    ("option", "2", (("恢复中断的配置发布", None),)),
    ("option", "3", (("回滚", "red"), ("整合安装", None))),
    ("option", "0", (("返回主菜单", None),)),
)

_USER_MENU_ROWS = (
    ("option", "1", (("查看用户历史版本", None),)),
    ("option", "2", (("回退", "red"), ("用户版本", None))),
    ("option", "3", (("轮换", "red"), ("用户订阅链接", None))),
    ("option", "4", (("重新初始化", "red"), (" owner", None))),
    ("option", "5", (("更换用户网页链接", None),)),
    ("option", "0", (("返回主菜单", None),)),
)

_WEB_MENU_ROWS = (
    ("title", "订阅页面与 3x-ui 管理面板"),
    ("section", "订阅页面"),
    ("option", "1", (("初始化订阅页面（关闭 3x-ui 管理面板公网访问）", None),)),
    ("option", "2", (("设置网页管理员账号与密码", None),)),
    ("option", "3", (("开启订阅页面", None),)),
    ("option", "4", (("关闭订阅页面（保留订阅下载）", None),)),
    ("section", "3x-ui 管理面板"),
    ("option", "5", (("开启公网访问", None),)),
    ("option", "6", (("关闭公网访问", None),)),
    ("option", "7", (("SSH 隧道使用说明", None),)),
    ("section", "API 配置"),
    ("option", "8", (("设置 3x-ui API 令牌", None),)),
    ("option", "9", (("搬瓦工 API 设置", None),)),
    ("spacer",),
    ("option", "0", (("返回主菜单", None),)),
)

_BWH_MENU_ROWS = (
    ("title", "搬瓦工 API 设置"),
    ("option", "1", (("添加或更换 API 配置", None),)),
    ("option", "2", (("验证连接并查看套餐用量", None),)),
    ("option", "3", (("删除 API 配置", "red"),)),
    ("option", "0", (("返回上一级", None),)),
)


def _display_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _render_label(segments, colors):
    painted = []
    plain = []
    for text, tone in segments:
        if tone == "green":
            painted.append(colors.green(text))
        elif tone == "red":
            painted.append(colors.red(text))
        else:
            painted.append(text)
        plain.append(text)
    return "".join(painted), "".join(plain)


def _render_option_line(number, segments, colors):
    painted, plain = _render_label(segments, colors)
    return colors.green("%s." % number), painted, _display_width("%s. %s" % (number, plain))


def _render_main_menu(colors):
    rule = "─" * _MENU_BOX_WIDTH
    lines = ["╔%s╗\n" % rule]
    for row in _MAIN_MENU_ROWS:
        if row[0] == "divider":
            lines.append("│%s│\n" % rule)
        elif row[0] == "title":
            padding = " " * (_MENU_BOX_WIDTH - 2 - _display_width(row[1]))
            lines.append("│  %s%s│\n" % (colors.green(row[1]), padding))
        else:
            _, number, segments = row
            number_painted, label_painted, width = _render_option_line(number, segments, colors)
            padding = " " * (_MENU_BOX_WIDTH - 2 - width)
            lines.append("│  %s %s%s│\n" % (number_painted, label_painted, padding))
    lines.append("╚%s╝\n" % rule)
    lines.append("\n")
    return "".join(lines)


def _paint_access_status(status, colors):
    for label, paint in (('● 已开启',colors.green),('○ 已关闭',colors.gray),('? 未初始化',colors.yellow),('? 未知',colors.yellow)):
        status=status.replace(label,paint(label))
    return status


def _render_submenu(rows, colors, *, status=None):
    lines = []
    for row in rows:
        if row[0] == "title":
            lines.append(colors.green(row[1]) + "\n")
            if status is not None:
                lines.append(_paint_access_status(status, colors) + "\n")
            lines.append("\n")
            continue
        if row[0] == "section":
            lines.append("\n" + colors.yellow("── %s ──" % row[1]) + "\n")
            continue
        if row[0] == "spacer":
            lines.append("\n")
            continue
        _, number, segments = row
        number_painted, label_painted, _width = _render_option_line(number, segments, colors)
        lines.append("%s %s\n" % (number_painted, label_painted))
    lines.append("\n")
    return "".join(lines)


MENU = _render_main_menu(_Colors(False)) + "请输入选项 [0-9]："
AIRPORT_MENU = _render_submenu(_AIRPORT_MENU_ROWS, _Colors(False)) + "请输入选项 [0-6]："
MAINTENANCE_MENU = _render_submenu(_MAINTENANCE_MENU_ROWS, _Colors(False)) + "请输入选项 [0-3]："
CERT_MENU = _render_submenu(_CERT_MENU_ROWS, _Colors(False)) + "请输入选项 [0-2]："
BACKUP_MENU = _render_submenu(_BACKUP_MENU_ROWS, _Colors(False)) + "请输入选项 [0-3]："
USER_MENU = _render_submenu(_USER_MENU_ROWS, _Colors(False)) + "请输入选项 [0-5]："
_WEB_STATUS_UNKNOWN = '订阅页面 ? 未知    3x-ui 面板 ? 未知'
WEB_MENU = _render_submenu(_WEB_MENU_ROWS, _Colors(False), status=_WEB_STATUS_UNKNOWN) + "请输入选项 [0-9]："


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


class _MenuExit(Exception):
    """EOF or Ctrl-C anywhere in the menu leaves quietly with status 0."""

    def __init__(self, code=0):
        super().__init__(code)
        self.code = code


def _menu(stdin, stdout, stderr, factory):
    """Loop the menu tree: invalid input stays, failures exit, 0/EOF leaves."""
    try:
        return _main_menu(stdin, stdout, stderr, factory, _colors_for(stdout))
    except _MenuExit as exit:
        return exit.code


def _main_menu(stdin, stdout, stderr, factory, colors):
    expanded = True
    while True:
        if expanded:
            stdout.write(_render_main_menu(colors))
        prompt = None if expanded else colors.yellow("\n主菜单：输入下一项 [1-9]，回车查看菜单，0 退出：")
        choice = _read_choice(stdin, stdout, 9, prompt=prompt)
        expanded = False
        if choice == "":
            expanded = True
            continue
        if choice == "0":
            return 0
        outcome = _main_dispatch(choice, stdin, stdout, stderr, factory, colors)
        if outcome is None:
            _menu_error(stderr, "invalid_menu_selection")
            continue
        code, exit_menu = outcome
        if exit_menu or code != 0:
            raise _MenuExit(code)
        # Returning from a submenu changes context; show the parent menu.
        expanded = choice in {"1", "3", "4", "6", "7", "8", "9"}


def _main_dispatch(choice, stdin, stdout, stderr, factory, colors):
    if choice == "4":
        return _run_submenu(
            _WEB_MENU_ROWS, 9, _web_dispatch, stdin, stdout, stderr, factory, colors, name="订阅页面与 3x-ui 管理面板"
        )
    if choice == "1":
        return _run_submenu(
            _AIRPORT_MENU_ROWS, 6, _airport_dispatch, stdin, stdout, stderr, factory, colors, name="机场订阅"
        )
    if choice == "2":
        return _call("sync", None, stdout, stderr, factory), False
    if choice == "3":
        return _menu_user_links(stdin,stdout,stderr,factory)
    if choice == "5":
        return _call("status", None, stdout, stderr, factory), False
    if choice == "6":
        return _run_submenu(
            _MAINTENANCE_MENU_ROWS, 3, _maintenance_dispatch, stdin, stdout, stderr, factory, colors, name="程序维护"
        )
    if choice == "7":
        return _run_submenu(
            _CERT_MENU_ROWS, 2, _cert_dispatch, stdin, stdout, stderr, factory, colors, name="证书管理"
        )
    if choice == "8":
        return _run_submenu(
            _BACKUP_MENU_ROWS, 3, _backup_dispatch, stdin, stdout, stderr, factory, colors, name="备份与恢复"
        )
    if choice == "9":
        return _run_submenu(
            _USER_MENU_ROWS, 5, _user_dispatch, stdin, stdout, stderr, factory, colors, name="用户与版本"
        )
    return None


def _run_submenu(rows, maximum, dispatch, stdin, stdout, stderr, factory, colors, *, name):
    expanded = True
    while True:
        if expanded:
            status = _web_access_status() if rows is _WEB_MENU_ROWS else None
            stdout.write(_render_submenu(rows, colors, status=status))
        prompt = None if expanded else colors.yellow("\n%s：输入下一项 [1-%d]，回车查看菜单，0 返回上一级：" % (name, maximum))
        choice = _read_choice(stdin, stdout, maximum, prompt=prompt)
        expanded = False
        if choice == "":
            expanded = True
            continue
        if choice == "0":
            return 0, False
        outcome = dispatch(choice, stdin, stdout, stderr, factory)
        if outcome is None:
            _menu_error(stderr, "invalid_menu_selection")
            continue
        code, exit_menu = outcome
        if exit_menu or code != 0:
            return code, True
        if rows is _WEB_MENU_ROWS and choice in {'1', '3', '4', '5', '6'}:
            stdout.write('\n' + _paint_access_status(_web_access_status(),colors) + '\n')


def _web_access_status():
    from clash_sub.web_manage import access_status
    try:
        return access_status(default_repo_root())
    except Exception:
        return _WEB_STATUS_UNKNOWN


def _airport_dispatch(choice, stdin, stdout, stderr, factory):
    if choice == "6":
        return _run_submenu(_BACKUP_SUBSCRIPTION_ROWS,6,_backup_subscription_dispatch,stdin,stdout,stderr,factory,_colors_for(stdout),name="自建备用订阅")
    if choice == "1":
        return _menu_airport_portal(stdin, stdout, stderr, factory)
    if choice == "2":
        return _call("airport-auto-refresh", None, stdout, stderr, factory), False
    if choice == "3":
        return _menu_airport_manual_refresh(stdin, stdout, stderr, factory)
    if choice == "4":
        return _menu_airport_source(stdin, stdout, stderr, factory)
    if choice == "5":
        return _call("airport-status", None, stdout, stderr, factory), False
    return None


def _backup_subscription_dispatch(choice, stdin, stdout, stderr, factory):
    if choice == '1':
        value=_prompt(stdin,stdout,'请输入备用订阅 HTTPS 链接（留空返回）：')
        if not value:return 0,False
        return _call('backup-replace',value,stdout,stderr,factory),False
    if choice == '2':return _call('backup-refresh',None,stdout,stderr,factory),False
    if choice == '3':
        stdout.write('删除后重新生成标准订阅，自建策略组保留；客户端需更新订阅。\n')
        answer=_prompt(stdin,stdout,'输入 DELETE 确认删除（其他输入取消）：')
        if answer!='DELETE':return 0,False
        return _call('backup-remove',None,stdout,stderr,factory),False
    if choice == '4':return _call('backup-status',None,stdout,stderr,factory),False
    if choice == '5':
        stdout.write('每行一个 VLESS、Trojan 或 Shadowsocks 链接，空行结束；替换现有手动节点，保留订阅来源。\n')
        lines=[];length=0
        while True:
            line=stdin.readline(1048577)
            if not line.strip():break
            length+=len(line.encode())
            if length>1048576 or len(lines)>=1000:return _error(stderr,'backup_nodes_invalid',1),False
            lines.append(line.strip())
        if not lines:return 0,False
        return _call('backup-import','\n'.join(lines),stdout,stderr,factory),False
    if choice == '6':
        answer=_prompt(stdin,stdout,'仅删除手动节点，保留订阅来源。输入 DELETE 确认：')
        if answer!='DELETE':return 0,False
        return _call('backup-remove-manual',None,stdout,stderr,factory),False
    return None


def _maintenance_dispatch(choice, stdin, stdout, stderr, factory):
    if choice == "1":
        return _menu_update_and_sync(stdout, stderr)
    if choice == "2":
        return _menu_update(stdout, stderr)
    if choice == "3":
        return _menu_mihomo_update(stdin, stdout, stderr)
    return None


def _cert_dispatch(choice, stdin, stdout, stderr, factory):
    if choice in ("1", "2"):
        return _menu_cert(stdin, stdout, stderr, renew=choice == "2")
    return None


def _backup_dispatch(choice, stdin, stdout, stderr, factory):
    if choice == "1":
        return _managed(stdout, stderr, manage.create_backup), False
    if choice == "2":
        return _recover(stdout, stderr), False
    if choice == "3":
        return _menu_install_rollback(stdin, stdout, stderr)
    return None


def _user_dispatch(choice, stdin, stdout, stderr, factory):
    if choice == '5':
        user = _menu_user_id(stdin,stdout,stderr,factory)
        if user is None:
            return 0,False
        target = str(user)
        if factory().status()['owner_client_id']==user:
            kind = _prompt(stdin,stdout,'网页类型：1 个人，2 机场专用，3 管理员入口（留空返回）：')
            if not kind:
                return 0,False
            if kind not in {'1','2','3'}:
                return _error(stderr,'invalid_menu_selection',2),False
            target = {'1':str(user),'2':'airport','3':'admin'}[kind]
        if not _confirm(stdin,stdout,'旧网页链接将失效，订阅地址不变。确认更换？(y/N)：'):
            return 0,False
        return _web_command('rotate-page',target,stdout,stderr),False
    if choice == "1":
        return _menu_history_view(stdin, stdout, stderr, factory)
    if choice == "2":
        return _menu_rollback(stdin, stdout, stderr, factory)
    if choice == "3":
        return _menu_single_user(stdin, stdout, stderr, factory, "rotate")
    if choice == "4":
        return _menu_single_user(stdin, stdout, stderr, factory, "reinitialize")
    return None


def _read_choice(stdin, stdout, maximum, *, prompt=None):
    stdout.write(prompt if prompt is not None else "请输入选项 [0-%d]：" % maximum)
    stdout.flush()
    try:
        line = stdin.readline()
    except (EOFError, KeyboardInterrupt):
        raise _MenuExit(0)
    if line == "":
        raise _MenuExit(0)
    return line.strip()


def _menu_error(stderr, code):
    stderr.write(_colors_for(stderr).red(_ERROR_TEMPLATE % code))


def _menu_airport_portal(stdin, stdout, stderr, factory):
    # Menu 1: one full-chain validation before anything is saved; the page
    # URL is typed visibly (never hidden, never a command-line argument)
    # and pasted whitespace is stripped; an empty answer is rejected
    # before any service is constructed.
    stdout.write("此操作会验证订阅开关页面、生成新的订阅链接，\n")
    stdout.write("并在下载成功后更新 AmyTelecom.yaml。\n")
    stdout.write("任一步失败都会保留当前配置。\n\n")
    activation_url = _prompt(stdin, stdout, "请输入订阅开关页面：")
    if activation_url is None:
        return 0, False
    if not activation_url:
        return _error(stderr, "invalid_activation_url", 2), False
    return _call("airport-portal", activation_url, stdout, stderr, factory), False


def _menu_airport_manual_refresh(stdin, stdout, stderr, factory):
    # Menu 3: the user enabled the subscription in a browser; the saved
    # link is re-downloaded verbatim without visiting or naming the page.
    stdout.write("请在浏览器中手动打开机场订阅开关页面。\n")
    answer = _prompt(stdin, stdout, "确认订阅已开启后，按回车继续；输入 0 取消：")
    if answer is None or answer == "0":
        return 0, False
    return _call("airport-refresh", None, stdout, stderr, factory), False


def _menu_airport_source(stdin, stdout, stderr, factory):
    # Menu 4: the permanent manual fallback — it never touches the portal
    # page and only swaps the saved link after a successful download.
    stdout.write("此操作不会访问订阅开关页面。\n")
    stdout.write("下载成功后会保存新订阅链接，并更新 AmyTelecom.yaml。\n\n")
    airport_url = _prompt(stdin, stdout, "请输入新的机场订阅链接：")
    if airport_url is None:
        return 0, False
    if not airport_url:
        return _error(stderr, "invalid_airport_url", 2), False
    return _call("airport-replace", airport_url, stdout, stderr, factory), False


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


def _menu_history_view(stdin, stdout, stderr, factory):
    user = _menu_user_id(stdin, stdout, stderr, factory)
    if user is None:
        return 0, False
    try:
        history = factory().history(user)
    except Exception:
        return _error(stderr, "service_unavailable", 1), False
    _write_history(stdout, user, history)
    return 0, False


def _menu_rollback(stdin, stdout, stderr, factory):
    user = _menu_user_id(stdin, stdout, stderr, factory)
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
    user = _menu_user_id(stdin, stdout, stderr, factory)
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


def _menu_user_id(stdin, stdout, stderr, factory):
    # The operator should never have to guess which numeric ids exist:
    # list the live users (with the owner marked) before reading the id,
    # and treat 0 / empty / EOF as cancelling back to the submenu.
    try:
        status = factory().status()
    except Exception:
        _menu_error(stderr, "service_unavailable")
        return None
    users = sorted(
        (user for user in status["users"] if user.get("active")),
        key=lambda user: user["client_id"],
    )
    if not users:
        stdout.write("当前没有可用用户。\n")
        return None
    stdout.write("当前用户：\n")
    for user in users:
        owner = "（owner）" if user["client_id"] == status["owner_client_id"] else ""
        stdout.write("  ID %d  %s%s\n" % (user["client_id"], user["email"], owner))
    raw = _prompt(stdin, stdout, "\n请输入用户 ID（0 返回）：")
    if not raw or raw == "0":
        return None
    user = _user_id(raw)
    if user is None:
        _menu_error(stderr, "invalid_menu_selection")
        return None
    if user not in {item["client_id"] for item in users}:
        _menu_error(stderr, "invalid_client")
        return None
    return user


def _prompt(stdin, stdout, message):
    stdout.write(message)
    stdout.flush()
    try:
        line = stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return None
    return line.strip() if line else None


def _menu_user_links(stdin, stdout, stderr, factory):
    user = _menu_user_id(stdin,stdout,stderr,factory)
    if user is None:
        return 0,False
    try:
        service = factory()
        item = next((u for u in service.links() if u['client_id']==user),None)
        if item is None:
            stdout.write('当前用户暂无已发布订阅链接。\n')
            return 0,False
        stdout.write('\n用户：%s（ID %d）\n' % (item['email'],user))
        config = getattr(service,'config',None)
        if config is not None:
            from clash_sub.web_settings import read_settings
            settings = read_settings(config.private_root)
            if settings['enabled']:
                from clash_sub.web_manage import execute
                stdout.write(execute(default_repo_root(),'links',target=str(user))+'\n')
            else:
                stdout.write('个人网页：尚未初始化\n')
        for url in item['urls']:
            stdout.write('%s：%s\n' % (url.rsplit('/',1)[-1],url))
        return 0,False
    except Exception:
        return _error(stderr,'service_unavailable',1),False


def _confirm(stdin, stdout, message):
    answer = _prompt(stdin, stdout, message)
    return answer is not None and answer.lower() == "y"


def _web_command(action, target, stdout, stderr, stdin=None):
    from clash_sub.web_manage import execute
    import getpass
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    if action in {"set-bwh-api", "bwh-status", "delete-bwh-api"} and target is not None:
        return _error(stderr, "invalid_arguments", 2)
    try:
        password = None
        username = None
        if action == "set-bwh-api":
            veid = _prompt(stdin or sys.stdin, stdout, "输入搬瓦工 VEID（回车取消）：")
            if not veid:
                return 0
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                api_key = getpass.getpass("输入搬瓦工 API Key（隐藏输入，回车取消）：")
            if not api_key:
                return 0
            stdout.write(execute(default_repo_root(), action, bwh_veid=veid, bwh_api_key=api_key) + "\n")
            return 0
        if action == "delete-bwh-api":
            if _prompt(stdin or sys.stdin, stdout, "仅删除搬瓦工凭据，保留 3x-ui 令牌。输入 DELETE 确认：") != "DELETE":
                return 0
        if action == "set-api-token":
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("输入 3x-ui API 令牌（隐藏输入，回车取消）：")
            if not token:
                return 0
            stdout.write(execute(default_repo_root(), action, api_token=token) + "\n")
            return 0
        if action in {"setup", "reset-password"}:
            username = _prompt(stdin or sys.stdin, stdout, "设置网页管理员账号（回车保留现有账号，新安装默认 owner 标识）：")
            if username is None:
                return 0
            username = username or None
            password = getpass.getpass("设置管理员密码（8—256字符）：")
            if password != getpass.getpass("再次输入密码："):
                return _error(stderr, "password_mismatch", 1)
        stdout.write(execute(default_repo_root(), action, password=password, username=username, target=target) + "\n")
        return 0
    except (EOFError, KeyboardInterrupt):
        return 0
    except Exception:
        return _error(stderr, "web_management_failed", 1)


def _web_dispatch(choice, stdin, stdout, stderr, factory):
    if choice == "9":
        return _run_submenu(_BWH_MENU_ROWS, 3, _bwh_dispatch, stdin, stdout, stderr, factory, _colors_for(stdout), name="搬瓦工 API 设置")
    actions = {"1": "setup", "2": "reset-password", "3": "web-on", "4": "web-off", "5": "panel-on", "6": "panel-off", "7": "tunnel", "8": "set-api-token"}
    if choice not in actions:
        return None
    return _web_command(actions[choice], None, stdout, stderr, stdin=stdin), False


def _bwh_dispatch(choice, stdin, stdout, stderr, factory):
    action = {"1": "set-bwh-api", "2": "bwh-status", "3": "delete-bwh-api"}.get(choice)
    if action is None:
        return None
    return _web_command(action, None, stdout, stderr, stdin=stdin), False


def _parser():
    parser = _CommandParser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", add_help=False)
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
    # Internal systemd entry (clash-sub-metadata.service); never in menus.
    commands.add_parser("metadata-serve", add_help=False)
    commands.add_parser("web-serve", add_help=False)
    task = commands.add_parser('web-task', add_help=False)
    task.add_argument('task_id')
    task.add_argument('--sync-only', action='store_true')
    web = commands.add_parser("web", add_help=False)
    web.add_argument("action", choices=("setup", "status", "links", "rotate-page", "reset-password", "panel-on", "panel-off", "tunnel", "set-api-token", "web-on", "web-off", "set-bwh-api", "bwh-status", "delete-bwh-api"))
    web.add_argument("target", nargs="?")
    # Internal systemd entry (clash-sub-airport-refresh.timer); never displayed.
    commands.add_parser("airport-scheduled-refresh", add_help=False)
    commands.add_parser("install", add_help=False)
    commands.add_parser("backup", add_help=False)
    template = commands.add_parser("template-sync", add_help=False)
    template.add_argument("--compat", type=Path)
    template.add_argument("--balance", type=Path)
    commands.add_parser("mihomo-update", add_help=False)
    update = commands.add_parser("update", add_help=False)
    update.add_argument("--post-update", action="store_true")
    cert = commands.add_parser("cert", add_help=False)
    cert.add_argument("--renew", action="store_true")
    cert.add_argument("--domain")
    return parser


def _run_command(parsed, stdout, stderr, factory):
    command = parsed.command
    if command == 'web-task':
        from clash_sub.web_tasks import run
        if os.geteuid() != 0:
            return _error(stderr,'not_root',1)
        try:
            root = default_repo_root()
            run(load_config(config_path(root),root),root,parsed.task_id,sync_only=parsed.sync_only)
            return 0
        except Exception:
            return _error(stderr,'web_task_failed',1)
    if command == "web":
        return _web_command(parsed.action, parsed.target, stdout, stderr)
    if command == "web-serve":
        from clash_sub.web_server import serve
        try:
            root = default_repo_root()
            serve(load_config(config_path(root), root), metadata_server.listener_from_environment())
            return 0
        except Exception:
            return _error(stderr, "web_serve_failed", 1)
    if command == "metadata-serve":
        return _metadata_serve(stderr)
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
        return _template_sync(parsed, stdout, stderr)
    if command == "mihomo-update":
        return _mihomo_update(stdout, stderr)
    if command == "cert":
        return _cert_command(parsed, stdout, stderr)
    if command == "sync":
        return _call("sync", None, stdout, stderr, factory)
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
    if command == "airport-scheduled-refresh":
        return _scheduled_refresh(stdout, stderr, factory)
    return _error(stderr, "invalid_command", 2)


def _scheduled_refresh(stdout, stderr, factory):
    # Daily subscription timer entry: a normal skip (or a busy lock / missing activation
    # page) is success for the unit; only real failures fail the run.
    backup_failed=False
    try:
        service=factory()
        refresh_backup=getattr(service,'refresh_backup_if_due',None)
        if refresh_backup is not None:
            try:refresh_backup()
            except ServiceError as error:
                _error(stderr,error.code,1)
                backup_failed=True
        result = service.auto_refresh_airport()
    except ServiceError as error:
        if error.code in {"operation_busy", "airport_activation_missing", "airport_refresh_skipped"}:
            return int(backup_failed)
        return _error(stderr, error.code, 1)
    except Exception:
        return _error(stderr, "service_unavailable", 1)
    if result.get("skipped"):
        return int(backup_failed)
    _write_airport_update(stdout, result)
    return int(backup_failed)


def _call(operation, value, stdout, stderr, factory):
    try:
        service = factory()
        if operation.startswith('backup-'):
            if operation=='backup-replace':result=service.replace_backup_source(value)
            elif operation=='backup-refresh':result=service.refresh_backup()
            elif operation=='backup-remove':result=service.remove_backup_source()
            elif operation=='backup-status':result=service.backup_status()
            elif operation=='backup-import':result=service.import_backup_nodes(value)
            elif operation=='backup-remove-manual':result=service.remove_manual_backup_nodes()
            else:return _error(stderr,'invalid_command',2)
            _write_backup_status(stdout,result)
        elif operation == "airport-replace":
            _write_airport_update(stdout, service.replace_airport_source(value))
        elif operation == "airport-portal":
            _write_airport_update(stdout, service.configure_airport_portal(value))
        elif operation == "airport-auto-refresh":
            try:
                result = service.auto_refresh_airport()
            except ServiceError as error:
                if error.code != "airport_activation_missing":
                    raise
                stderr.write("尚未设置订阅开关页面，请先使用菜单 1。\n")
                return 0
            if result.get("skipped"):
                stdout.write("本次已跳过，当前机场配置保持不变。\n")
                return 0
            _write_airport_update(stdout, result)
        elif operation == "airport-refresh":
            try:
                result = service.refresh_airport()
            except ServiceError as error:
                if error.code != "airport_source_missing":
                    raise
                stderr.write("未保存机场订阅链接（错误代码：airport_source_missing）\n")
                return 1
            _write_airport_update(stdout, result)
        elif operation == "airport-status":
            _write_airport_status(stdout, service.airport_status())
        elif operation == "sync":
            result = service.sync_all()
            errors = tuple(result["errors"])
            if errors:
                stdout.write("同步部分完成。\n")
                for error in errors:
                    stderr.write("客户端 ID %s（错误代码：%s）\n" % (error["client_id"], error["code"]))
                    if error["code"] in {"member_update_failed", "owner_update_failed"}:
                        stderr.write("该用户配置同步失败，可能发生于节点读取、配置生成、校验或发布阶段；此错误码不能确定是 Flow 问题。本次未替换该用户配置。\n")
                    elif error["code"] == "node_subscription_export_failed":
                        stderr.write("纯节点订阅导出失败：存在不支持或无效的节点参数。本次未替换该用户配置，请检查纯节点导出兼容性，无需据此修改 Flow。\n")
                return 1
            stdout.write("同步已完成。\n")
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
        elif operation == "history":
            _write_history(stdout, value, service.history(value))
        elif operation == "rollback":
            user, release = value
            service.rollback(user, release)
            stdout.write("已回滚用户 %s 到版本 %s。\n" % (user, release))
        elif operation == "rotate":
            result = service.rotate_link(value)
            stdout.write("订阅链接已轮换：\n")
            _write_urls(stdout, result["urls"])
        elif operation == "reinitialize":
            service.reinitialize_owner(value)
            stdout.write("所有者已重新初始化；请执行 sync。\n")
        else:
            return _error(stderr, "invalid_command", 2)
    except ServiceError as error:
        return _error(stderr, error.code, 1)
    except Exception:
        return _error(stderr, "service_unavailable", 1)
    return 0


def _write_links(stdout, links):
    for user in links:
        stdout.write("%s [ID %s]\n" % (user["email"], user["client_id"]))
        if user.get("sync_required"):
            stdout.write("待同步：以下是已发布的旧链接，请执行 clash-sub sync 后再分发新链接。\n")
        _write_urls(stdout, user["urls"])


def _write_urls(stdout, urls):
    for url in urls:
        label = "机场镜像链接：" if url.endswith("/" + AIRPORT_FILENAME) else ""
        stdout.write("%s%s\n" % (label, url))


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


def _write_airport_update(stdout, result):
    if result["traffic_captured"]:
        stdout.write("机场订阅已更新。\n")
    else:
        stdout.write("机场订阅已更新，未获取到流量信息。\n")


def _write_airport_status(stdout, status):
    # Only the public summary: the two configured flags with their hostnames,
    # aggregate traffic, expiry, last success, and provider presence — never
    # a URL path, query, sid, or token.
    stdout.write("机场状态：\n")
    stdout.write("开关页面已配置：%s\n" % ("是" if status.get("activation_configured") else "否"))
    stdout.write("开关页面域名：%s\n" % (status.get("activation_host") or "无"))
    stdout.write("订阅链接已配置：%s\n" % ("是" if status.get("source_configured") else "否"))
    stdout.write("来源域名：%s\n" % (status.get("source_host") or "无"))
    stdout.write("总量：%s\n" % _traffic_number(status["traffic_total"]))
    stdout.write("已用：%s\n" % _traffic_number(status["traffic_used"]))
    stdout.write("剩余：%s\n" % _traffic_number(status["traffic_remaining"]))
    stdout.write("到期时间：%s\n" % _format_expiry(status["traffic_expiry_ms"]))
    stdout.write("最近成功时间：%s\n" % _format_timestamp(status.get("last_success")))
    stdout.write("AmyTelecom.yaml：%s\n" % ("存在" if status["provider_present"] else "不存在"))


def _traffic_number(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "未获取"
    amount = float(value)
    unit = "B"
    for step in ("KiB", "MiB", "GiB", "TiB"):
        if amount < 1024:
            break
        amount /= 1024
        unit = step
    if unit == "B":
        return "%d B" % value
    return "%.2f %s" % (amount, unit)


def _format_expiry(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "未获取"
    if value <= 0:
        return "未设置"
    # traffic_expiry_ms is legacy-named: the unified contract carries the
    # Subscription-Userinfo expire value in Unix SECONDS.
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


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
    if code.startswith('backup_node_line_') and code.endswith('_invalid_or_unsupported'):
        number=code[len('backup_node_line_'):-len('_invalid_or_unsupported')]
        if number.isdigit():
            stderr.write('第 %s 行节点链接格式错误或含不支持的参数，未替换原节点。\n' % number)
            return status
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
        stdout.write("请输入 Cloudflare API Token：\n")
        token = input().strip()
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
        progress_offset = int(os.environ.get("CLASH_SUB_PROGRESS_OFFSET", "0"))
    except ValueError:
        progress_offset = 0
    if progress_offset < 0:
        progress_offset = 0
    if progress_offset == 0:
        stdout.write("\nclash-sub 安装程序\n")
    try:
        installer = Installer(
            default_repo_root(),
            print_fn=lambda message: stdout.write("%s\n" % message),
            progress_offset=progress_offset,
        )
        report = installer.install(
            domain=domain,
            cf_token=token,
            swap_mb=swap_mb,
            owner_email=owner,
            node_host=node_host,
        )
    except InstallerError as error:
        stderr.write("修正问题后重新执行：bash install.sh\n")
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


def _metadata_serve(stderr, *, listener_factory=None, build_store=None, run_server=None):
    # Internal systemd entry: the listener is the descriptor systemd passed,
    # and the store is the traffic metadata cache alone — never the full
    # ClashSubService (no nginx, mihomo, or YAML dependencies).
    listener_factory = listener_factory or metadata_server.listener_from_environment
    build_store = build_store or _default_metadata_store
    run_server = run_server or metadata_server.serve
    try:
        listener = listener_factory()
        store = build_store()
        run_server(store, listener)
    except Exception:
        return _error(stderr, "metadata_serve_failed", 1)
    return 0


def _default_metadata_store():
    root = default_repo_root()
    return TrafficMetadataStore(load_config(config_path(root), root))


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


def _template_sync(parsed, stdout, stderr):
    try:
        report = template_sync.run_template_sync(
            default_repo_root(), parsed.compat, parsed.balance
        )
    except template_sync.TemplateSyncError as error:
        return _error(stderr, error.code, 1)
    except Exception:
        return _error(stderr, "template_sync_failed", 1)
    for line in report.lines:
        stdout.write("%s\n" % line)
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


def _write_backup_status(stdout, result):
    labels={None:'无','backup_download_failed':'下载失败，保留上次成功缓存','backup_source_invalid':'备用配置不可用'}
    stdout.write('备用线路：%s\n' % ('已配置' if result['configured'] else '未配置'))
    stdout.write('合并节点：%d 个\n' % len(result['nodes']))
    stdout.write('订阅来源：%d 个；手动导入：%d 个\n' % (len(result.get('subscription_nodes',result['nodes'])),len(result.get('manual_nodes',[]))))
    for name in result['nodes']:stdout.write('  %s\n' % name)
    stamp=result.get('last_success')
    stdout.write('最近成功刷新：%s\n' % (datetime.fromtimestamp(stamp,timezone.utc).astimezone().isoformat() if stamp else '暂无'))
    updated=result.get('manual_updated')
    stdout.write('手动节点修改时间：%s\n' % (datetime.fromtimestamp(updated,timezone.utc).astimezone().isoformat() if updated else '暂无'))
    stdout.write('最近错误：%s\n' % labels.get(result.get('last_error'),'状态不可用'))
    stdout.write('仅标准订阅包含备用；42vio、openwrt 除外。客户端需更新订阅。\n')
