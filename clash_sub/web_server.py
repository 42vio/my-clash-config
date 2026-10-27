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

from clash_sub.domain import PROFILE_FILENAMES
from clash_sub.metadata import TrafficMetadataStore
from clash_sub.state import airport_only_token, load_state
from clash_sub.web_settings import read_settings, verify_password
from clash_sub.xui import read_xui_snapshot, read_inbound_traffic


def esc(value):
    return html.escape(str(value), quote=True)


def size(value):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "%.1f %s" % (value, unit)
        value /= 1024


def expiry(value):
    return datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime("%Y.%m.%d") if value > 0 else "长期"


def usage_card(title, traffic, note="", *, compact=False):
    if traffic is None:
        return '<section class="card"><h2>%s</h2><p style="margin-top:20px">暂无数据</p></section>' % esc(title)
    used = traffic.upload + traffic.download
    ratio = min(100, used / traffic.total * 100) if traffic.total else 0
    total = size(traffic.total) if traffic.total else "不限额"
    remaining = size(max(0, traffic.total - used)) if traffic.total else "不限额"
    if not compact:
        ratio_label = '%.1f%%' % ratio if traffic.total else '∞'
        return '''<section class="card"><div class="card-head"><h2>%s</h2><span class="subtle">%s</span></div><div class="usage"><div class="ring" role="img" aria-label="已用 %s" style="background:conic-gradient(var(--green) 0 %.2f%%,#eef2f4 %.2f%% 100%%)"><div class="ring-inner"><strong>%s</strong><span>本期已用</span></div></div><div><p class="subtle">已用流量</p><div class="number" style="font-size:27px">%s</div><p class="subtle">总额度 %s</p><div class="remaining">剩余 %s</div></div></div><div class="stats"><div><label>↑ 上传</label><strong>%s</strong></div><div><label>↓ 下载</label><strong>%s</strong></div><div><label>到期时间</label><strong>%s</strong></div></div></section>''' % (esc(title), esc(note), ratio_label, ratio, ratio, ratio_label, size(used), total, remaining, size(traffic.upload), size(traffic.download), expiry(traffic.expiry_ms))
    return '''<section class="card"><div class="card-head"><h2>%s</h2><span class="subtle">%s</span></div>
    <div class="provider-usage"><p>已用流量</p><div class="number">%s <small>/ %s</small></div>
    <div class="bar"><i style="width:%.2f%%"></i></div><p class="remaining">剩余 %s</p></div>
    <div class="stats"><div><label>↑ 上传</label><strong>%s</strong></div><div><label>↓ 下载</label><strong>%s</strong></div><div><label>到期时间</label><strong>%s</strong></div></div></section>''' % (esc(title), esc(note), size(used), total, ratio, remaining, size(traffic.upload), size(traffic.download), expiry(traffic.expiry_ms))


