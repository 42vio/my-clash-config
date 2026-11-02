"""Private Amy cumulative usage captured during successful subscription refreshes.

Writers hold the existing service operation lock. Readers never repair files.
Differences describe the actual interval between observations, never an
invented calendar-day total. Resets and cycle boundaries keep their baselines.
"""

import json
import html
import os
import stat
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from clash_sub.domain import Traffic


FILENAME = "airport-usage.json"
TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_SAMPLES = 10000
MAX_FILE_BYTES = 2 * 1024 * 1024


class AirportUsageError(ValueError):
    def __init__(self):
        super().__init__("airport_usage_invalid")


def read_samples(private_root):
    return _read_history(private_root)['samples']


def _read_history(private_root):
    path = Path(private_root) / FILENAME
    try:
        details = path.lstat()
    except FileNotFoundError:
        return {'samples': [], 'reset_window': None}
    if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
            or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > MAX_FILE_BYTES):
        raise AirportUsageError()
    try:
        document = json.loads(path.read_bytes())
        if (not isinstance(document, dict)
                or type(document.get('schema_version')) is not int
                or document['schema_version'] not in (1, 2)
                or set(document) != ({'schema_version', 'samples'} if document['schema_version'] == 1
                                     else {'schema_version', 'samples', 'reset_window'})
                or not isinstance(document["samples"], list) or len(document["samples"]) > MAX_SAMPLES):
            raise AirportUsageError()
        previous = -1
        for row in document["samples"]:
            if (not isinstance(row, dict) or set(row) != {"at", "upload", "download"}
                    or any(type(v) is not int or v < 0 for v in row.values())
                    or row["at"] < previous):
                raise AirportUsageError()
            datetime.fromtimestamp(row["at"], TIMEZONE)
            previous = row["at"]
        reset = document.get('reset_window')
        if reset is not None:
            if (not isinstance(reset, list) or len(reset) != 2
                    or any(type(v) is not int or v < 0 for v in reset)
                    or reset[0] > reset[1] or not document['samples']
                    or reset[1] > document['samples'][0]['at']):
                raise AirportUsageError()
            for value in reset:
                datetime.fromtimestamp(value, TIMEZONE)
        return {'samples': document['samples'], 'reset_window': reset}
    except (ValueError, OverflowError, OSError):
        raise AirportUsageError() from None


