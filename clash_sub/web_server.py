"""Read-only account portal. No subscription mutation or remote command execution."""

import hashlib
import html
import http.cookies
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from clash_sub.domain import PROFILE_FILENAMES, Traffic
from clash_sub.metadata import TrafficMetadataStore
from clash_sub.state import airport_only_token, load_state
from clash_sub.web_settings import admin_username, admin_prefix, page_path, read_settings, verify_password
from clash_sub.xui import read_xui_snapshot, read_inbound_traffic, read_traffic_resets
from clash_sub.online import OnlineCache, query_online


# Confirmed by the owner; subscription headers do not expose the reset day.
AMY_RESET_RULE = "每月 19 日"


def esc(value):
    return html.escape(str(value), quote=True)


def size(value):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "%.1f %s" % (value, unit)
        value /= 1024


def usage_card(title, traffic, note="", *, compact=False, reset="待确认", unlimited_label="不限额"):
    tag, kind, heading = ('div', 'traffic-part', 'h3') if compact else ('section', 'card usage-card', 'h2')
    reset_label = reset + '重置' if reset.startswith('每') else '重置时间待确认' if reset == '待确认' else reset
    header = '<div class="card-head"><%s>%s</%s><span class="subtle reset-hint" title="%s">%s</span></div>' % (heading, esc(title), heading, esc(note), esc(reset_label))
    if traffic is None:
        return '<%s class="%s">%s<p>暂无数据</p></%s>' % (tag, kind, header, tag)
    used = traffic.upload + traffic.download
    total = size(traffic.total) if traffic.total else unlimited_label
    if traffic.total:
        ratio = min(100, used / traffic.total * 100)
        bar = '<div class="bar" role="progressbar" aria-label="流量使用比例" aria-valuemin="0" aria-valuemax="100" aria-valuenow="%.1f"><i style="width:%.2f%%"></i></div>' % (ratio, ratio)
        remaining = '<span class="remaining">剩余 %s</span>' % size(max(0,traffic.total-used))
    else:
        bar = ''
        remaining = '<span class="remaining">%s</span>' % esc(unlimited_label)
    caption = '<span class="transfer"><span class="upload" title="上传流量">↑ %s</span><span class="download" title="下载流量">↓ %s</span></span>%s' % (size(traffic.upload),size(traffic.download),remaining)
    return '<%s class="%s">%s<div class="usage usage-linear"><div class="number">%s <small>/ %s</small></div>%s<div class="usage-caption">%s</div></div></%s>' % (tag, kind, header, size(used), esc(total), bar, caption, tag)