class Portal:
    def __init__(self, config, *, clock=time.time, store=None, snapshot_reader=read_xui_snapshot):
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

    def inbound_card(self):
        with self._snapshot_lock:
            if self.clock() - self._inbound_time >= 300:
                self._inbound = read_inbound_traffic(self.config.xui_database)
                self._inbound_time = self.clock()
            traffic = self._inbound
        if traffic is None:
            return '<h3>自建流量 · 3x-ui 入站</h3><p>暂无数据 · 入站统计读取失败</p>'
        used = traffic['upload'] + traffic['download']
        total = traffic['total']
        quota = ' / ' + size(total) if total else ''
        ratio = min(100, used / total * 100) if total else 0
        return '<h3>自建流量 · 3x-ui 入站</h3><div class="provider-usage"><div class="number">%s<small>%s</small></div>%s<p class="subtle">↑ %s · ↓ %s</p></div><p class="subtle">入站当前累计 · 每月由 3x-ui 重置%s</p>' % (size(used), quota, '<div class="bar"><i style="width:%.2f%%"></i></div>' % ratio if total else '', size(traffic['upload']), size(traffic['download']), ' · 剩余 '+size(max(0,total-used)) if total else ' · 入站总额度未设置')

    def snapshot(self):
        with self._snapshot_lock:
            if self._snapshot is None or self.clock() - self._snapshot_time >= 300:
                self._snapshot = self.snapshot_reader(self.config.xui_database)
                self._snapshot_time = self.clock()
            return self._snapshot

    def session(self, cookie, settings):
        try:
            parsed = http.cookies.SimpleCookie(cookie or "")
            value = parsed["portal_session"].value
        except (KeyError, http.cookies.CookieError):
            return False
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self.lock:
            record = self.sessions.get(digest)
        return bool(record and record[0] > self.clock() and secrets.compare_digest(record[1], settings.get("password_hash", "")))

    def login(self, password, settings):
        with self.lock:
            now = self.clock()
            while self.failures and self.failures[0] <= now - 300:
                self.failures.popleft()
            if len(self.failures) >= 5:
                return None
            if not verify_password(password, settings.get("password_hash", "")):
                self.failures.append(now)
                return None
            self.sessions = {key: value for key, value in self.sessions.items() if value[0] > now}
            if len(self.sessions) >= 32:
                self.sessions.clear()
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (now + 8 * 3600, settings["password_hash"])
            return token

    def resolve_page(self, token, settings, state):
        for key, record in settings["pages"].items():
            if not secrets.compare_digest(token, record.get("token", "")):
                continue
            user = state.users.get(state.owner_client_id if key == "airport" else int(key))
            if user and user.active and secrets.compare_digest(hashlib.sha256(user.token.encode()).hexdigest(), record.get("binding", "")):
                return key
        return None

    def shell(self, title, body, *, admin=False):
        top = '<a href="/admin/">管理总览</a>' if admin else "只读订阅中心"
        return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>42 / %s</title><link rel="stylesheet" href="/portal-assets/portal.css"><script src="/portal-assets/portal.js" defer></script></head><body><header><div class="nav"><div class="brand"><div class="logo">42</div> CONNECT</div><div class="subtle">%s</div></div></header><main><div class="heading"><div><div class="eyebrow">42 CONNECT</div><h1>%s</h1></div>%s</div>%s<footer><span>只读订阅中心 · 请妥善保管页面与订阅链接</span><span>访问时间 %s 北京时间 · 用量按缓存展示</span></footer></main><div id="toast" role="status" hidden></div></body></html>''' % (esc(title), top, esc(title), '<form method="post" action="/admin/logout"><input type="hidden" name="logout" value="1"><button class="button">退出登录</button></form>' if admin else "", body, datetime.fromtimestamp(self.clock(), ZoneInfo("Asia/Shanghai")).strftime("%H:%M"))

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

    def account(self, key, state, *, admin=False):
        airport = key == "airport"
        user = state.users[state.owner_client_id if airport else int(key)]
        client = next((c for c in self.snapshot().clients if c.client_id == user.client_id), None)
        if client is None:
            raise ValueError("account_unavailable")
        traffic = self.store.airport_traffic() if airport else self.store.traffic_for(user.client_id)
        links = self.links(state, user, airport=airport) if user.active and client.enabled and user.current_release else '<p>当前账户未启用，暂无可用订阅。</p>'
        note = self.freshness_note("airport" if airport else "users")
        body = '<div class="grid">' + usage_card("Amy 用量" if airport else "个人用量", traffic, note) + '<section class="card"><div class="card-head"><h2>订阅连接</h2></div>' + links + '''<div class="subscription-help"><details class="help"><summary>如何导入订阅？</summary><p>Clash Verge Rev：在「订阅」中粘贴标准订阅链接。Clash Mi：添加远程配置并粘贴链接。</p><p>仅节点订阅供不支持 Clash / Mihomo 配置、但支持通用节点订阅的客户端使用；不包含机场节点、DNS、策略组和分流规则。</p></details><a class="client-shortcut" href="/clients/">下载客户端 ↗</a></div><p class="privacy">订阅链接包含访问凭据，请勿转发。</p></section></div>'''
        if not airport:
            body += self.system_card()
        return self.shell("机场订阅" if airport else user.email + " · 我的订阅", body, admin=admin)

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
        # Linux metrics only; unavailable values stay unavailable instead of looking healthy.
        metrics = {"CPU 使用率": "暂无数据", "内存": "暂无数据", "磁盘": "暂无数据", "实时网速": "暂无数据"}
        uptime = "暂无数据"
        try:
            values = dict((key.rstrip(":"), int(value.split()[0]) * 1024) for key, value in (line.split(None, 1) for line in Path('/proc/meminfo').read_text().splitlines()))
            metrics["内存"] = size(values['MemTotal'] - values['MemAvailable']) + " / " + size(values['MemTotal'])
            disk = shutil.disk_usage(self.config.public_root)
            metrics["磁盘"] = size(disk.used) + " / " + size(disk.total)
            uptime = "%.1f 天" % (float(Path('/proc/uptime').read_text().split()[0]) / 86400)
        except (OSError, ValueError, KeyError):
            pass
        try:
            cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            # Exclude loopback, container bridges and virtual peer devices.
            # The service has PrivateNetwork=yes. PID 1 remains in the host
            # namespace; /proc/net/dev would incorrectly report only loopback.
            counters = [line.split(':', 1) for line in Path('/proc/1/net/dev').read_text().splitlines()[2:]]
            counters = [numbers.split() for name, numbers in counters if not re.match(r'^(lo|veth|docker|br-|tun|sit|ipv6net|wg|tailscale)', name.strip())]
            current = (self.clock(), sum(int(v[0]) for v in counters), sum(int(v[8]) for v in counters), sum(cpu), cpu[3] + cpu[4])
            with self.lock:
                previous, self._net_previous = self._net_previous, current
            if previous and current[0] > previous[0]:
                elapsed = current[0] - previous[0]
                metrics["实时网速"] = '↑ %s/s · ↓ %s/s' % (size(max(0, current[2]-previous[2])/elapsed), size(max(0, current[1]-previous[1])/elapsed))
                if current[3] > previous[3]:
                    metrics["CPU 使用率"] = '%.1f%%' % (100 * (1 - (current[4]-previous[4])/(current[3]-previous[3])))
        except (OSError, ValueError, IndexError):
            pass
        return '<section class="card server"><div class="card-head"><h2>服务器概况</h2><span class="subtle">运行时间 %s</span></div><div class="metrics">%s</div><p class="subtle" style="margin-top:16px">CPU 与网速在再次访问时根据采样差值更新，不代表外部连通性。</p></section>' % (esc(uptime), ''.join('<div class="metric"><label>%s</label><strong style="font-size:18px">%s</strong></div>' % (esc(k), esc(v)) for k, v in metrics.items()))

    def admin(self, state, settings):
        body = '<div class="admin-overview"><section class="card traffic-card"><div class="card-head"><h2>流量概况</h2></div>' + self.inbound_card() + '<hr class="divider">'
        body += usage_card("Amy 机场", self.store.airport_traffic(), self.freshness_note('airport'), compact=True) + '</section></div><section class="card users"><div class="card-head"><h2>成员用量</h2></div><table class="user-table"><thead><tr><th>用户</th><th>已用 / 额度</th><th>到期</th><th>状态</th><th></th></tr></thead><tbody>'
        clients = {c.client_id: c for c in self.snapshot().clients}
        for client_id, user in sorted(state.users.items()):
            client = clients.get(client_id)
            if client is None:
                continue
            traffic = self.store.traffic_for(client_id)
            used = size(traffic.upload + traffic.download) if traffic else "暂无数据"
            total = (size(traffic.total) if traffic.total else "不限额") if traffic else "暂无数据"
            status = "有效" if user.active and client.enabled and (client.expiry_ms <= 0 or client.expiry_ms > self.clock()*1000) else "已停用或到期"
            body += '<tr><td class="user-name">%s</td><td>%s / %s</td><td>%s</td><td>%s</td><td><a class="detail" href="/admin/user/%d/">查看详情 ↗</a></td></tr>' % (esc(user.email), used, total, expiry(client.expiry_ms // 1000), status, client_id)
        body += '</tbody></table></section><p style="margin-top:16px"><a class="detail" href="/admin/airport/">机场专用页面 ↗</a> · <a class="detail" href="/clients/">下载客户端 ↗</a></p>' + self.system_card()
        return self.shell("管理总览", body, admin=True)


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
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
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
        if not settings["enabled"]:
            return self.respond(404, "页面不可用")
        assets = {"/portal-assets/portal.css": ("portal.css", "text/css"), "/portal-assets/portal.js": ("portal.js", "application/javascript"), "/clients/": ("clients.html", "text/html; charset=utf-8")}
        if self.path in assets:
            filename, content_type = assets[self.path]
            return self.respond(200, (portal.root / "assets" / "portal" / filename).read_bytes(), content_type=content_type)
        authenticated = portal.session(self.headers.get("Cookie"), settings)
        if self.path == "/admin/login":
            body = '<section class="card login-card"><form method="post" action="/admin/login"><label for="password">管理员密码</label><input id="password" type="password" name="password" autocomplete="current-password" required minlength="12" maxlength="256"><button class="button primary">登录</button></form></section>'
            return self.respond(200, portal.shell("管理员登录", body))
        if self.path.startswith("/admin/") and not authenticated:
            return self.respond(303, headers=(("Location", "/admin/login"),))
        state = load_state(Path(portal.config.private_root) / "state.json")
        if not state:
            return self.respond(503, "尚未初始化订阅。")
        if self.path == "/admin/":
            return self.respond(200, portal.admin(state, settings))
        if self.path == "/admin/airport/":
            return self.respond(200, portal.account("airport", state, admin=True))
        match = re.fullmatch(r"/admin/user/([1-9][0-9]{0,18})/", self.path)
        if match and int(match[1]) in state.users:
            return self.respond(200, portal.account(match[1], state, admin=True))
        match = re.fullmatch(r"/p/([A-Za-z0-9_-]{43})/", self.path)
        if match:
            key = portal.resolve_page(match[1], settings, state)
            if key:
                return self.respond(200, portal.account(key, state))
        return self.respond(404, "页面不可用")

    def do_POST(self):
        portal = self.server.portal
        try:
            settings = read_settings(portal.config.private_root)
            if not settings["enabled"]:
                return self.respond(404, "页面不可用")
            # Fixed configured origin, never trust Host or client forwarded headers.
            parsed_origin = urlsplit("https://" + portal.config.subscription_authority)
            origin = "https://" + parsed_origin.hostname + (":" + str(parsed_origin.port) if parsed_origin.port not in (None,443) else "")
            if self.headers.get("Origin") != origin:
                return self.respond(403, "请求无效")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return self.respond(400, "请求无效")
            payload = parse_qs(self.rfile.read(length).decode(), max_num_fields=2)
            if self.path == "/admin/login":
                token = portal.login(payload.get("password", [""])[0], settings)
                if not token:
                    return self.respond(429, '登录失败或尝试过于频繁，请五分钟后再试。<a href="/admin/login">返回</a>')
                return self.respond(303, headers=(("Location", "/admin/"), ("Set-Cookie", "portal_session=%s; Path=/admin/; Secure; HttpOnly; SameSite=Strict; Max-Age=28800" % token)))
            if self.path == "/admin/logout":
                parsed = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                if "portal_session" in parsed:
                    with portal.lock:
                        portal.sessions.pop(hashlib.sha256(parsed['portal_session'].value.encode()).hexdigest(), None)
                return self.respond(303, headers=(("Location", "/admin/login"), ("Set-Cookie", "portal_session=; Path=/admin/; Secure; HttpOnly; SameSite=Strict; Max-Age=0")))
            return self.respond(404, "页面不可用")
        except Exception:
            return self.respond(400, "请求无效")


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
