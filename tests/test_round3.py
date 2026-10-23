"""Shared waits, dependency handoffs, board facts and counted reads on both schemas.

All board/session access uses temporary fixtures. Workflow agents are inert Node stubs;
these tests check orchestration, not provider usage or installed runtime behaviour.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest

from test_kanban_live import KanbanLiveContract, ROOT, SCRIPT, WORKFLOW, insert


class RoundThreeContract:
    # Reuse fixture construction and command helpers, without rerunning the old tests.
    setUp = KanbanLiveContract.setUp
    store_rows = KanbanLiveContract.store_rows
    live_env = KanbanLiveContract.live_env
    run_live = KanbanLiveContract.run_live
    add_events = KanbanLiveContract.add_events
    run_workflow_process = KanbanLiveContract.run_workflow_process
    run_workflow = KanbanLiveContract.run_workflow
    DEFAULT_INITIAL = KanbanLiveContract.DEFAULT_INITIAL
    HARNESS = r"""
const fs = require('fs');
const cp = require('child_process');
const [scriptPath, cfgJson] = process.argv.slice(2);
const cfg = JSON.parse(cfgJson);
const source = fs.readFileSync(scriptPath, 'utf8').replace('export const meta', 'const meta');
const calls = [], logs = [], trace = [];
const runLive = prompt => {
  const cmd = prompt.match(/`([^`]*kanban_live\.py[^`]*)`/)[1]
    .replace(/--max-seconds \d+/g, '--max-seconds 0');
  const out = cp.spawnSync('bash', ['-c', cmd], {encoding: 'utf8', timeout: 10000});
  if (out.error) throw out.error;
  return {code: out.status, data: JSON.parse(out.stdout || '{}')};
};
let eventsSeen = 0, boardsSeen = 0, waitsSeen = 0;
const agent = async (prompt, options) => {
  const call = {prompt, ...options};
  calls.push(call);
  const req = options.schema.required;
  if (req.includes('now')) {
    if (cfg.live) { const r = runLive(prompt); call.exit = r.code; return r.data; }
    if (cfg.boards) return cfg.boards[boardsSeen++];
    return {now: cfg.now === undefined ? 1 : cfg.now, cards: cfg.initial,
            count: cfg.count === undefined ? cfg.initial.length : cfg.count,
            stale_skipped: []};
  }
  if (req.includes('changed')) {
    await new Promise(r => setTimeout(r, 5));
    if (cfg.live || cfg.executeEvents) {
      const r = runLive(prompt); call.exit = r.code; call.result = r.data; return r.data;
    }
    const scripted = (cfg.events || [])[eventsSeen++];
    if (scripted === 'NULL') return null;
    return scripted || {changed: [], new: [], count_changed: 0, count_new: 0, all_finished: true};
  }
  if (req.includes('started')) {
    const ids = [...prompt.matchAll(/--task '(t_\w+)'/g)].map(m => m[1]);
    trace.push({type: 'wait', ids});
    await new Promise(r => setTimeout(r, cfg.waitDelay || 1));
    if (cfg.live) { const r = runLive(prompt); call.exit = r.code; return {...r.data, exit: r.code}; }
    const scripted = (cfg.waits || [])[waitsSeen++];
    if (scripted) return scripted;
    const status = cfg.waitStatus || 'running';
    const exit = cfg.waitExit === undefined ? 0 : cfg.waitExit;
    const idle = exit === 6 || /not[\s_-]*start/i.test(status);
    return {started: idle ? [] : ids,
            cards: idle ? [] : ids.map(id => ({id, title: 'Queued', status, model: 'worker'})),
            not_starting: idle ? ids : [], exit: idle ? 6 : exit};
  }
  if (req.includes('reason')) {
    await new Promise(r => setTimeout(r, cfg.flagDelay || 0));
    return {reason: 'needs input'};
  }
  const id = prompt.match(/Kanban card (t_\w+)/)[1];
  trace.push({type: 'follow', id});
  await new Promise(r => setTimeout(r, (cfg.followDelays || {})[id] || 0));
  const scripted = (cfg.verdicts || {})[id];
  const verdict = Array.isArray(scripted) ? (scripted.length > 1 ? scripted.shift() : scripted[0]) : (scripted || 'DONE');
  trace.push({type: 'report', id, verdict});
  if ((cfg.nullReports || []).includes(id)) return null;
  return {id, verdict, retried: (cfg.reportRetries || []).includes(id), warnings: {},
          model_that_ran: 'worker', summary: 'ok', ...((cfg.reports || {})[id] || {})};
};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('args', 'agent', 'log', source)(cfg.args || {}, agent, message => logs.push(message))
  .then(result => console.log(JSON.stringify(cfg.captureLogs ? {calls, logs, trace, result} : calls)))
  .catch(e => {console.error(e); process.exit(1)});
