import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from clash_sub.airport_usage import (
    AirportUsageError, FILENAME, read_samples, record_sample, render_history, usage_cycles,
)
from clash_sub.domain import Traffic


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())


def sample(value, upload, download):
    return {'at': stamp(value), 'upload': upload, 'download': download}


class AirportUsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_records_cumulative_values_even_without_a_change(self):
        t = stamp('2026-11-02T06:30:00')
        traffic = Traffic(12, 34, 100, 0)
        self.assertTrue(record_sample(self.root, traffic, t))
        self.assertTrue(record_sample(self.root, traffic, t + 3600))
        self.assertTrue(record_sample(self.root, Traffic(13, 35, 100, 0), t + 3600))
        rows = read_samples(self.root)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {'at': t, 'upload': 12, 'download': 34})
        self.assertEqual((self.root / FILENAME).stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads((self.root / FILENAME).read_bytes())), {'schema_version', 'samples', 'reset_window'})

    def test_missing_header_and_older_timestamp_do_not_add_records(self):
        self.assertFalse(record_sample(self.root, None, 1))
        self.assertFalse((self.root / FILENAME).exists())
        record_sample(self.root, Traffic(1, 2, 0, 0), 100)
        self.assertFalse(record_sample(self.root, Traffic(9, 9, 0, 0), 99))
        self.assertEqual(len(read_samples(self.root)), 1)

    def test_failed_atomic_write_keeps_existing_history(self):
        record_sample(self.root, Traffic(1, 2, 0, 0), 100)
        before = (self.root / FILENAME).read_bytes()
        with patch('clash_sub.airport_usage.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                record_sample(self.root, Traffic(2, 3, 0, 0), 101)
        self.assertEqual((self.root / FILENAME).read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], [FILENAME])

    def test_unsafe_or_corrupt_history_is_not_overwritten(self):
        outside = self.root / 'outside.json'
        outside.write_text('unchanged')
        path = self.root / FILENAME
        path.symlink_to(outside)
        with self.assertRaises(AirportUsageError):
            record_sample(self.root, Traffic(1, 2, 0, 0), 100)
        self.assertEqual(outside.read_text(), 'unchanged')
        path.unlink()
        path.write_text('{broken')
        os.chmod(path, 0o600)
        with self.assertRaises(AirportUsageError):
            record_sample(self.root, Traffic(1, 2, 0, 0), 100)
        self.assertEqual(path.read_text(), '{broken')

    def test_difference_uses_real_interval_without_inventing_missing_days(self):
        rows = [sample('2026-11-01T07:00:00', 10, 100),
                sample('2026-11-03T06:15:00', 20, 300),
                sample('2026-11-03T09:00:00', 20, 300)]
        cycle = usage_cycles(rows, stamp('2026-11-03T10:00:00'))[0]
        self.assertEqual(len(cycle['rows']), 3)
        self.assertEqual([r['delta'] for r in cycle['rows']], [0, 210, None])
        self.assertEqual(cycle['rows'][1]['since'], rows[0]['at'])
        self.assertEqual(cycle['start'], '2026-10-19')
        self.assertEqual(cycle['end'], '2026-11-18')

    def test_reset_of_either_counter_restarts_baseline_even_when_total_grows(self):
        rows = [sample('2026-11-01T07:00:00', 100, 100),
                sample('2026-11-02T07:00:00', 10, 500),
                sample('2026-11-03T07:00:00', 15, 520)]
        actual = usage_cycles(rows, stamp('2026-11-03T10:00:00'))[0]['rows']
        self.assertEqual(actual[1]['status'], 'anomaly')
        self.assertIsNone(actual[1]['delta'])
        self.assertEqual(actual[0]['delta'], 25)

    def test_nineteenth_midnight_and_morning_do_not_clear_old_records(self):
        rows = [sample('2026-11-18T07:00:00', 10, 100),
                sample('2026-11-19T00:00:00', 11, 110),
                sample('2026-11-19T07:00:00', 12, 120)]
        for row in rows:
            record_sample(self.root, Traffic(row['upload'], row['download'], 999, 0), row['at'])
        self.assertEqual(read_samples(self.root), rows)
        before = (self.root / FILENAME).read_bytes()
        page = render_history(self.root, stamp('2026-11-19T09:00:00'), str)
        self.assertIn('尚未观察到符合条件的重置', page)
        self.assertIn('11-18 07:00:00', page)
        self.assertEqual((self.root / FILENAME).read_bytes(), before)

    def test_daytime_reset_observed_next_morning_prunes_old_cycle(self):
        rows = [sample('2026-11-18T07:00:00', 10, 100),
                sample('2026-11-19T07:00:00', 12, 120),
                sample('2026-11-20T07:00:00', 1, 2)]
        for row in rows:
            record_sample(self.root, Traffic(row['upload'], row['download'], 999, 0), row['at'])
        self.assertEqual(read_samples(self.root), [rows[-1]])
        page = render_history(self.root, rows[-1]['at'], str)
        self.assertIn('11-19 07:00:00 至 11-20 07:00:00', page)
        self.assertIn('具体时刻未知', page)
        self.assertIn('首次记录', page)
        self.assertNotIn('尚未观察到符合条件的重置', page)
        # A second correction near the 19th must not discard this cycle again.
        record_sample(self.root, Traffic(0, 1, 999, 0), stamp('2026-11-20T08:00:00'))
        self.assertEqual(len(read_samples(self.root)), 2)
        self.assertIn('计数异常回落', render_history(self.root, stamp('2026-11-20T09:00:00'), str))

    def test_off_schedule_drop_does_not_delete_records(self):
        for row in [sample('2026-11-01T07:00:00', 100, 100),
                    sample('2026-11-02T07:00:00', 1, 2)]:
            record_sample(self.root, Traffic(row['upload'], row['download'], 0, 0), row['at'])
        self.assertEqual(len(read_samples(self.root)), 2)
        self.assertIn('计数异常回落，不计增量', render_history(self.root, stamp('2026-11-02T08:00:00'), str))

    def test_long_gap_does_not_automatically_discard_evidence(self):
        rows = [sample('2026-11-10T07:00:00', 100, 100), sample('2026-11-20T07:00:00', 1, 2)]
        cycle = usage_cycles(rows, stamp('2026-11-20T09:00:00'))[0]
        self.assertEqual(len(cycle['rows']), 2)
        self.assertEqual(cycle['rows'][0]['status'], 'anomaly')
        self.assertTrue(cycle['pending'])

    def test_first_observation_on_nineteenth_does_not_assume_reset_happened(self):
        record_sample(self.root, Traffic(100, 100, 999, 0), stamp('2026-11-19T07:00:00'))
        self.assertIn('尚未观察到符合条件的重置', render_history(self.root, stamp('2026-11-19T08:00:00'), str))
        record_sample(self.root, Traffic(1, 2, 999, 0), stamp('2026-11-19T17:00:00'))
        self.assertEqual(len(read_samples(self.root)), 1)
        self.assertNotIn('尚未观察到符合条件的重置', render_history(self.root, stamp('2026-11-19T18:00:00'), str))

    def test_reset_metadata_survives_restart_and_year_boundary(self):
        rows = [sample('2026-12-19T07:00:00', 100, 100), sample('2026-12-20T07:00:00', 1, 2),
                sample('2027-01-19T07:00:00', 100, 100), sample('2027-01-20T07:00:00', 2, 3)]
        for row in rows:
            record_sample(self.root, Traffic(row['upload'], row['download'], 0, 0), row['at'])
        self.assertEqual(read_samples(self.root), [rows[-1]])
        page = render_history(self.root, rows[-1]['at'], str)
        self.assertIn('2027-01-19 起', page)
        self.assertIn('01-19 07:00:00 至 01-20 07:00:00', page)
        self.assertNotIn('<select', page)

    def test_legacy_file_is_not_pruned_just_because_month_changed(self):
        rows = [sample('2026-10-18T06:30:00', 10, 100), sample('2026-11-02T06:30:00', 20, 200)]
        path = self.root / FILENAME
        path.write_text(json.dumps({'schema_version': 1, 'samples': rows}))
        os.chmod(path, 0o600)
        record_sample(self.root, Traffic(25, 250, 999, 0), stamp('2026-11-03T06:30:00'))
        self.assertEqual(len(read_samples(self.root)), 3)
        self.assertIn('10-18 06:30:00', render_history(self.root, stamp('2026-11-03T07:00:00'), str))

    def test_render_shows_refresh_times_cumulative_usage_and_interval_delta(self):
        first = stamp('2026-11-01T06:05:00')
        second = stamp('2026-11-02T07:30:00')
        record_sample(self.root, Traffic(10, 100, 999, 0), first)
        record_sample(self.root, Traffic(20, 300, 999, 0), second)
        html = render_history(self.root, second, lambda n: '%d B' % n)
        self.assertIn('11-02 07:30:00', html)
        self.assertIn('320 B', html)
        self.assertIn('210 B', html)
        self.assertIn('11-01 06:05:00 至 11-02 07:30:00', html)
        self.assertIn('并非自然日统计', html)
        self.assertNotIn('最高用量日', html)

    def test_empty_and_corrupt_history_keep_the_page_available(self):
        self.assertIn('尚无用量记录', render_history(self.root, 100, str))
        p = self.root / FILENAME
        p.write_text('bad')
        os.chmod(p, 0o600)
        self.assertIn('暂时不可用', render_history(self.root, 100, str))

    def test_dialog_charts_keep_missing_intervals_and_resets_distinct(self):
        from html.parser import HTMLParser
        class Shapes(HTMLParser):
            def __init__(self):
                super().__init__()
                self.lines = []
                self.bars = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'polyline':
                    self.lines.append(attrs['points'])
                if tag == 'rect' and 'usage-column' in attrs.get('class', ''):
                    self.bars.append(attrs)
        rows = [sample('2026-11-01T07:00:00', 10, 100),
                sample('2026-11-02T07:00:00', 20, 200),
                sample('2026-11-05T07:00:00', 30, 300),
                sample('2026-11-06T07:00:00', 1, 2)]
        for row in rows:
            record_sample(self.root, Traffic(row['upload'], row['download'], 0, 0), row['at'])
        html = render_history(self.root, rows[-1]['at'], str)
        self.assertIn('<dialog id="airport-usage-dialog"', html)
        self.assertIn('周期累计用量', html)
        self.assertIn('区间用量', html)
        self.assertIn('<details class="usage-record-details">', html)
        shapes = Shapes()
        shapes.feed(html)
        self.assertEqual([len(points.split()) for points in shapes.lines], [2, 1, 1])
        self.assertEqual(len(shapes.bars), 2)
        self.assertIn('usage-column-gap', shapes.bars[1]['class'])


if __name__ == '__main__':
    unittest.main()
