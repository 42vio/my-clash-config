"""Root-only portal setup and reversible public-panel controls."""

import copy
import subprocess
from pathlib import Path

from clash_sub.config import load_config
from clash_sub.installer import InstallPaths
from clash_sub.manage import _load_install_state, _rerender_nginx
from clash_sub.runtime import config_path
from clash_sub.service import _OperationLock
from clash_sub.state import load_state
from clash_sub.web_settings import page_path, password_hash, provision_pages, read_settings, write_settings


def execute(root, action, *, password=None, target=None, runner=subprocess.run, actor=None, authorize=None):
    config = load_config(config_path(root), root)
    with _OperationLock(Path(config.private_root) / "operation.lock"):
        settings = read_settings(config.private_root)
        previous = copy.deepcopy(settings)
        state = load_state(Path(config.private_root) / "state.json")
        if authorize is not None:
            authorize()
        if actor is not None:
            import secrets
            from clash_sub.web_tasks import authorization
            if not secrets.compare_digest(actor,authorization(settings,state)):
                raise ValueError('owner_authorization_invalid')
        if action == "status":
            return "面板公网访问：%s；订阅网页：%s" % ("开启" if settings['panel_public'] else "关闭", "开启" if settings['enabled'] else "未初始化")
        if action == "tunnel":
            installed = _load_install_state(root)
            return "在本机执行（请替换 SSH 别名或地址）：\nssh -N -L 127.0.0.1:14631:127.0.0.1:%d <SSH别名>\n浏览器打开：http://127.0.0.1:14631%s/" % (installed.panel_port, installed.panel_base_path)
        if action in {"setup", "links", "rotate-page", "reset-password"}:
            if state is None:
                raise ValueError("subscription_state_missing")
            if action == "rotate-page" and target not in {str(k) for k in state.users} | {"airport", "admin"}:
                raise ValueError("invalid_page_target")
            provision_pages(settings, state, rotate=target if action == "rotate-page" else None)
        if action in {"setup", "reset-password"}:
            settings['password_hash'] = password_hash(password)
        if action == "setup":
            if not all(Path('/etc/systemd/system/' + name).is_file() for name in ('clash-sub-web.socket', 'clash-sub-online.socket')):
                raise ValueError("run_clash_sub_update_first")
            settings['enabled'] = True
            settings['panel_public'] = False
            result = runner([str(config.systemctl_binary), 'enable', '--now', 'clash-sub-web.socket', 'clash-sub-online.socket'], capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise ValueError("web_socket_enable_failed")
        if action in {"panel-on", "panel-off"}:
            settings['panel_public'] = action == "panel-on"
        write_settings(config.private_root, settings)
        if action in {"setup", "panel-on", "panel-off"}:
            try:
                paths = InstallPaths(xui_database=config.xui_database, private_root=config.private_root, public_root=config.public_root, routes_conf=config.nginx_routes, mihomo_binary=config.mihomo_binary)
                _rerender_nginx(root, runner, _load_install_state(root), paths=paths, config=config)
            except Exception:
                write_settings(config.private_root, previous)
                raise
        if action in {"setup", "links", "rotate-page", "reset-password"}:
            base = 'https://' + config.subscription_authority
            from clash_sub.web_settings import admin_prefix
            lines = ['管理员：' + base + admin_prefix(settings,state) + '/'] if target is None or target in {'admin',str(state.owner_client_id)} else []
            for key, record in sorted(settings['pages'].items()):
                if action == 'rotate-page' and key != target:
                    continue
                if action == 'links' and target is not None and key != target and not (key=='airport' and target==str(state.owner_client_id)):
                    continue
                email = state.users[state.owner_client_id if key == 'airport' else int(key)].email
                name = email + '（机场专用）' if key == 'airport' else email
                lines.append('%s：%s%s' % (name, base, page_path(record['token'], email)))
            return '\n'.join(lines)
        return '面板公网访问已%s；代理与订阅服务继续运行。' % ('开启' if settings['panel_public'] else '关闭')