"""

    def test_snapshot_warnings_count_only_current_run_events(self):
        with sqlite3.connect(self.board_db) as con:
            insert(con, 'task_runs', id=3, task_id='t_run', status='running', started_at=10)
            con.executemany('insert into task_events(task_id,kind,run_id,created_at) values(?,?,?,?)', [
                ('t_run', 'completion_warnings', 1, 1), ('t_run', 'completion_warnings', 1, 2),
                ('t_run', 'completion_warnings', 3, self.now),
                ('t_done', 'completion_warnings', 1, self.now), ('t_run', 'heartbeat', 1, self.now)])
        before = self.board_db.read_bytes()
        out = self.run_live('snapshot', '--board', 'b1', '--task', 't_run')
        self.assertEqual(out.returncode, 0, out.stderr)
        card = json.loads(out.stdout)
        self.assertEqual(card['warnings'], {'completion_warnings': 2})
        self.assertEqual(card['run']['id'], 1)
        self.assertEqual(self.board_db.read_bytes(), before)

    def test_snapshot_warnings_fall_back_only_for_unscoped_events(self):
        kinds = ['completion_warnings', 'protocol_violation', 'suspected_hallucinated_references',
                 'model_fallback', 'timed_out', 'crashed', 'block_loop_detected']
        with sqlite3.connect(self.board_db) as con:
            con.execute('delete from task_events')
            con.execute('update task_runs set started_at=100 where id=1')
            con.executemany("insert into task_events(task_id,kind,created_at) values('t_run',?,100)",
                            [(kind,) for kind in kinds])
            con.execute("insert into task_events(task_id,kind,created_at) values('t_run','completion_warnings',99)")
            con.execute("insert into task_events(task_id,kind,created_at,run_id) values('t_run','completion_warnings',200,99)")
        card = json.loads(self.run_live('snapshot', '--board', 'b1', '--task', 't_run').stdout)
        self.assertEqual(card['warnings'], dict.fromkeys(kinds, 1))
        self.assertEqual(json.loads(self.run_live('snapshot', '--board', 'b1', '--task', 't_wait').stdout)['warnings'], {})
        # A legacy events table without run_id still uses the start boundary.
        spec = importlib.util.spec_from_file_location('_live_warnings', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with sqlite3.connect(':memory:') as con:
            con.execute('create table task_events(task_id text,kind text,created_at int)')
            con.executemany('insert into task_events values(?,?,?)',
                            [('t_run', 'crashed', 99), ('t_run', 'crashed', 100)])
            self.assertEqual(module.run_warnings(con, 't_run', {'id': 1, 'started_at': 100}), {'crashed': 1})

    def test_snapshot_warnings_use_latest_run_when_pointer_is_null(self):
        with sqlite3.connect(self.board_db) as con:
            con.execute("update tasks set current_run_id=null where id='t_run'")
            insert(con, 'task_runs', id=3, task_id='t_run', status='running', started_at=100)
            con.execute("insert into task_events(task_id,kind,created_at) values('t_run','completion_warnings',100)")
        card = json.loads(self.run_live('snapshot', '--board', 'b1', '--task', 't_run').stdout)
        self.assertEqual(card['run']['id'], 3)
        self.assertEqual(card['warnings'], {'completion_warnings': 1})
        with sqlite3.connect(self.board_db) as con:
            con.execute("update tasks set current_run_id=999 where id='t_run'")
        card = json.loads(self.run_live('snapshot', '--board', 'b1', '--task', 't_run').stdout)
        self.assertIsNone(card['run'])
        self.assertEqual(card['warnings'], {})

    def test_graph_card_counts_runs_and_marks_retries_without_writing(self):
        def read():
            out = self.run_live('cards', '--board', 'b1', '--task', 't_run', '--task', 't_wait')
            self.assertEqual(out.returncode, 0, out.stderr)
            return {c['id']: c for c in json.loads(out.stdout)['cards']}
        self.assertEqual(read()['t_run']['runs'], 1)
        self.assertIs(read()['t_run']['retried'], False)
        with sqlite3.connect(self.board_db) as con:
            insert(con, 'task_runs', id=3, task_id='t_run', status='running', started_at=self.now)
        before = self.board_db.read_bytes()
        cards = read()
        self.assertEqual((cards['t_run']['runs'], cards['t_wait']['runs']), (2, 0))
        self.assertIs(cards['t_run']['retried'], True)
        self.assertIs(json.loads(self.run_live('snapshot', '--board', 'b1', '--task', 't_run').stdout)['retried'], True)
        self.assertEqual(self.board_db.read_bytes(), before)
        self.add_events('t_wait', ('blocked', {}), ('unblocked', {}))
        self.assertIs(read()['t_wait']['retried'], True)

    def test_snapshot_of_a_missing_card_has_empty_facts(self):
        out = self.run_live('snapshot', '--board', 'b1', '--task', 't_ghost')
        self.assertEqual(out.returncode, 4)
        self.assertEqual(json.loads(out.stdout), {'id': 't_ghost', 'status': 'NOT_FOUND', 'retried': False, 'warnings': {}})

    def test_wait_start_reports_cap_facts_and_clears_stale_caps(self):
        self.add_events('t_wait', ('capped', {'scope': 'assignee', 'assignee': 'builder', 'running': 2, 'cap': 2}),
                        ('capped_alerted', {}))
        with sqlite3.connect(self.board_db) as con:
            con.execute("update task_events set created_at=? where task_id='t_wait'", (time.time() - 14 * 60,))
        before = self.board_db.read_bytes()
        out = self.run_live('wait-start', '--board', 'b1', '--task', 't_wait', '--max-seconds', '0')
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn('capped builder 2/2 14m', out.stdout)
        self.assertEqual(self.board_db.read_bytes(), before)
        self.add_events('t_wait', ('promoted', {}))
        out = self.run_live('wait-start', '--board', 'b1', '--task', 't_wait', '--max-seconds', '0')
        self.assertNotIn('capped', out.stdout)

    def test_capped_wait_prints_facts_not_a_ticking_minute(self):
        # A deterministic clock crosses a minute without changing the cap facts.
        import contextlib
        import io
        import runpy
        from unittest.mock import patch
        fn = runpy.run_path(str(SCRIPT))['cmd_wait_start']
        clock = [1000.0]
        card = {'id': 't_wait', 'title': 'Waiting', 'status': 'ready', 'assignee': 'worker'}
        def cap(*args):
            return dict(scope='assignee', assignee='worker', running=2, cap=2, alerted=False,
                        label=f'capped worker 2/2 {int(clock[0] - 1000)}m')
        def nap(*args):
            clock[0] += 1
        import argparse
        output = io.StringIO()
        with patch.dict(fn.__globals__, read_card=lambda *a: card, cap_label=cap, nap=nap,
                        board_db=lambda *a: self.board_db), \
                patch('time.time', side_effect=lambda: clock[0]), contextlib.redirect_stdout(output):
            args = argparse.Namespace(board='b1', task='t_wait', max_seconds=3, interval=1, ready_stale=0)
            self.assertEqual(fn(args), 3)
        self.assertEqual(output.getvalue().count('capped worker'), 1)

    def script_copy(self, name, sibling=None, link=False):
        directory = self.home / name
        directory.mkdir()
        if link:
            (directory / 'kanban_live.py').symlink_to(SCRIPT)
        else:
            shutil.copy(SCRIPT, directory / 'kanban_live.py')
        if sibling is not None:
            (directory / 'wave_status.py').write_text(sibling)
        return directory / 'kanban_live.py'

    def test_optional_wave_status_import_is_symlink_safe_and_keeps_json_clean(self):
        for name, sibling in [('missing', None), ('old', 'def snapshot(*a): pass\n'),
                              ('broken', "raise ImportError('boom')\n"),
                              ('noisy', "print('sibling diagnostic')\nraise RuntimeError('boom')\n")]:
            with self.subTest(name=name):
                path = self.script_copy(name, sibling)
                for command in [('snapshot', '--task', 't_run'), ('wait-start', '--task', 't_wait', '--max-seconds', '0')]:
                    out = subprocess.run([sys.executable, str(path), command[0], '--board', 'b1', *command[1:]],
                                         env=self.live_env(), capture_output=True, text=True, timeout=30)
                    self.assertEqual(out.returncode, 0 if command[0] == 'snapshot' else 3, out.stderr)
                    self.assertEqual(out.stderr.count('cap labels off'), 1)
                    if command[0] == 'snapshot':
                        self.assertEqual(json.loads(out.stdout)['id'], 't_run')
                    self.assertNotIn('sibling diagnostic', out.stdout)
        linked = self.script_copy('linked', link=True)
        out = subprocess.run([sys.executable, str(linked), 'snapshot', '--board', 'b1', '--task', 't_run'],
                             env=self.live_env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, '')

    def test_wait_any_returns_all_started_ids_and_keeps_queue(self):
        out = self.run_live('wait-any', '--board', 'b1', '--task', 't_wait', '--task', 't_run',
                            '--task', 't_done', '--task', 't_run', '--max-seconds', '0')
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(set(data['started']), {'t_run', 't_done'})
        self.assertEqual(len(data['cards']), 2)
        out = self.run_live('wait-any', '--board', 'b1', '--task', 't_wait', '--max-seconds', '0')
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(json.loads(out.stdout)['started'], [])

    def test_wait_any_missing_start_priority_and_ready_warning(self):
        out = self.run_live('wait-any', '--board', 'b1', '--task', 't_ghost')
        self.assertEqual(out.returncode, 4, out.stderr)
        self.assertEqual(json.loads(out.stdout)['missing'], ['t_ghost'])
        out = self.run_live('wait-any', '--board', 'b1', '--task', 't_ghost', '--task', 't_run', '--max-seconds', '0')
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual((data['started'], data['missing']), (['t_run'], ['t_ghost']))
        out = self.run_live('wait-any', '--board', 'b1', '--task', 't_wait', '--ready-stale', '1')
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertEqual(json.loads(out.stdout)['not_starting'], ['t_wait'])
        out = self.run_live('wait-start', '--board', 'b1', '--task', 't_wait', '--ready-stale', '1')
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn('status NOT_STARTING', out.stdout)

    def test_wait_any_observes_later_start_without_writing(self):
        proc = subprocess.Popen([sys.executable, str(SCRIPT), 'wait-any', '--board', 'b1', '--task', 't_wait',
                                 '--max-seconds', '3', '--interval', '1'], env=self.live_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.2)
            self.assertIsNone(proc.poll())
            with sqlite3.connect(self.board_db) as con:
                con.execute("update tasks set status='running' where id='t_wait'")
            before = self.board_db.read_bytes()
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)['started'], ['t_wait'])
            self.assertEqual(self.board_db.read_bytes(), before)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_wait_any_rejects_invalid_budgets_and_reports_unreadable_board(self):
        for extra in [('--max-seconds', '271'), ('--max-seconds', '-1'), ('--interval', '0'), ('--ready-stale', '-1')]:
            self.assertEqual(self.run_live('wait-any', '--board', 'b1', '--task', 't_wait', *extra).returncode, 2)
        self.assertEqual(self.run_live('wait-any', '--board', 'nope', '--task', 't_run').returncode, 5)
        self.board_db.write_bytes(b'not a database')
        out = self.run_live('wait-any', '--board', 'b1', '--task', 't_run')
        self.assertEqual(out.returncode, 5, out.stderr)
        self.assertIn('board unreadable', json.loads(out.stdout)['error'])

    def test_cards_skip_old_queued_only_on_implicit_watches(self):
        with sqlite3.connect(self.board_db) as con:
            con.execute('update tasks set created_at=?', (self.now - 8 * 86400,))
            con.execute('update task_events set created_at=?', (self.now - 8 * 86400,))
        data = json.loads(self.run_live('cards', '--board', 'b1').stdout)
        self.assertEqual([c['id'] for c in data['cards']], ['t_run'])
        self.assertEqual(data['stale_skipped'], ['t_wait'])
        self.assertEqual(data['stale_days'], 7)
        for extra in [('--stale-days', '9'), ('--stale-days', '0'), ('--task', 't_wait')]:
            self.assertIn('t_wait', [c['id'] for c in json.loads(self.run_live('cards', '--board', 'b1', *extra).stdout)['cards']])
        self.add_events('t_wait', ('promoted', {}))
        data = json.loads(self.run_live('cards', '--board', 'b1').stdout)
        self.assertEqual(data['stale_skipped'], [])
        for invalid in ['-1', 'nan', 'inf']:
            self.assertEqual(self.run_live('cards', '--board', 'b1', '--stale-days', invalid).returncode, 2)

    def test_events_counts_both_arrays_on_every_exit(self):
        for watch, since, code in [('t_run=ready', '0', 0), ('t_run=ready', str(self.now + 10), 3),
                                   ('t_done=ready', '0', 2)]:
            out = self.run_live('events', '--board', 'b1', '--watch', watch, '--since', since, '--max-seconds', '0')
            self.assertEqual(out.returncode, code, out.stderr)
            data = json.loads(out.stdout)
            self.assertEqual(data['count_changed'], len(data['changed']))
            self.assertEqual(data['count_changed'], 1)
            self.assertEqual(data['count_new'], len(data['new']))

    def test_workflow_shares_waiters_per_phase_and_launches_once(self):
        initial = [dict(id=f't_q{i}', title=f'Queued {i}', status='ready') for i in range(4)]
        data = self.run_workflow(captureLogs=True, initial=initial, args={'follow_new': False, 'phases': [
            {'title': 'Build', 'tasks': ['t_q0', 't_q1', 't_q2']}, {'title': 'Verify', 'tasks': ['t_q3']}]})
        waits = [c for c in data['calls'] if ' wait-any ' in c['prompt']]
        self.assertEqual(len(waits), 2)
        self.assertEqual({c['phase'] for c in waits}, {'Build', 'Verify'})
        self.assertEqual(len([c for c in data['calls'] if ' follow ' in c['prompt']]), 4)

    def test_shared_waiter_rechecks_remaining_ids_and_rejects_foreign_starts(self):
        queued = [dict(id=f't_q{i}', title=f'Q{i}', status='ready') for i in range(3)]
        waits = [{'started': ['t_q0', 't_foreign'], 'cards': [{**queued[0], 'status': 'running'},
                  {'id': 't_foreign', 'status': 'running'}], 'exit': 0}]
        data = self.run_workflow(captureLogs=True, initial=queued, waits=waits, args={'follow_new': False})
        calls = [c for c in data['calls'] if ' wait-any ' in c['prompt']]
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--task 't_q0'", calls[1]['prompt'])
        self.assertEqual({r['id'] for r in data['result']['cards']}, {'t_q0', 't_q1', 't_q2'})

    def test_new_card_in_running_or_stopped_phase_gets_own_waiter(self):
        q0 = dict(id='t_q0', title='Q0', status='ready')
        q1 = dict(id='t_q1', title='Q1', status='ready')
        for stopped in [False, True]:
            data = self.run_workflow(captureLogs=True, initial=[q0], waitDelay=1 if stopped else 60,
                waits=[{'started': [], 'cards': [], 'exit': 3}] if stopped else [],
                events=[{'changed': [], 'new': [q1], 'count_new': 1}],
                args={'phases': [{'title': 'Build', 'tasks': ['t_q0', 't_q1']}]})
            waits = [row for row in data['trace'] if row['type'] == 'wait']
            self.assertEqual(waits, [{'type': 'wait', 'ids': ['t_q0']}, {'type': 'wait', 'ids': ['t_q1']}])
            self.assertEqual(sum(row == {'type': 'follow', 'id': 't_q1'} for row in data['trace']), 1)
            if not stopped:
                self.assertLess(data['trace'].index(waits[1]), data['trace'].index({'type': 'follow', 'id': 't_q0'}))
            else:
                self.assertTrue(any('joined stopped phase' in line for line in data['logs']))

    def test_not_starting_flag_clears_after_its_cards_start(self):
        queued = [dict(id='t_a', title='A', status='ready'), dict(id='t_b', title='B', status='todo')]
        waits = [{'started': [], 'cards': [], 'not_starting': ['t_a'], 'exit': 6},
                 {'started': ['t_a'], 'cards': [{**queued[0], 'status': 'running'}], 'exit': 0},
                 {'started': [], 'cards': [], 'exit': 3}]
        data = self.run_workflow(captureLogs=True, initial=queued, waits=waits, args={'follow_new': False})
        calls = [c for c in data['calls'] if ' wait-any ' in c['prompt']]
        self.assertEqual(len(calls), 3)
        self.assertIn('NOT STARTING', calls[1]['label'])
        self.assertIn('t_a', calls[1]['label'])
        self.assertEqual(calls[1]['phase'], 'Needs you')
        self.assertNotIn('--ready-stale', calls[1]['prompt'])
        self.assertIn('--ready-stale', calls[2]['prompt'])
        self.assertTrue(calls[2]['label'].startswith('⏳ waiting'))

    def test_waiter_missing_ids_and_invalid_statuses_do_not_lose_siblings_or_loop(self):
        queued = [dict(id=f't_q{i}', title=f'Q{i}', status='ready') for i in range(3)]
        data = self.run_workflow(captureLogs=True, initial=queued, args={'follow_new': False},
            waits=[{'started': [], 'cards': [], 'missing': ['t_q2'], 'exit': 4}])
        self.assertEqual(sorted(r['id'] for r in data['result']['cards']), ['t_q0', 't_q1'])
        for status in ['Running', '[running]', ' RUNNING ', 'wibble']:
            data = self.run_workflow(captureLogs=True, initial=[queued[0]], args={'follow_new': False},
                waits=[{'started': ['t_q0'], 'cards': [{**queued[0], 'status': status}], 'exit': 0}])
            self.assertEqual(any(row['type'] == 'follow' for row in data['trace']), status != 'wibble')
        for code in [0, 1, 2, 3, 4, 5]:
            data = self.run_workflow(captureLogs=True, initial=[queued[0]],
                waits=[{'started': [], 'cards': [], 'missing': ['t_other'], 'exit': code}],
                events=[{'changed': [], 'new': [], 'timeout': True}] * 3)
            self.assertEqual(sum(' wait-any ' in c['prompt'] for c in data['calls']), 1)
            self.assertTrue(any('still queued: t_q0' in line for line in data['logs']))

    def test_parent_done_opens_child_after_last_report_and_chains(self):
        initial = [dict(id='t_grand', title='Grand', status='todo', parents=['t_child']),
                   dict(id='t_child', title='Child', status='ready', parents=['t_a', 't_b']),
                   dict(id='t_a', title='A', status='running'), dict(id='t_b', title='B', status='done')]
        data = self.run_workflow(initial=initial, captureLogs=True, followDelays={'t_b': 30}, args={'follow_new': False})
        trace = data['trace']
        self.assertFalse(any(row['type'] == 'wait' for row in trace))
        child = trace.index({'type': 'follow', 'id': 't_child'})
        for parent in ['t_a', 't_b']:
            self.assertLess(trace.index({'type': 'report', 'id': parent, 'verdict': 'DONE'}), child)
        self.assertLess(trace.index({'type': 'report', 'id': 't_child', 'verdict': 'DONE'}),
                        trace.index({'type': 'follow', 'id': 't_grand'}))
        self.assertEqual(len(data['result']['cards']), 4)
        prompt = next(c['prompt'] for c in data['calls'] if 'live view of Kanban card t_child ' in c['prompt'])
        self.assertIn("wait-start --board 'default' --task 't_child' --max-seconds 270 --ready-stale '900'", prompt)

    def test_unsafe_parent_histories_use_waiter(self):
        parent = dict(id='t_parent', title='Parent', status='running')
        child = dict(id='t_child', title='Child', status='todo', parents=['t_parent'])
        cases = [([], ['t_parent']), ([parent], ['t_parent', 't_outside']),
                 ([{**parent, 'retried': True}], ['t_parent']), ([{**parent, 'status': 'blocked'}], ['t_parent'])]
        for parents, links in cases:
            data = self.run_workflow(initial=[{**child, 'parents': links}, *parents], captureLogs=True,
                followDelays={'t_parent': 30}, args={'follow_new': False})
            self.assertIn({'type': 'wait', 'ids': ['t_child']}, data['trace'])
            self.assertEqual(sum(row == {'type': 'follow', 'id': 't_child'} for row in data['trace']), 1)
        for verdict in ['BLOCKED', 'REVIEW', 'ARCHIVED', 'STILL_RUNNING', 'UNKNOWN', 'NOT_FOUND', None]:
            data = self.run_workflow(initial=[parent, child], captureLogs=True,
                verdicts={'t_parent': verdict or 'UNKNOWN'}, nullReports=['t_parent'] if verdict is None else [],
                followDelays={'t_parent': 20}, flagDelay=50, args={'follow_new': False})
            self.assertEqual(data['trace'][2], {'type': 'wait', 'ids': ['t_child']})
        data = self.run_workflow(initial=[parent, child], captureLogs=True, reportRetries=['t_parent'], args={'follow_new': False})
        self.assertEqual(data['trace'][2], {'type': 'wait', 'ids': ['t_child']})

    def test_new_child_uses_parent_report_before_or_after_arrival(self):
        parent = dict(id='t_parent', title='Parent', status='running')
        child = dict(id='t_child', title='Child', status='todo', parents=['t_parent'])
        for delay in [1, 30]:
            data = self.run_workflow(initial=[parent], captureLogs=True, followDelays={'t_parent': delay},
                events=[{'changed': [], 'new': [child], 'count_new': 1}])
            self.assertFalse(any(row['type'] == 'wait' for row in data['trace']))
            self.assertLess(data['trace'].index({'type': 'report', 'id': 't_parent', 'verdict': 'DONE'}),
                            data['trace'].index({'type': 'follow', 'id': 't_child'}))

    def test_stopped_or_missing_parent_waiter_releases_child(self):
        initial = [dict(id='t_p', title='P', status='todo'), dict(id='t_c', title='C', status='todo', parents=['t_p'])]
        for first in [{'started': [], 'cards': [], 'exit': 5},
                      {'started': [], 'cards': [], 'missing': ['t_p'], 'exit': 4}]:
            data = self.run_workflow(initial=initial, captureLogs=True, waits=[first], args={'follow_new': False})
            self.assertEqual([row for row in data['trace'] if row['type'] == 'wait'],
                             [{'type': 'wait', 'ids': ['t_p']}, {'type': 'wait', 'ids': ['t_c']}])
            self.assertEqual([r['id'] for r in data['result']['cards']], ['t_c'])

    def test_stopped_sibling_does_not_release_running_parents_child(self):
        initial = [dict(id='t_p', title='P', status='running'), dict(id='t_q', title='Q', status='todo'),
                   dict(id='t_c', title='C', status='todo', parents=['t_p'])]
        data = self.run_workflow(initial=initial, captureLogs=True, followDelays={'t_p': 60},
            waits=[{'started': [], 'cards': [], 'exit': 5}],
            args={'follow_new': False, 'phases': [{'title': 'Build', 'tasks': ['t_p', 't_q', 't_c']}]})
        self.assertEqual([r for r in data['trace'] if r['type'] == 'wait'], [{'type': 'wait', 'ids': ['t_q']}])
        self.assertLess(data['trace'].index({'type': 'report', 'id': 't_p', 'verdict': 'DONE'}),
                        data['trace'].index({'type': 'follow', 'id': 't_c'}))

    def test_child_fallback_into_running_or_timed_out_waiter_gets_own_waiter(self):
        initial = [dict(id='t_p', title='P', status='running'),
                   dict(id='t_v1', title='V1', status='todo', parents=['t_outside']),
                   dict(id='t_v2', title='V2', status='todo', parents=['t_p'])]
        for timeout in [False, True]:
            data = self.run_workflow(initial=initial, captureLogs=True, verdicts={'t_p': 'BLOCKED'},
                followDelays={'t_p': 5}, waitDelay=60,
                waits=[{'started': [], 'cards': [], 'exit': 3}] if timeout else [],
                args={'follow_new': False, 'phases': [{'title': 'Build', 'tasks': ['t_p']},
                                                     {'title': 'Verify', 'tasks': ['t_v1', 't_v2']}]})
            self.assertIn({'type': 'wait', 'ids': ['t_v2']}, data['trace'])
            self.assertIn('t_v2', [r['id'] for r in data['result']['cards']])

    def test_not_starting_child_returns_to_waiter_without_row_loop(self):
        initial = [dict(id='t_p', title='P', status='running'), dict(id='t_c', title='C', status='ready', parents=['t_p'])]
        waits = [{'started': [], 'cards': [], 'not_starting': ['t_c'], 'exit': 6},
                 {'started': ['t_c'], 'cards': [{'id': 't_c', 'title': 'C', 'status': 'running'}], 'exit': 0}]
        data = self.run_workflow(initial=initial, captureLogs=True, verdicts={'t_c': ['NOT_STARTING', 'DONE']},
                                 waits=waits, args={'follow_new': False})
        self.assertEqual(sum(row == {'type': 'follow', 'id': 't_c'} for row in data['trace']), 2)
        self.assertEqual({r['id']: r['verdict'] for r in data['result']['cards']}, {'t_p': 'DONE', 't_c': 'DONE'})
        waiters = [c for c in data['calls'] if ' wait-any ' in c['prompt']]
        self.assertEqual(len(waiters), 2)
        self.assertIn('NOT STARTING', waiters[1]['label'])
        data = self.run_workflow(initial=[dict(id='t_r', title='R', status='running')], captureLogs=True,
                                 verdicts={'t_r': 'NOT_STARTING'}, args={'follow_new': False})
        self.assertEqual(len(data['result']['cards']), 1)
        self.assertEqual(data['result']['cards'][0]['verdict'], 'NOT_STARTING')

    def test_workflow_warning_facts_and_run_labels_survive_all_ingress(self):
        running = dict(id='t_run', title='Retry', status='running', runs=2)
        for ingress in ['initial', 'wait', 'event']:
            cfg = dict(captureLogs=True, args={'follow_new': ingress == 'event'},
                       reports={'t_run': {'warnings': {'completion_warnings': 2, 'model_fallback': 1}}})
            if ingress == 'initial':
                cfg['initial'] = [running]
            elif ingress == 'wait':
                cfg['initial'] = [{**running, 'status': 'ready', 'runs': 1}]
                cfg['waits'] = [{'exit': 0, 'started': ['t_run'], 'cards': [running]}]
            else:
                cfg['initial'] = [dict(id='t_parent', title='Parent', status='running')]
                cfg['events'] = [{'changed': [], 'new': [running], 'count_new': 1}]
            data = self.run_workflow(**cfg)
            call = next(c for c in data['calls'] if 'live view of Kanban card t_run ' in c['prompt'])
            self.assertIn(' · run 2', call['label'])
            entry = next(r for r in data['result']['cards'] if r['id'] == 't_run')
            self.assertEqual(entry['odd'], 'completion_warnings: 2; model_fallback: 1')
            self.assertIn('⚑ completion_warnings, model_fallback · t_run', data['logs'])
            self.assertIn('snapshot.warnings', call['prompt'])
            self.assertTrue({'retried', 'warnings'} <= set(call['schema']['required']))

    LATE_START_HARNESS = r"""