def record_sample(private_root, traffic, timestamp):
    if traffic is None:
        return False
    if (not isinstance(traffic, Traffic) or type(timestamp) is not int or timestamp < 0
            or any(type(v) is not int or v < 0 for v in (traffic.upload, traffic.download))):
        raise AirportUsageError()
    root = Path(private_root)
    details = root.lstat()
    if not stat.S_ISDIR(details.st_mode) or root.is_symlink() or details.st_uid != os.geteuid():
        raise AirportUsageError()
    history = _read_history(root)
    samples = history['samples']
    # A delayed observation cannot replace the baseline of newer data.
    if samples and timestamp < samples[-1]["at"]:
        return False
    samples.append({"at": timestamp, "upload": traffic.upload, "download": traffic.download})
    samples, reset = _current_observations(samples, history['reset_window'])
    samples = samples[-MAX_SAMPLES:]
    payload = json.dumps({"schema_version": 2, "samples": samples, 'reset_window': reset}, separators=(",", ":")).encode() + b"\n"
    if len(payload) > MAX_FILE_BYTES:
        raise AirportUsageError()
    descriptor, temporary = tempfile.mkstemp(prefix=".airport-usage-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / FILENAME)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def cycle_start(day):
    if day.day >= 19:
        return day.replace(day=19)
    return (day.replace(day=1) - timedelta(days=1)).replace(day=19)


def next_cycle(start):
    return (start.replace(day=28) + timedelta(days=4)).replace(day=19)


def _decreased(previous, current):
    return any(current[key] < previous[key] for key in ('upload', 'download'))


def _current_observations(samples, reset_window=None):
    """Only prune on a fresh drop observed around the expected reset day.

    The 19th through 21st covers a daytime reset followed by the next daily
    refresh. Longer gaps and other dates remain anomalies, preserving evidence.
    A persisted reset window prevents a second correction in the same month
    from discarding the newly started cycle again.
    """
    first = 0
    for index in range(1, len(samples)):
        previous, current = samples[index - 1:index + 1]
        day = datetime.fromtimestamp(current['at'], TIMEZONE).date()
        already_reset = (reset_window is not None and cycle_start(day) <=
                         cycle_start(datetime.fromtimestamp(reset_window[1], TIMEZONE).date()))
        if (19 <= day.day <= 21 and current['at'] - previous['at'] <= 72 * 3600
                and _decreased(previous, current) and not already_reset):
            first = index
            reset_window = [previous['at'], current['at']]
    return samples[first:], reset_window


def usage_cycles(samples, now, reset_window=None):
    today = datetime.fromtimestamp(now, TIMEZONE).date()
    samples, reset_window = _current_observations([s for s in samples if s['at'] <= now], reset_window)
    start = cycle_start(datetime.fromtimestamp(samples[0]['at'], TIMEZONE).date()) if samples else cycle_start(today)
    expected = next_cycle(start)
    # A first-ever observation around the 19th cannot establish whether the
    # provider has reset yet. Keep it explicitly unconfirmed.
    if reset_window is None and samples and 19 <= datetime.fromtimestamp(samples[0]['at'], TIMEZONE).day <= 21:
        expected = start
    rows = []
    previous = None
    for sample in samples:
        delta, status = None, 'baseline'
        if previous is not None:
            if _decreased(previous, sample):
                status = 'anomaly'
            else:
                delta = sample['upload'] + sample['download'] - previous['upload'] - previous['download']
                status = 'ok'
        row = dict(sample, used=sample['upload'] + sample['download'], delta=delta,
                   since=None if previous is None else previous['at'], status=status)
        rows.append(row)
        previous = sample
    return [{'start': start.isoformat(),
             'end': max(expected - timedelta(days=1), today, start).isoformat(),
             'expected': expected.isoformat(), 'pending': today >= expected,
             'reset_window': reset_window, 'rows': list(reversed(rows))}]


def history_button():
    return '<button type="button" class="section-shortcut" data-usage-history aria-haspopup="dialog" aria-controls="airport-usage-dialog">用量记录 ↗</button>'


def _usage_chart(cycle, kind, format_size):
    rows = list(reversed(cycle['rows']))
    incremental = kind == 'delta'
    title = '区间用量' if incremental else '周期累计用量'
    eligible = [r for r in rows if r[kind] is not None]
    if not eligible:
        return '<section class="usage-chart"><h3>%s</h3><p class="subtle">需要两次同周期记录才能计算增量。</p></section>' % title
    start = datetime.fromisoformat(cycle['start']).replace(tzinfo=TIMEZONE).timestamp()
    end = datetime.fromisoformat(cycle['end']).replace(tzinfo=TIMEZONE).timestamp() + 86400
    maximum = max(1, max(r[kind] for r in eligible)) * 1.15
    x = lambda row: 68 + 580 * (row['at'] - start) / (end - start)
    y = lambda value: 164 - 140 * value / maximum
    parts = ['<section class="usage-chart"><h3>%s</h3><svg viewBox="0 0 680 204" role="img" aria-label="%s，详细数值见下方记录明细"><title>%s</title>' % (title, title, title)]
    for fraction in (0, .5, 1):
        yy = y(maximum * fraction)
        parts.append('<line class="usage-gridline" x1="68" x2="648" y1="%.2f" y2="%.2f"/><text class="usage-axis" x="58" y="%.2f" text-anchor="end">%s</text>' % (yy, yy, yy + 4, html.escape(format_size(maximum * fraction))))
    for fraction in (0, .5, 1):
        timestamp = start + (end - start - 86400) * fraction
        label = datetime.fromtimestamp(timestamp, TIMEZONE).strftime('%m-%d')
        xx = 68 + 580 * (timestamp - start) / (end - start)
        parts.append('<text class="usage-axis" x="%.2f" y="190" text-anchor="middle">%s</text>' % (xx, label))
    if incremental:
        width = max(3, min(16, 420 / max(1, len(eligible))))
        for row in eligible:
            label = '%s 至 %s：%s' % (datetime.fromtimestamp(row['since'], TIMEZONE).strftime('%m-%d %H:%M'), datetime.fromtimestamp(row['at'], TIMEZONE).strftime('%m-%d %H:%M'), format_size(row['delta']))
            gap = row['at'] - row['since'] > 36 * 3600
            parts.append('<rect class="usage-column%s" x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="2" tabindex="0" aria-label="%s"><title>%s</title></rect>' % (' usage-column-gap' if gap else '', x(row)-width/2, y(row['delta']), width, max(1, 164-y(row['delta'])), html.escape(label, quote=True), html.escape(label)))
    else:
        segments = []
        segment = []
        for row in rows:
            if row['status'] != 'ok' or (row['since'] is not None and row['at'] - row['since'] > 36 * 3600):
                if segment:
                    segments.append(segment)
                segment = []
            segment.append('%.2f,%.2f' % (x(row), y(row['used'])))
        if segment:
            segments.append(segment)
        for points in segments:
            parts.append('<polyline class="usage-line" points="%s"/>' % ' '.join(points))
        for row in rows:
            label = '%s：%s' % (datetime.fromtimestamp(row['at'], TIMEZONE).strftime('%m-%d %H:%M'), format_size(row['used']))
            parts.append('<circle class="usage-point" cx="%.2f" cy="%.2f" r="3.5" tabindex="0" aria-label="%s"><title>%s</title></circle>' % (x(row), y(row['used']), html.escape(label, quote=True), html.escape(label)))
    parts.append('</svg></section>')
    return ''.join(parts)


def render_history(private_root, now, format_size):
    """Render only into an already authorized owner/airport page."""
    header = '<dialog id="airport-usage-dialog" aria-labelledby="airport-usage-title"><div class="airport-usage-history" id="airport-usage-history"><div class="dialog-head"><h2 id="airport-usage-title">Amy 用量记录</h2><button type="button" class="close-dialog" data-usage-close aria-label="关闭用量记录">×</button></div>'
    end = '</div></dialog>'
    try:
        history = _read_history(private_root)
        samples = history['samples']
    except (OSError, AirportUsageError):
        return header + '<p>用量记录暂时不可用，请稍后重试。</p>' + end
    if not samples:
        return header + '<p>尚无用量记录，下一次成功拉取机场订阅后开始记录。</p>' + end
    cycles = usage_cycles(samples, now, history['reset_window'])
    if not cycles:
        return header + '<p>暂无可用记录。</p>' + end
    escape = html.escape
    body = header + '<div class="usage-cycle-label"><span>当前记录</span><span>%s 起 · 预计每月 19 日重置</span></div>' % cycles[0]['start']
    for cycle in cycles:
        body += '<div class="usage-cycle-records">'
        if cycle['pending']:
            body += '<p class="warning-note">已到预计重置日，尚未观察到符合条件的重置，继续保留原记录。</p>'
        if cycle['reset_window']:
            left, right = [datetime.fromtimestamp(t, TIMEZONE).strftime('%m-%d %H:%M:%S') for t in cycle['reset_window']]
            body += '<p class="subtle usage-reset-window">根据计数回落推定，重置发生于 %s 至 %s 之间，具体时刻未知。</p>' % (left, right)
        if not cycle['rows']:
            body += '<p>本周期尚无记录。</p></div>'
            continue
        latest = cycle['rows'][0]
        body += '<div class="usage-history-summary"><span>最近累计 <strong>%s</strong></span><span>%d 次记录</span></div>' % (html.escape(format_size(latest['used'])), len(cycle['rows']))
        body += _usage_chart(cycle, 'used', format_size) + _usage_chart(cycle, 'delta', format_size)
        body += '<details class="usage-record-details"><summary>记录明细</summary><table class="usage-history-table"><thead><tr><th>拉取时间</th><th>累计已用</th><th>较上次增加</th></tr></thead><tbody>'
        for row in cycle['rows']:
            labels = {'baseline': '首次记录', 'anomaly': '计数异常回落，不计增量'}
            change = format_size(row['delta']) if row['delta'] is not None else labels[row['status']]
            stamp = datetime.fromtimestamp(row['at'], TIMEZONE).strftime('%m-%d %H:%M:%S')
            detail = '上传 %s，下载 %s' % (format_size(row['upload']), format_size(row['download']))
            interval = '' if row['since'] is None else datetime.fromtimestamp(row['since'], TIMEZONE).strftime('%m-%d %H:%M:%S') + ' 至 ' + stamp
            body += '<tr><td>%s</td><td title="%s">%s</td><td title="%s">%s</td></tr>' % (stamp, escape(detail, quote=True), escape(format_size(row['used'])), escape(interval, quote=True), escape(change))
        body += '</tbody></table></details></div>'
    body += '<p class="subtle usage-history-note">北京时间 · 19 日仅为预计重置日期，不按零点清理。19–21 日、相隔不超过 72 小时的两次记录发生计数回落时推定重置，保留新周期并清理旧记录；同月不重复切换，其他回落标记异常并保留记录。曲线为机场累计值，柱形为两次拉取间增量，并非自然日统计；超过 36 小时的区间用浅橙色标记，缺失期间不分摊。</p>'
    return body + end
