"""wave_status.py safety contracts, on both board schemas (tests/fixtures/hermes_schema_*.sql).

Temp databases only, no agent processes or signals.
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'wave_status.py'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
spec = importlib.util.spec_from_file_location('wave_status', SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_board(path, flavour):
    con = sqlite3.connect(path)
    with con:
        con.executescript((FIXTURES / f'hermes_schema_{flavour}.sql').read_text())
    return con


def add_task(con, task_id, status, assignee, title='card'):
    """One row literal for both schemas: block_reason is fork-only (a fork trigger requires it)."""
    have = {r[1] for r in con.execute('PRAGMA table_info(tasks)')}
    row = {'id': task_id, 'title': title, 'status': status, 'assignee': assignee, 'created_at': 1,
           'block_reason': 'needs a person' if status == 'blocked' else None}
    row = {k: v for k, v in row.items() if k in have}
    with con:
        con.execute(f"INSERT INTO tasks({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))


class WaveStatusContract:
    FLAVOUR = ''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'board.db'
        con = make_board(self.db, self.FLAVOUR)
        self.addCleanup(con.close)
        for row in [('task-a', 'done', 'worker'), ('task-b', 'review', 'worker'),
                    ('real-human-task', 'blocked', 'human'), ('probe', 'ready', 'worker'),
                    ('later', 'scheduled', 'worker')]:
            add_task(con, *row)
        self.con = con

    def test_explicit_members_not_creator_guess(self):
        report = module.snapshot(self.db, ['task-a', 'real-human-task'])
        self.assertEqual(report['counts'], {'blocked': 1, 'done': 1})
        self.assertTrue(report['quiescent'])
        self.assertFalse(report['all_done'])
        self.assertNotIn('probe', str(report))

    def test_all_done_is_not_delivery_proof(self):
        report = module.snapshot(self.db, ['task-a'])
        self.assertTrue(report['all_done'])
        self.assertEqual(report['delivery'], 'NOT_ASSESSED')

    def test_review_not_done(self):
        report = module.snapshot(self.db, ['task-b'])
        self.assertTrue(report['quiescent'])
        self.assertFalse(report['all_done'])

    def test_scheduled_is_a_known_waiting_state(self):
        report = module.snapshot(self.db, ['later'])
        self.assertEqual(report['counts'], {'scheduled': 1})
        self.assertFalse(report['quiescent'])

    def test_missing_task_never_silent_success(self):
        with self.assertRaises(ValueError):
            module.snapshot(self.db, ['unknown'])

    def test_missing_db_not_created(self):
        missing = self.db.with_name('missing.db')
        with self.assertRaises((OSError, ValueError, sqlite3.Error)):
            module.snapshot(missing, ['task-a'])
        self.assertFalse(missing.exists())

    def test_duplicates_and_empty_refused(self):
        for ids in ([], ['task-a', 'task-a']):
            with self.assertRaises(ValueError):
                module.snapshot(self.db, ids)

    def test_read_only_and_repeatable(self):
        before = self.db.read_bytes()
        first = module.snapshot(self.db, ['task-a'])
        self.assertEqual(first, module.snapshot(self.db, ['task-a']))
        self.assertEqual(before, self.db.read_bytes())

    def test_unknown_state_refused(self):
        with self.con:
            self.con.execute("UPDATE tasks SET status='new-unmapped-state' WHERE id='task-a'")
        with self.assertRaises(ValueError):
            module.snapshot(self.db, ['task-a'])

    def test_wait_returns_when_quiescent_without_sleeping(self):
        slept = []
        report, reached = module.wait_for(self.db, ['task-a', 'task-b'], timeout=60, sleep=slept.append)
        self.assertTrue(reached)
        self.assertTrue(report['quiescent'])
        self.assertEqual(slept, [])

    def test_wait_times_out_with_last_snapshot(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
        report, reached = module.wait_for(self.db, ['probe'], timeout=90, interval=30,
                                          sleep=sleep, clock=lambda: now[0])
        self.assertFalse(reached)
        self.assertEqual(report['counts'], {'ready': 1})
        self.assertEqual(now[0], 90)

    def test_wait_stop_on_blocked_only_when_asked(self):
        ids = ['real-human-task', 'probe']
        report, reached = module.wait_for(self.db, ids, timeout=60, stop_on_blocked=True, sleep=lambda s: None)
        self.assertTrue(reached)
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
        _, reached = module.wait_for(self.db, ids, timeout=30, interval=10, sleep=sleep, clock=lambda: now[0])
        self.assertFalse(reached)

    def test_wait_never_mutates(self):
        before = self.db.read_bytes()
        module.wait_for(self.db, ['task-a'], timeout=5, sleep=lambda s: None)
        self.assertEqual(before, self.db.read_bytes())

    def test_wait_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            module.wait_for(self.db, ['task-a'], timeout=0)

    # The command line, with the board found under $HERMES_HOME.

    def run_cli(self, *args, hermes_home=None):
        hermes = Path(self.tmp.name) / '.hermes'
        board = hermes / 'kanban' / 'boards' / 'b1'
        if not board.exists():
            board.mkdir(parents=True)
            (board / 'kanban.db').write_bytes(self.db.read_bytes())
        env = dict(os.environ, HERMES_HOME=str(hermes_home or hermes), HOME=self.tmp.name)
        env.pop('HERMES_KANBAN_HOME', None)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_cli_snapshot_by_board_under_hermes_home(self):
        out = self.run_cli('--board', 'b1', '--task', 'task-a', '--task', 'probe')
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report['counts'], {'done': 1, 'ready': 1})
        self.assertTrue(report['database'].endswith('/kanban/boards/b1/kanban.db'))

    def test_cli_profile_hermes_home_resolves_to_its_root(self):
        profile = Path(self.tmp.name) / '.hermes' / 'profiles' / 'worker'
        out = self.run_cli('--board', 'b1', '--task', 'task-a', hermes_home=profile)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_cli_real_root_inside_a_directory_named_profiles_stays_the_root(self):
        root = Path(self.tmp.name) / 'profiles' / 'hermes-main'
        board = root / 'kanban' / 'boards' / 'b1'
        board.mkdir(parents=True)
        (board / 'kanban.db').write_bytes(self.db.read_bytes())
        out = self.run_cli('--board', 'b1', '--task', 'task-a', hermes_home=root)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertTrue(json.loads(out.stdout)['database'].startswith(str(root.resolve())))

    def test_cli_wait_reached_and_timeout(self):
        out = self.run_cli('--board', 'b1', '--task', 'task-a', '--task', 'task-b', '--wait', '5', '--interval', '1')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)['wait'], 'REACHED')
        out = self.run_cli('--board', 'b1', '--task', 'probe', '--wait', '1', '--interval', '0.2')
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(json.loads(out.stdout)['wait'], 'TIMEOUT')

    def test_cli_db_still_works_and_a_missing_board_fails_closed(self):
        out = self.run_cli('--db', str(self.db), '--task', 'task-a')
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.run_cli('--board', 'nope', '--task', 'task-a')
        self.assertEqual(out.returncode, 2)
        self.assertEqual(json.loads(out.stdout)['state'], 'UNKNOWN')


class CappedContract:
    """A ready card held back by a fork's concurrency cap reads as capped, not stuck.

    Upstream never writes cap events but has the same task_events table, so the same
    reads must work there (and report capped 0 when no cap event exists)."""
    FLAVOUR = ''
    NOW = 1_800_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'board.db'
        con = make_board(self.db, self.FLAVOUR)
        self.addCleanup(con.close)
        for row in [('cap-a', 'ready', 'builder', 'A'), ('cap-b', 'ready', 'builder', 'B'),
                    ('run-c', 'running', 'builder', 'C'), ('done-d', 'done', 'builder', 'D'),
                    ('plain-e', 'ready', 'worker', 'E'), ('group-f', 'ready', 'builder', 'F')]:
            add_task(con, *row)
        self.con = con

    def event(self, task, kind, payload, minutes_ago):
        with self.con:
            self.con.execute('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)',
                             (task, kind, None if payload is None else json.dumps(payload),
                              self.NOW - minutes_ago * 60))

    def cap(self, task, minutes_ago, scope='assignee', running=2, cap=2, assignee='builder'):
        self.event(task, 'capped', {'assignee': assignee, 'running': running, 'cap': cap, 'scope': scope},
                   minutes_ago)

    def members(self, report):
        return {m['id']: m for m in report['members']}

    def test_no_cap_events_reads_capped_zero(self):
        self.event('cap-a', 'promoted', None, 30)
        report = module.snapshot(self.db, ['cap-a', 'run-c'], now=self.NOW)
        self.assertEqual(report['capped'], 0)
        self.assertIn('capped 0', report['summary'])

    def test_ready_card_with_latest_capped_event_shows_scope_ratio_and_wait(self):
        self.event('cap-a', 'promoted', None, 30)
        self.cap('cap-a', 14)
        report = module.snapshot(self.db, ['cap-a', 'plain-e'], now=self.NOW)
        member = self.members(report)['cap-a']
        self.assertEqual(member['capped']['label'], 'capped builder 2/2 14m')
        self.assertEqual(member['capped']['scope'], 'assignee')
        self.assertEqual(member['capped']['waiting_minutes'], 14)
        self.assertNotIn('capped', self.members(report)['plain-e'])
        self.assertEqual(report['capped'], 1)
        self.assertIn('capped 1', report['summary'])
        self.assertEqual(report['counts'], {'ready': 2})

    def test_alert_after_capped_still_capped_and_marked_alerted(self):
        self.cap('cap-a', 50)
        self.event('cap-a', 'capped_alerted', {'assignee': 'builder'}, 5)
        member = self.members(module.snapshot(self.db, ['cap-a'], now=self.NOW))['cap-a']
        self.assertEqual(member['capped']['label'], 'capped builder 2/2 50m')
        self.assertTrue(member['capped']['alerted'])

    def test_public_cap_label_matches_snapshot_and_is_read_only(self):
        self.cap('cap-a', 14)
        self.cap('run-c', 14)
        self.event('cap-a', 'capped_alerted', {}, 2)
        before = self.db.read_bytes()
        with mock.patch.object(module.time, 'time', return_value=self.NOW):
            capped = module.cap_label(self.con, 'cap-a')
            self.assertEqual(capped['label'], 'capped builder 2/2 14m')
            self.assertTrue(capped['alerted'])
            self.assertIsNone(module.cap_label(self.con, 'run-c'))
            self.assertIsNone(module.cap_label(self.con, 'plain-e'))
        self.assertEqual(self.members(module.snapshot(self.db, ['cap-a'], now=self.NOW))['cap-a']['capped'], capped)
        self.assertEqual(self.db.read_bytes(), before)

    def test_later_non_cap_event_clears_capped(self):
        self.cap('cap-b', 20)
        self.event('cap-b', 'promoted', None, 2)
        report = module.snapshot(self.db, ['cap-b'], now=self.NOW)
        self.assertNotIn('capped', self.members(report)['cap-b'])
        self.assertEqual(report['capped'], 0)

    def test_only_ready_cards_count_as_capped(self):
        self.cap('run-c', 10)
        self.cap('done-d', 10)
        report = module.snapshot(self.db, ['run-c', 'done-d'], now=self.NOW)
        self.assertEqual(report['capped'], 0)
        self.assertTrue(all('capped' not in m for m in report['members']))

    def test_group_scope_names_the_group(self):
        self.cap('group-f', 3, scope='group:gpu', running=4, cap=4)
        member = self.members(module.snapshot(self.db, ['group-f'], now=self.NOW))['group-f']
        self.assertEqual(member['capped']['label'], 'capped group:gpu 4/4 3m')

    def test_unreadable_payload_still_capped_without_failing_snapshot(self):
        with self.con:
            self.con.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('cap-a','capped','not json',?)",
                             (self.NOW - 120,))
        member = self.members(module.snapshot(self.db, ['cap-a'], now=self.NOW))['cap-a']
        self.assertEqual(member['capped']['label'], 'capped ? ?/? 2m')

    def test_capped_read_never_mutates(self):
        self.cap('cap-a', 14)
        before = self.db.read_bytes()
        module.snapshot(self.db, ['cap-a'], now=self.NOW)
        self.assertEqual(before, self.db.read_bytes())


class UpstreamWaveStatusTests(WaveStatusContract, unittest.TestCase):
    FLAVOUR = 'upstream'


class ForkWaveStatusTests(WaveStatusContract, unittest.TestCase):
    FLAVOUR = 'fork'


class UpstreamCappedTests(CappedContract, unittest.TestCase):
    FLAVOUR = 'upstream'


class ForkCappedTests(CappedContract, unittest.TestCase):
    FLAVOUR = 'fork'


class LegacyBoardTests(unittest.TestCase):
    def test_board_without_events_table_reports_zero_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / 'legacy.db'
            con = sqlite3.connect(legacy)
            with con:
                con.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT, assignee TEXT)')
                con.execute("INSERT INTO tasks VALUES('old','ready','builder')")
            con.close()
            report = module.snapshot(legacy, ['old'], now=CappedContract.NOW)
            self.assertEqual(report['capped'], 0)
            self.assertNotIn('capped', report['members'][0])
            self.assertIn('capped 0', report['summary'])


if __name__ == '__main__':
    unittest.main()