const fs = require('fs');
const [scriptPath, mode] = process.argv.slice(2);
const source = fs.readFileSync(scriptPath, 'utf8').replace('export const meta', 'const meta');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const gate = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const childOpened = gate(), eventReturned = gate();
let events = 0, rows = 0, waits = 0;
const logs = [];
const p = {id: 't_parent', title: 'Parent', status: 'running', parents: [], runs: 1, retried: false};
const c = {id: 't_child', title: 'Child', status: 'ready', parents: ['t_parent'], runs: 0, retried: false};
const agent = async prompt => {
  if (prompt.includes(' cards --board ')) return {now: 100, cards: [p, c], count: 2, stale_skipped: []};
  if (prompt.includes(' events --board ')) {
    if (events++ === 0) {
      await childOpened.promise;
      // Apply the event to the shared card before the old live row reports NOT_STARTING.
      setImmediate(() => eventReturned.resolve());
      return {changed: [{...c, status: mode}], new: [], count_changed: 1, count_new: 0};
    }
    await eventReturned.promise;
    return {changed: [], new: [], count_changed: 0, count_new: 0, all_finished: true};
  }
  if (prompt.includes(' wait-any ')) {
    waits++;
    return {exit: 0, started: ['t_child'], cards: [{...c, status: 'running'}]};
  }
  if (prompt.includes('live view of Kanban card t_parent ')) {
    return {id: p.id, verdict: 'DONE', warnings: {}, retried: false, model_that_ran: 'stub', summary: 'parent done'};
  }
  if (prompt.includes('live view of Kanban card t_child ')) {
    rows++;
    if (rows === 1) {
      childOpened.resolve();
      await eventReturned.promise;
      return {id: c.id, verdict: 'NOT_STARTING', warnings: {}, retried: false, model_that_ran: 'stub', summary: 'ready-stale'};
    }
    return {id: c.id, verdict: 'DONE', warnings: {}, retried: false, model_that_ran: 'stub', summary: 'child done'};
  }
  if (prompt.includes('stopped as')) return {reason: 'no block reason recorded'};
  throw Error('unexpected ' + prompt);
};
new AsyncFunction('args', 'agent', 'log', source)({follow_new: true}, agent, s => logs.push(s))
  .then(result => console.log(JSON.stringify({rows, waits, logs, verdicts: Object.fromEntries(result.cards.map(r => [r.id, r.verdict]))})))
  .catch(e => { console.error(e); process.exit(1); });