class Portal:
    def __init__(self, config, *, clock=time.time, store=None, snapshot_reader=read_xui_snapshot, online_reader=None):
        self.config = config
        self.clock = clock
        self.store = store or TrafficMetadataStore(config)
        self.snapshot_reader = snapshot_reader
        self.sessions = {}
        self.failures = deque()
        self.lock = threading.Lock()
        self.root = Path(config.template_root).parent
        self._snapshot = None
        self._snapshot_time = 0
        self._snapshot_lock = threading.Lock()
        self._net_previous = None
        self._inbound = None
        self._inbound_time = 0
        self.online = OnlineCache(online_reader if online_reader is not None else lambda:query_online(config.xui_database))
        self._resets = None
        self._resets_time = 0

    def online_states(self, state):
        names = self.online.get()
        return {str(client_id): 'unknown' if names is None else 'online' if user.email in names else 'offline' for client_id, user in state.users.items()}

    @staticmethod
    def presence(status):
        labels = {'online':'在线','offline':'离线','unknown':'未知'}
        return '<span class="presence %s" title="来源：3x-ui 在线查询">%s%s</span>' % (status, '<span aria-hidden="true">—</span>' if status == 'unknown' else '<i aria-hidden="true"></i>', labels[status])

    def inbound_card(self):
        with self._snapshot_lock:
            if self._inbound is None or self.clock() - self._inbound_time >= 300:
                self._inbound = read_inbound_traffic(self.config.xui_database)
                self._inbound_time = self.clock()
            traffic = self._inbound
        if traffic is None:
            return '<h3>自建流量 · 3x-ui 入站</h3><p>暂无数据 · 入站统计读取失败</p>'
        return usage_card('自建流量 · 3x-ui 入站', Traffic(traffic['upload'], traffic['download'], traffic['total'] or 0, 0), '入站当前累计', compact=True, reset=self.reset_rule(), unlimited_label='入站总额度未设置')

    def refresh_overview_data(self):
        """Refresh local display data without fetching upstream subscriptions."""
        with self._snapshot_lock:
            self._snapshot = None
            self._inbound_time = 0
            self._inbound = None
            self._resets = None
        self.store.refresh()

    def reset_rule(self, client_id=None):
        with self._snapshot_lock:
            if self._resets is None or self.clock() - self._resets_time >= 300:
                self._resets = read_traffic_resets(self.config.xui_database)
                self._resets_time = self.clock()
            return self._resets['inbound'] if client_id is None else self._resets['clients'].get(client_id, '待确认')

    def snapshot(self):
        with self._snapshot_lock:
            if self._snapshot is None or self.clock() - self._snapshot_time >= 300:
                self._snapshot = self.snapshot_reader(self.config.xui_database)
                self._snapshot_time = self.clock()
            return self._snapshot

    def session_record(self, cookie, settings, state=None):
        try:
            parsed = http.cookies.SimpleCookie(cookie or "")
            value = parsed["portal_session"].value
        except (KeyError, http.cookies.CookieError):
            return None
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self.lock:
            record = self.sessions.get(digest)
        prefix = admin_prefix(settings,state)
        if record and prefix and record[0] > self.clock() and secrets.compare_digest(record[1], settings.get("password_hash", "")) and record[2] == settings['admin']['binding'] and record[4] == settings['admin']['token'] and len(record) == 6 and record[5] == admin_username(settings):
            return record
        return None

    def session(self, cookie, settings, state=None):
        return self.session_record(cookie,settings,state) is not None

    def login(self, password, settings, *, username=None):
        with self.lock:
            now = self.clock()
            while self.failures and self.failures[0] <= now - 300:
                self.failures.popleft()
            if len(self.failures) >= 5:
                return None
            password_ok = verify_password(password, settings.get("password_hash", ""))
            username_ok = secrets.compare_digest((username or "").encode(),admin_username(settings).encode())
            if not password_ok or not username_ok:
                self.failures.append(now)
                return None
            self.sessions = {key: value for key, value in self.sessions.items() if value[0] > now}
            if len(self.sessions) >= 32:
                self.sessions.clear()
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (now + 8 * 3600, settings["password_hash"], settings["admin"]["binding"], secrets.token_urlsafe(32), settings["admin"]["token"], admin_username(settings))
            return token

    def resolve_page(self, token, settings, state):
        for key, record in settings["pages"].items():
            if not secrets.compare_digest(token, record.get("token", "")):
                continue
            user = state.users.get(state.owner_client_id if key == "airport" else int(key))
            if user and user.active and secrets.compare_digest(hashlib.sha256(user.token.encode()).hexdigest(), record.get("binding", "")):
                return key
        return None

    def shell(self, title, body, *, admin=False, prefix='', csrf=''):
        top = '<a href="%s/">管理总览</a>' % esc(prefix) if admin else "只读订阅中心"
        logout = '<form method="post" action="%s/logout"><input type="hidden" name="csrf" value="%s"><button class="button">退出登录</button></form>' % (esc(prefix),esc(csrf)) if admin else ''
        metadata = '<meta name="csrf-token" content="%s">' % esc(csrf) if admin else ''
        return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="strict-origin"><title>42 / %s</title>%s<link rel="stylesheet" href="/portal-assets/portal.css"><script src="/portal-assets/portal.js" defer></script></head><body><header><div class="nav"><div class="brand"><div class="logo">42</div> CONNECT</div><div class="subtle">%s</div></div></header><main data-admin-base="%s"><div class="heading"><div><div class="eyebrow">42 CONNECT</div><h1>%s</h1></div>%s</div>%s<footer><span>订阅中心 · 请妥善保管页面与订阅链接</span><span>访问时间 %s 北京时间 · 用量按缓存展示</span></footer></main><div id="toast" role="status" hidden></div></body></html>' % (esc(title),metadata,top,esc(prefix) if admin else '',esc(title),logout,body,datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).strftime('%H:%M'))

    def links(self, state, user, *, airport=False):
        token = airport_only_token(user.token, schema_version=state.schema_version) if airport else user.token
        variants = ["airport"] if airport else ["compat", "balance", "airport", "nodes"] if user.client_id == state.owner_client_id else ["compat", "nodes"]
        titles = {"compat": "标准订阅", "balance": "Balance 订阅", "airport": "机场专用订阅", "nodes": "仅节点订阅"}
        parts = []
        for variant in variants:
            filename = PROFILE_FILENAMES[variant]
            path = Path(self.config.public_root) / "releases" / str(user.client_id) / str(user.current_release) / filename
            if not path.is_file():
                continue
            link_token = airport_only_token(user.token, schema_version=state.schema_version) if variant == "airport" else token
            url = "https://%s/s/%s/%s" % (self.config.subscription_authority, link_token, filename)
            parts.append('''<div class="subscription-block"><div class="subscription-label"><div class="label">%s</div>%s</div><div class="linkbox"><code data-hidden="https://%s/s/••••••••/%s" data-url="%s">https://%s/s/••••••••/%s</code><button class="icon-btn" data-action="reveal" aria-label="显示或隐藏订阅链接" aria-pressed="false">◉</button><button class="icon-btn copy-icon" data-action="copy" title="复制链接" aria-label="复制%s">⧉</button></div></div>''' % (titles[variant], '<button class="import-small" data-action="import">导入 Clash Verge ↗</button>' if variant != "nodes" else "", esc(self.config.subscription_authority), filename, esc(url), esc(self.config.subscription_authority), filename, titles[variant]))
        return ''.join(parts) or '<p>暂无已发布订阅，请联系管理员。</p>'

    def account(self, key, state, *, admin=False, prefix='', csrf=''):
        airport = key == "airport"
        user = state.users[state.owner_client_id if airport else int(key)]
        client = next((c for c in self.snapshot().clients if c.client_id == user.client_id), None)
        if client is None:
            raise ValueError("account_unavailable")
        traffic = self.store.airport_traffic() if airport else self.store.traffic_for(user.client_id)
        links = self.links(state, user, airport=airport) if user.active and client.enabled and user.current_release else '<p>当前账户未启用，暂无可用订阅。</p>'
        note = self.freshness_note("airport" if airport else "users")
        body = '<div class="grid">' + usage_card("Amy 用量" if airport else "个人用量", traffic, note, reset=AMY_RESET_RULE if airport else self.reset_rule(user.client_id)) + '<section class="card"><div class="card-head"><h2>订阅连接</h2></div>' + links + '''<div class="subscription-help"><details class="help"><summary>如何导入订阅？</summary><p>Clash Verge Rev：在「订阅」中粘贴标准订阅链接。Clash Mi：添加远程配置并粘贴链接。</p><p>仅节点订阅供不支持 Clash / Mihomo 配置、但支持通用节点订阅的客户端使用；不包含机场节点、DNS、策略组和分流规则。</p></details><a class="client-shortcut" href="/clients/">下载客户端 ↗</a></div><p class="privacy">订阅链接包含访问凭据，请勿转发。</p></section></div>'''
        return self.shell("机场订阅" if airport else user.email + " · 我的订阅", body, admin=admin, prefix=prefix, csrf=csrf)

    def freshness_note(self, kind):
        times = self.store.freshness() if hasattr(self.store, 'freshness') else {}
        stamp = times.get(kind)
        if not stamp:
            return "更新时间未知"
        label = datetime.fromtimestamp(stamp, ZoneInfo('Asia/Shanghai')).strftime('%m.%d %H:%M 北京时间')
        if kind == 'users' and self.clock() - stamp > 300:
            return "缓存已过期 · " + label
        return "数据更新于 " + label

    def system_card(self):
        # Only host measurements; unknown values never imply healthy connectivity.
        metrics = {name: {'value': '暂无数据', 'unit': '', 'detail': '暂无数据', 'ratio': None}
                   for name in ('CPU', '内存', '磁盘', '平均网速')}
        uptime = '暂无数据'
        swap = 'Swap 暂无数据'
        values = {}
        network = []
        try:
            values = dict((key.rstrip(':'), int(value.split()[0]) * 1024) for key, value in (line.split(None, 1) for line in Path('/proc/meminfo').read_text().splitlines()))
            used, total = values['MemTotal'] - values['MemAvailable'], values['MemTotal']
            ratio = used / total * 100
            metrics['内存'] = dict(value='%.1f' % ratio, unit='%', detail=size(used)+' / '+size(total), ratio=ratio)
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            pass
        try:
            swap_total = values['SwapTotal']
            swap = ('Swap ' + size(max(0,swap_total-values['SwapFree'])) + ' / ' + size(swap_total)) if swap_total else 'Swap 未启用'
        except KeyError:
            pass
        try:
            disk = shutil.disk_usage(self.config.public_root)
            ratio = disk.used / disk.total * 100
            metrics['磁盘'] = dict(value='%.1f' % ratio, unit='%', detail=size(disk.used)+' / '+size(disk.total), ratio=ratio)
        except (OSError, ZeroDivisionError):
            pass
        try:
            hours = int(float(Path('/proc/uptime').read_text().split()[0]) / 3600)
            uptime = '%d 天 %d 小时' % divmod(hours, 24)
        except (OSError, ValueError, IndexError):
            pass
        try:
            metrics['CPU']['detail'] = '负载 ' + ' / '.join(Path('/proc/loadavg').read_text().split()[:3])
            cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            counters = [line.split(':', 1) for line in Path('/proc/1/net/dev').read_text().splitlines()[2:]]
            counters = [numbers.split() for name, numbers in counters if not re.match(r'^(lo|veth|docker|br-|tun|sit|ipv6net|wg|tailscale)', name.strip())]
            current = (self.clock(), sum(int(v[0]) for v in counters), sum(int(v[8]) for v in counters), sum(cpu), cpu[3] + cpu[4])
            with self.lock:
                previous, self._net_previous = self._net_previous, current
            if previous and current[0] > previous[0]:
                elapsed = current[0] - previous[0]
                upload = size(max(0, current[2]-previous[2])/elapsed) + '/s'
                download = size(max(0, current[1]-previous[1])/elapsed) + '/s'
                metrics['平均网速'] = dict(value=download, unit='↓', detail='↑ '+upload+' · ↓ '+download, ratio=None)
                if current[3] > previous[3]:
                    ratio = max(0, min(100, 100 * (1 - (current[4]-previous[4])/(current[3]-previous[3]))))
                    metrics['CPU'].update(value='%.1f' % ratio, unit='%', ratio=ratio)
            else:
                metrics['平均网速']['detail'] = '等待下一次访问采样'
        except (OSError, ValueError, IndexError):
            pass
        try:
            if any(line.split()[1] == '00000000' for line in Path('/proc/1/net/route').read_text().splitlines()[1:]):
                network.append('IPv4 已配置')
            if any(line.split()[3] == '00' for line in Path('/proc/1/net/if_inet6').read_text().splitlines()):
                network.append('IPv6 已配置')
        except (OSError, IndexError):
            pass
        cards = []
        for name, metric in metrics.items():
            bar = '<div class="bar"><i style="width:%.2f%%"></i></div>' % metric['ratio'] if metric['ratio'] is not None else '<div class="metric-sampling">等待采样</div>' if name == 'CPU' else '<div class="metric-spacer"></div>'
            extra = '<p class="swap-usage">%s</p>' % esc(swap) if name == '内存' else ''
            cards.append('<div class="metric"><label>%s</label><strong>%s <small>%s</small></strong>%s<p>%s</p>%s</div>' % (esc(name), esc(metric['value']), esc(metric['unit']), bar, esc(metric['detail']), extra))
        return '<section class="card server" id="server-overview"><div class="card-head"><div class="server-title"><h2>服务器概况</h2></div><div class="server-tools"><span class="badge"><i class="dot"></i>运行中</span></div></div><div class="metrics">%s</div><div class="server-footer"><span>已连续运行 %s · <span class="refresh-stamp">更新于 %s</span></span><div class="network">%s</div></div><p class="subtle server-sampling-note">进入页面刷新一次，之后手动刷新；CPU 与网速为两次采样间的平均值，网络配置不代表外部连通。</p></section>' % (''.join(cards), esc(uptime), datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).strftime('%H:%M:%S 北京时间'), ''.join('<span><i class="dot"></i>%s</span>' % esc(label) for label in network))

    def admin(self, state, settings, *, csrf=''):
        prefix = admin_prefix(settings,state)
        body = '<div class="admin-overview"><section class="card traffic-card"><div class="card-head"><h2>流量概况</h2></div>' + self.inbound_card() + '<hr class="divider">'
        body += usage_card("Amy 机场", self.store.airport_traffic(), self.freshness_note('airport'), compact=True, reset=AMY_RESET_RULE) + '</section>' + self.maintenance_card() + '</div><section class="card users" id="members"><div class="card-head"><h2>成员用量</h2><span class="subtle">在线来源：3x-ui · 进入页面及手动刷新</span></div><p class="mobile-member-note">重置规则和账号详情可点「详情」查看</p><table class="user-table has-presence"><thead><tr><th>用户</th><th>在线状态</th><th>已用 / 额度</th><th>流量重置时间</th><th>账号状态</th><th></th></tr></thead><tbody>'
        online = self.online_states(state)
        clients = {c.client_id: c for c in self.snapshot().clients}
        for client_id, user in sorted(state.users.items()):
            client = clients.get(client_id)
            if client is None:
                continue
            traffic = self.store.traffic_for(client_id)
            used = size(traffic.upload + traffic.download) if traffic else "暂无数据"
            total = (size(traffic.total) if traffic.total else "不限额") if traffic else "暂无数据"
            status = "有效" if user.active and client.enabled and (client.expiry_ms <= 0 or client.expiry_ms > self.clock()*1000) else "已停用或到期"
            ratio = min(100,(traffic.upload+traffic.download)/traffic.total*100) if traffic and traffic.total else None
            progress = '<div class="mini-bar" role="progressbar" aria-label="%s 流量使用比例" aria-valuemin="0" aria-valuemax="100" aria-valuenow="%.1f"><i style="width:%.2f%%"></i></div>' % (esc(user.email),ratio,ratio) if ratio is not None else ''
            record = settings['pages'].get(str(client_id))
            detail = '<span class="subtle">网页未启用</span>'
            if record and self.resolve_page(record['token'],settings,state) == str(client_id):
                detail = '<a class="detail" target="_blank" rel="noopener noreferrer" href="%s">查看详情 ↗</a>' % esc(page_path(record['token'],user.email))
            body += '<tr%s><td class="user-name">%s</td><td data-online-client="%d">%s</td><td>%s <span class="subtle">/ %s</span>%s</td><td>%s</td><td>%s</td><td><div class="user-actions">%s<button class="manage-link" data-user="%d" data-name="%s">管理链接</button></div></td></tr>' % (' data-expired="true"' if status!='有效' else '',esc(user.email),client_id,self.presence(online[str(client_id)]),used,total,progress,esc(self.reset_rule(client_id)),status,detail,client_id,esc(user.email))

        body += '</tbody></table></section>' + self.task_history() + self.backup_dialog() + self.system_card() + '<button class="floating-refresh" data-action="refresh-overview" aria-label="刷新全部页面数据" title="刷新全部页面数据"><span class="refresh-icon" aria-hidden="true">↻</span><span class="refresh-label">刷新</span></button>' + self.management_dialogs()
        return self.shell(state.users[state.owner_client_id].email + " · 管理总览", body, admin=True,prefix=prefix,csrf=csrf)

    def maintenance_card(self):
        return '<section class="card maintenance"><div class="card-head"><h2>订阅维护</h2><button class="button history-toggle" data-history="open" aria-haspopup="dialog" aria-controls="history-dialog">执行记录</button></div><p class="subtle">更新订阅内容，保留现有链接。</p><div class="maintenance-action"><div><strong>刷新 Amy 订阅</strong><p>获取机场最新节点与流量信息</p></div><button class="button" data-task="refresh-airport">刷新订阅 ↻</button></div><div class="maintenance-action"><div><strong>更新代码并生成配置</strong><p>先更新项目，再生成所有用户配置</p></div><button class="button" data-task="update-sync">更新并生成 ↗</button></div><div class="backup-access"><div><span>备用线路</span><span class="backup-entry-state">%s</span></div><button class="backup-detail-link" data-backup-details>查看状态 ↗</button></div><p class="subtle" id="task-progress" role="status">任务在后台执行，执行期间请勿重复操作。</p></section>' % esc(self.backup_summary()['label'])

    def task_history(self):
        from clash_sub.web_tasks import history
        rows = history(self.config)
        content = ''.join('<div class="history-row"><div><strong>%s</strong><span class="subtle">%s</span></div><span class="badge">%s</span><time>%s</time></div>' % (esc(r['title']),esc(r.get('target_label','')+' · '+r['phase']),esc(r['label']),datetime.fromtimestamp(r['updated_at'],ZoneInfo('Asia/Shanghai')).strftime('%m.%d %H:%M')) for r in rows[:10])
        return '<dialog id="history-dialog" aria-labelledby="history-title"><div class="dialog-head"><h2 id="history-title">执行记录</h2><div class="history-tools"><button class="history-delete" data-clear-history>删除全部</button><button class="close-dialog" data-close="history-dialog" aria-label="关闭执行记录">×</button></div></div><p class="dialog-note">最近十次操作</p><div id="operation-list">%s</div></dialog>' % (content or '<p class="subtle">暂无执行记录</p>')

    @staticmethod
    def management_dialogs():
        return '<dialog id="manage-dialog" aria-labelledby="manage-title"><div class="dialog-head"><h2 id="manage-title">用户链接</h2><button class="close-dialog" data-close="manage-dialog" aria-label="关闭">×</button></div><p class="dialog-note">网页与订阅链接独立管理，请勿公开分享。</p><div id="user-links"></div></dialog><dialog id="confirm-dialog" aria-labelledby="confirm-title"><div class="dialog-head"><h2 id="confirm-title">确认操作</h2><button class="close-dialog" data-close="confirm-dialog" aria-label="取消">×</button></div><p class="confirm-text" id="confirm-text"></p><div class="dialog-actions"><button class="button" data-close="confirm-dialog">取消</button><button class="button primary" id="confirm-action">确认执行</button></div></dialog>'

    def published_composition(self, user):
        if not user.current_release:return None
        path=Path(self.config.public_root)/'releases'/str(user.client_id)/user.current_release/PROFILE_FILENAMES['compat']
        try:
            if path.stat().st_size>5*1024*1024:return None
            import yaml
            document=yaml.safe_load(path.read_bytes())
            providers=document.get('proxy-providers',{}) or {}
            parts=['自建节点']
            if 'AmyTelecom' in providers:parts.append('机场')
            if 'Backup' in providers:parts.append('备用节点')
            return ' + '.join(parts) if len(parts)>1 else '仅自建节点'
        except Exception:return None

    def backup_summary(self):
        from clash_sub.backup_subscription import status, eligible
        result=status(self.config.private_root)
        state=load_state(Path(self.config.private_root)/'state.json')
        result['users']=[user.email for user in state.users.values() if user.active and eligible(user.email) and '备用节点' in (self.published_composition(user) or '')] if state else []
        result['excluded']=['42vio','openwrt']
        result['label']='刷新异常' if result['last_error'] else '已启用' if result['enabled'] and result['nodes'] else '未就绪' if result['configured'] else '未配置'
        return result

    @staticmethod
    def backup_dialog():
        return '<dialog id="backup-dialog" aria-labelledby="backup-title"><div class="dialog-head"><h2 id="backup-title">备用线路</h2><button class="close-dialog" data-close="backup-dialog" aria-label="关闭备用线路状态">×</button></div><div id="backup-content" aria-live="polite"></div><p class="backup-caption">仅标准订阅 Compat · 主节点优先，故障时使用备用。此处不代表客户端当前正在使用的线路。</p><p class="backup-terminal-note">订阅链接在终端「机场订阅 → 自建备用订阅」中添加、更换或删除。客户端需更新标准订阅。</p></dialog>'

    def user_links(self, state, settings, client_id):
        user = state.users.get(client_id)
        if user is None:
            return None
        base = 'https://' + self.config.subscription_authority
        result = []
        def add_page(key, title):
            record = settings['pages'].get(key)
            if record and record['binding'] == hashlib.sha256(user.token.encode()).hexdigest():
                result.append({'title':title,'url':base+page_path(record['token'],user.email),'rotate':'rotate-page','target':key})
        if client_id == state.owner_client_id:
            result.append({'title':'管理员入口','url':base+admin_prefix(settings,state)+'/','rotate':None,'target':None})
        add_page(str(client_id),'个人网页')
        if client_id == state.owner_client_id:
            add_page('airport','机场专用网页')
        variants = ['compat','balance','airport','nodes'] if client_id==state.owner_client_id else ['compat','nodes']
        titles = {'compat':'标准订阅','balance':'Balance 订阅','airport':'机场专用订阅','nodes':'仅节点'}
        for variant in variants:
            filename = PROFILE_FILENAMES[variant]
            if not user.current_release or not (Path(self.config.public_root)/'releases'/str(client_id)/user.current_release/filename).is_file():
                continue
            token = airport_only_token(user.token,schema_version=state.schema_version) if variant=='airport' else user.token
            result.append({'title':titles[variant],'url':base+'/s/'+token+'/'+filename,'rotate':'rotate-sub' if variant=='compat' else None,'target':str(client_id),'composition':self.published_composition(user) if variant=='compat' else None})
        if not any(r['title']=='个人网页' for r in result):
            result.append({'title':'个人网页尚未生成','url':None,'rotate':'rotate-page','target':str(client_id)})
        return {'email':user.email,'links':result}


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, *_):
        pass

    def send_error(self, *_):
        self.respond(404, "页面不可用")

    def respond(self, code, body="", headers=(), content_type="text/html; charset=utf-8"):
        encoded = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        # no-referrer makes native form POSTs send Origin:null, breaking the
        # strict same-origin check. strict-origin preserves the origin while
        # still stripping capability paths from every Referer header.
        self.send_header("Referrer-Policy", "strict-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.get_page()
        except Exception:
            self.respond(503, "数据暂时不可用，请稍后重试。")

    def get_page(self):
        portal = self.server.portal
        settings = read_settings(portal.config.private_root)
        if not settings['enabled']:
            return self.respond(404, '页面不可用')
        assets = {'/portal-assets/portal.css':('portal.css','text/css'),'/portal-assets/portal.js':('portal.js','application/javascript'),'/clients/':('clients.html','text/html; charset=utf-8')}
        if self.path in assets:
            filename, content_type = assets[self.path]
            return self.respond(200,(portal.root/'assets'/'portal'/filename).read_bytes(),content_type=content_type)
        state = load_state(Path(portal.config.private_root)/'state.json')
        if state is None:
            return self.respond(503,'尚未初始化订阅。')
        prefix = admin_prefix(settings,state)
        if prefix and self.path.startswith(prefix+'/'):
            rest = self.path[len(prefix):]
            session = portal.session_record(self.headers.get('Cookie'),settings,state)
            if rest == '/login':
                if session:
                    return self.respond(303,headers=(('Location',prefix+'/'),))
                body = '<section class="card login-card"><form method="post" action="%s/login"><label for="username">管理员账号</label><input id="username" name="username" autocomplete="username" required maxlength="254"><label for="password">管理员密码</label><input id="password" type="password" name="password" autocomplete="current-password" required minlength="8" maxlength="256"><button class="button primary">登录</button></form></section>' % esc(prefix)
                return self.respond(200,portal.shell('owner 登录',body))
            if not session:
                if rest in {'/onlines','/tasks','/server','/backup','/overview','/overview/refresh'} or rest.endswith('/links'):
                    return self.respond(401,'{}',content_type='application/json')
                return self.respond(303,headers=(('Location',prefix+'/login'),))
            if rest == '/':
                return self.respond(200,portal.admin(state,settings,csrf=session[3]))
            if rest in {'/overview','/overview/refresh'}:
                if rest == '/overview/refresh':
                    portal.refresh_overview_data()
                return self.respond(200,portal.admin(state,settings,csrf=session[3]))
            if rest == '/backup':
                return self.respond(200,json.dumps(portal.backup_summary()),content_type='application/json')
            if rest == '/server':
                return self.respond(200,portal.system_card())
            if rest == '/onlines':
                return self.respond(200,json.dumps({'states':portal.online_states(state)}),content_type='application/json')
            if rest == '/tasks':
                from clash_sub.web_tasks import history
                return self.respond(200,json.dumps({'tasks':history(portal.config)}),content_type='application/json')
            if rest == '/airport/':
                return self.respond(200,portal.account('airport',state,admin=True,prefix=prefix,csrf=session[3]))
            match = re.fullmatch(r'/user/([1-9][0-9]{0,18})/(links)?',rest)
            if match and int(match[1]) in state.users:
                if match[2]:
                    return self.respond(200,json.dumps(portal.user_links(state,settings,int(match[1]))),content_type='application/json')
                return self.respond(200,portal.account(match[1],state,admin=True,prefix=prefix,csrf=session[3]))
        match = re.fullmatch(r'/p/([A-Za-z0-9_-]{43})(?:-([A-Za-z0-9._@+-]{1,254}))?/',self.path)
        if match:
            key = portal.resolve_page(match[1],settings,state)
            if key:
                user = state.users[state.owner_client_id if key=='airport' else int(key)]
                if match[2] is None or secrets.compare_digest(match[2],user.email):
                    return self.respond(200,portal.account(key,state))
        return self.respond(404,'页面不可用')

    def do_POST(self):
        portal = self.server.portal
        try:
            settings = read_settings(portal.config.private_root)
            state = load_state(Path(portal.config.private_root)/'state.json')
            prefix = admin_prefix(settings,state) if state else None
            if not settings['enabled'] or not prefix or not self.path.startswith(prefix+'/'):
                return self.respond(404,'页面不可用')
            parsed_origin = urlsplit('https://'+portal.config.subscription_authority)
            origin = 'https://'+parsed_origin.hostname+(':'+str(parsed_origin.port) if parsed_origin.port not in (None,443) else '')
            if self.headers.get('Origin') != origin:
                return self.respond(403,'请求无效')
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 2048 or self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                return self.respond(400,'请求无效')
            fields = parse_qs(self.rfile.read(length).decode(),max_num_fields=6)
            if any(len(values)!=1 for values in fields.values()):
                return self.respond(400,'请求无效')
            payload = {key:values[0] for key,values in fields.items()}
            rest = self.path[len(prefix):]
            if rest == '/login':
                token = portal.login(payload.get('password',''),settings,username=payload.get('username',''))
                if not token:
                    return self.respond(429,'登录失败或尝试过于频繁，请五分钟后再试。<a href="%s/login">返回</a>'%esc(prefix))
                return self.respond(303,headers=(('Location',prefix+'/'),('Set-Cookie','portal_session=%s; Path=/admin/; Secure; HttpOnly; SameSite=Strict; Max-Age=28800'%token)))
            session = portal.session_record(self.headers.get('Cookie'),settings,state)
            if not session:
                return self.respond(401,'{}',content_type='application/json')
            if not secrets.compare_digest(payload.get('csrf',''),session[3]):
                return self.respond(403,'请求无效')
            if rest == '/logout':
                cookie = http.cookies.SimpleCookie(self.headers.get('Cookie',''))
                if 'portal_session' in cookie:
                    with portal.lock:
                        portal.sessions.pop(hashlib.sha256(cookie['portal_session'].value.encode()).hexdigest(),None)
                return self.respond(303,headers=(('Location',prefix+'/login'),('Set-Cookie','portal_session=; Path=/admin/; Secure; HttpOnly; SameSite=Strict; Max-Age=0')))
            if rest == '/tasks/delete':
                if payload.get('confirmed') != 'yes':
                    return self.respond(400,'必须确认操作')
                from clash_sub.web_tasks import delete_history, authorization
                from clash_sub.operation import ServiceError
                try:
                    delete_history(portal.config,authorization(settings,state))
                except ServiceError:
                    return self.respond(409,'操作繁忙，请稍后重试')
                except ValueError:
                    return self.respond(400,'记录无效')
                return self.respond(200,'{}',content_type='application/json')
            if rest == '/tasks':
                if payload.get('confirmed') != 'yes':
                    return self.respond(400,'必须确认操作')
                from clash_sub.web_tasks import enqueue, authorization
                from clash_sub.operation import ServiceError
                try:
                    task_id = enqueue(portal.config,payload.get('action'),payload.get('target') or None,payload.get('request_id'),authorization(settings,state))
                except ServiceError:
                    return self.respond(409,'已有操作正在执行，请稍后查看执行记录。')
                except ValueError:
                    return self.respond(400,'操作无效或后台任务未能启动。')
                return self.respond(202,json.dumps({'task_id':task_id}),content_type='application/json')
            return self.respond(404,'页面不可用')
        except Exception:
            return self.respond(400,'请求无效')


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    def handle_error(self, *_):
        pass


def serve(config, listener):
    server = Server.__new__(Server)
    socketserver.BaseServer.__init__(server, listener.getsockname(), Handler)
    server.socket = listener
    server.portal = Portal(config)
    try:
        server.serve_forever()
    finally:
        server.server_close()
