"""Root-only portal setup and reversible public-panel controls."""

import copy
import subprocess
from pathlib import Path

from clash_sub.config import load_config
from clash_sub.installer import InstallPaths
from clash_sub.manage import _load_install_state, _rerender_nginx, _stop_web_services
from clash_sub.runtime import config_path
from clash_sub.service import _OperationLock
from clash_sub.state import load_state
from clash_sub.web_settings import admin_username, validate_username, admin_prefix, page_path, password_hash, provision_pages, read_settings, write_settings


def _access_status(settings):
    webpage = '● 已开启' if settings['enabled'] else '○ 已关闭' if settings.get('password_hash') else '? 未初始化'
    panel = '● 已开启' if settings['panel_public'] else '○ 已关闭'
    return '订阅页面 %s    3x-ui 面板 %s' % (webpage, panel)


def access_status(root):
    """Read configured access switches without changing settings or services."""
    config = load_config(config_path(root), root)
    return _access_status(read_settings(config.private_root))


def execute(root, action, *, password=None, username=None, api_token=None, bwh_veid=None, bwh_api_key=None, target=None, runner=subprocess.run, actor=None, authorize=None):
    config = load_config(config_path(root), root)
    with _OperationLock(Path(config.private_root) / "operation.lock"):
        if action in {'set-bwh-api', 'bwh-status', 'delete-bwh-api'}:
            from clash_sub.bandwagon import load_credentials, save_api, delete_api, query_usage, format_usage
            if action == 'set-bwh-api':
                usage = query_usage(bwh_veid, bwh_api_key)
                summary = format_usage(usage)
                save_api(bwh_veid, bwh_api_key)
                return '搬瓦工 API 已保存并验证成功。' + summary
            if action == 'delete-bwh-api':
                delete_api()
                return '搬瓦工 API 配置已删除，3x-ui 令牌和代理服务不受影响。'
            credentials = load_credentials()
            if credentials is None:
                return '搬瓦工 API 尚未配置，请先添加 VEID 和 API Key。'
            return '搬瓦工 API 验证成功。' + format_usage(query_usage(*credentials))
        if action == 'set-api-token':
            from clash_sub.online import TOKEN_FILE, save_token, query_online
            save_token(api_token)
            verified = query_online(config.xui_database, credentials=TOKEN_FILE) is not None
            return '令牌已保存，%s；网页会在下一次查询时读取新令牌，无需重启服务。' % ('在线查询验证成功' if verified else '在线查询未成功，请检查令牌权限与 3x-ui 状态')
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
            return _access_status(settings)
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
            settings['username'] = validate_username(username if username is not None else admin_username(settings))
            settings['password_hash'] = password_hash(password)
        if action == "web-on" and (not state or not settings['password_hash'] or not admin_prefix(settings,state)):
            raise ValueError("initialize_web_first")
        if action in {"setup", "web-on"}:
            if not all(Path('/etc/systemd/system/' + name).is_file() for name in ('clash-sub-web.socket',)):
                raise ValueError("run_clash_sub_update_first")
            settings['enabled'] = True
            if action == 'setup':
                settings['panel_public'] = False
            result = runner([str(config.systemctl_binary), 'enable', '--now', 'clash-sub-web.socket'], capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise ValueError("web_socket_enable_failed")
        if action == "web-off":
            settings["enabled"] = False
        if action in {"panel-on", "panel-off"}:
            settings['panel_public'] = action == "panel-on"
        write_settings(config.private_root, settings)
        if action in {"setup", "panel-on", "panel-off", "web-on", "web-off"}:
            try:
                paths = InstallPaths(xui_database=config.xui_database, private_root=config.private_root, public_root=config.public_root, routes_conf=config.nginx_routes, mihomo_binary=config.mihomo_binary)
                _rerender_nginx(root, runner, _load_install_state(root), paths=paths, config=config)
            except Exception:
                write_settings(config.private_root, previous)
                if action in {'setup','web-on'} and not previous['enabled']:
                    _stop_web_services(config, runner)
                raise
        if action == 'web-off':
            if not _stop_web_services(config, runner):
                return '订阅页面已关闭，但网页服务或 socket 未完全停止，内存释放未完成；请检查服务后重试关闭。'
            return '订阅页面已关闭，网页服务及 socket 已停止以释放内存；订阅下载、代理和 3x-ui 不受影响。'
        if action in {"setup", "links", "rotate-page", "reset-password"}:
            base = 'https://' + config.subscription_authority
            lines = ['管理员：' + base + admin_prefix(settings,state) + '/'] if target is None or target in {'admin',str(state.owner_client_id)} else []
            for key, record in sorted(settings['pages'].items()):
                if action == 'rotate-page' and key != target:
                    continue
                if action == 'links' and target is not None and key != target and not (key=='airport' and target==str(state.owner_client_id)):
                    continue
                email = state.users[state.owner_client_id if key == 'airport' else int(key)].email
                name = email + '（机场专用）' if key == 'airport' else email
                lines.append('%s：%s%s' % (name, base, page_path(record['token'], email)))
            if action in {'setup','reset-password'}:
                lines.insert(0,'网页管理员账号：'+admin_username(settings))
            return '\n'.join(lines)
        if action in {'web-on','web-off'}:
            return '订阅页面已%s；个人页、机场页和管理员页统一切换，订阅下载、代理和 3x-ui 面板开关保持不变。' % ('开启' if settings['enabled'] else '关闭')
        return '3x-ui 面板公网访问已%s；代理与订阅服务继续运行。' % ('开启' if settings['panel_public'] else '关闭')