"""

    def test_queued_at_open_child_recovers_after_events_race(self):
        for status in ['running', 'ready']:
            out = subprocess.run(['node', '-', str(WORKFLOW), status], input=self.LATE_START_HARNESS,
                                 capture_output=True, text=True, timeout=60, env=self.live_env())
            self.assertEqual(out.returncode, 0, out.stderr)
            data = json.loads(out.stdout)
            self.assertEqual(data['rows'], 2)
            self.assertEqual(data['waits'], int(status == 'ready'))
            self.assertEqual(data['verdicts']['t_child'], 'DONE')
            self.assertTrue(any(('re-watching now' if status == 'running' else 'back to a waiter') in line
                                for line in data['logs']))

    def test_stop_is_per_card_when_late_parent_has_a_live_waiter(self):
        parent = dict(id='t_p', title='P', status='todo')
        child = dict(id='t_c', title='C', status='todo', parents=['t_p'])
        data = self.run_workflow(captureLogs=True, initial=[dict(id='t_q0', title='Q0', status='todo')],
            waits=[{'started': [], 'cards': [], 'exit': 5},
                   {'started': ['t_p'], 'cards': [{**parent, 'status': 'running'}], 'exit': 0}],
            events=[{'changed': [], 'new': [parent, child], 'count_new': 2},
                    {'changed': [], 'new': [], 'all_finished': True}], waitDelay=60,
            args={'phases': [{'title': 'Build', 'tasks': ['t_q0', 't_p', 't_c']}]})
        self.assertEqual([row for row in data['trace'] if row['type'] == 'wait'],
                         [{'type': 'wait', 'ids': ['t_q0']}, {'type': 'wait', 'ids': ['t_p']}])
        self.assertLess(data['trace'].index({'type': 'report', 'id': 't_p', 'verdict': 'DONE'}),
                        data['trace'].index({'type': 'follow', 'id': 't_c'}))

    def test_stale_skips_are_logged_and_explicit_or_zero_overrides_keep_cards(self):
        with sqlite3.connect(self.board_db) as con:
            con.execute("update tasks set created_at=? where id='t_wait'", (self.now - 8 * 86400,))
            con.execute("update task_events set created_at=? where task_id='t_wait'", (self.now - 8 * 86400,))
            con.execute('delete from task_links')
        data = self.run_workflow(live=True, captureLogs=True, args={'board': 'b1', 'follow_new': False})
        self.assertTrue(any('Skipped queued cards older than 7 days: t_wait' in line for line in data['logs']))
        self.assertFalse(any(' wait-any ' in c['prompt'] for c in data['calls']))
        for extra in [{'stale_days': 0}, {'tasks': ['t_wait']}]:
            data = self.run_workflow(live=True, captureLogs=True, args={'board': 'b1', 'follow_new': False, **extra})
            self.assertTrue(any(' wait-any ' in c['prompt'] for c in data['calls']))
            self.assertFalse(any('Skipped queued' in line for line in data['logs']))
        for value in [-1, '7', None]:
            out = self.run_workflow_process(args={'stale_days': value})
            self.assertEqual(out.returncode, 1)
            self.assertIn('stale_days must be', out.stderr)

    def test_graph_schemas_require_all_board_facts(self):
        calls = self.run_workflow(args={'follow_new': False})
        read = next(c for c in calls if ' cards --board ' in c['prompt'])
        self.assertIn('stale_skipped', read['schema']['required'])
        self.assertTrue({'parents', 'retried', 'runs'} <= set(read['schema']['properties']['cards']['items']['required']))

    def test_workflow_filters_warning_keys_and_shapes_and_clean_runs(self):
        for raw, expected in [({'invented\nlog': 1, 'crashed': 2}, {'crashed': 2}), ('abc', {}),
                              (['completion_warnings'], {}), (None, {}), (7, {}),
                              ({'timed_out': 0, 'model_fallback': 1.5, 'protocol_violation': 3}, {'protocol_violation': 3})]:
            data = self.run_workflow(captureLogs=True, args={'follow_new': False},
                initial=[dict(id='t_run', title='Clean', status='running', runs=1)],
                reports={'t_run': {'warnings': raw}})
            entry = data['result']['cards'][0]
            self.assertEqual(entry['warnings'], expected)
            self.assertNotIn('invented', json.dumps(data['logs']))
            self.assertEqual(entry['odd'], '; '.join(f'{k}: {expected[k]}' for k in sorted(expected)))
            self.assertNotIn(' · run ', next(c['label'] for c in data['calls'] if ' follow ' in c['prompt']))
        spec = importlib.util.spec_from_file_location('_live_schema', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(call_names := next(c['schema']['properties']['warnings']['propertyNames']['enum']
                         for c in data['calls'] if ' follow ' in c['prompt']), list(module.WARNING_KINDS))

    BAD = {'id': 't_bad-id', 'title': 'Bad', 'status': 'ready'}

    def test_counted_board_reread_merges_keeps_earliest_time_and_falls_back(self):
        a = dict(id='t_a', title='A', status='running')
        b = dict(id='t_b', title='B', status='done')
        c = dict(id='t_c', title='C', status='done')
        for second in [None, {'now': 200, 'cards': [{**a, 'title': 'A2'}, c], 'count': 3}]:
            boards = [{'now': 100, 'cards': [a, b, self.BAD], 'count': 3}]
            if second:
                boards.append(second)
            data = self.run_workflow(captureLogs=True, boards=boards, verdicts={'t_a': 'STILL_RUNNING'})
            self.assertEqual(sum(' cards --board ' in x['prompt'] for x in data['calls']), 2)
            self.assertEqual(sorted(r['id'] for r in data['result']['cards']), ['t_a', 't_b'] + (['t_c'] if second else []))
            events = next(x for x in data['calls'] if ' events --board ' in x['prompt'])
            self.assertIn("--since '100'", events['prompt'])
            if second:
                self.assertIn('A2', next(x['label'] for x in data['calls'] if 'live view of Kanban card t_a ' in x['prompt']))
            else:
                self.assertIn('re-read failed; keeping first read', data['logs'])

    def test_counted_reads_report_excess_without_negative_loss(self):
        cards = [dict(id='t_a', title='A', status='done'), dict(id='t_b', title='B', status='done')]
        data = self.run_workflow(captureLogs=True, args={'follow_new': False},
            boards=[{'now': 1, 'cards': cards, 'count': 1}, {'now': 2, 'cards': cards, 'count': 1}])
        self.assertEqual(sum('returned 1 more cards than count 1' in line for line in data['logs']), 2)
        self.assertFalse(any('lost -' in line for line in data['logs']))
        self.assertEqual(len(data['result']['cards']), 2)

    def test_counted_event_reread_checks_both_counts_and_preserves_scope(self):
        parent = dict(id='t_parent', title='Parent', status='running')
        child = dict(id='t_child', title='Child', status='done')
        for field in ['changed', 'new']:
            for recover in [False, True]:
                first = {'changed': [], 'new': [], 'count_changed': 0, 'count_new': 0,
                         field: [self.BAD], f'count_{field}': 1, 'all_finished': True}
                second = {**first, field: [parent if field == 'changed' else child]} if recover else first
                data = self.run_workflow(captureLogs=True, initial=[parent], verdicts={'t_parent': 'STILL_RUNNING'},
                                         events=[first, second])
                reads = [c for c in data['calls'] if ' events --board ' in c['prompt']]
                self.assertEqual(len(reads), 2)
                self.assertIn('--max-seconds 0', reads[1]['prompt'])
                self.assertNotIn('up to 13 times', reads[1]['prompt'])
                self.assertEqual(sum(r['id'] == 't_child' for r in data['result']['cards']), int(field == 'new' and recover))
                import re
                import shlex
                commands = [shlex.split(re.findall(r'`([^`]*kanban_live\.py[^`]*)`', c['prompt'])[0]) for c in reads]
                for flag in ['--board', '--watch', '--known', '--since']:
                    self.assertEqual(commands[0][commands[0].index(flag) + 1], commands[1][commands[1].index(flag) + 1])

    def test_failed_event_reread_keeps_filed_child(self):
        data = self.run_workflow(captureLogs=True, initial=[dict(id='t_parent', title='P', status='running')],
            verdicts={'t_parent': 'STILL_RUNNING'}, events=[
                {'changed': [], 'new': [dict(id='t_child', title='C', status='ready'), self.BAD],
                 'count_changed': 0, 'count_new': 2}, 'NULL'])
        self.assertIn('t_child', [r['id'] for r in data['result']['cards']])
        self.assertIn('re-read failed; keeping first read', data['logs'])

    def test_matching_counts_do_not_reread_or_treat_known_cards_as_loss(self):
        parent = dict(id='t_parent', title='P', status='running')
        data = self.run_workflow(captureLogs=True, initial=[parent], verdicts={'t_parent': 'STILL_RUNNING'},
            events=[{'changed': [parent], 'new': [parent], 'count_changed': 1, 'count_new': 1, 'all_finished': True}])
        self.assertEqual(sum(' cards --board ' in c['prompt'] for c in data['calls']), 1)
        self.assertEqual(sum(' events --board ' in c['prompt'] for c in data['calls']), 1)
        self.assertFalse(any('board read lost' in line for line in data['logs']))


class UpstreamRoundThreeTests(RoundThreeContract, unittest.TestCase):
    FLAVOUR = 'upstream'


class ForkRoundThreeTests(RoundThreeContract, unittest.TestCase):
    FLAVOUR = 'fork'
