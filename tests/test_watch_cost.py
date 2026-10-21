"""Known-answer accounting through the public CLI; no live board access."""
import json
import os
import runpy
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "watch_cost.py"
FIXTURES = HERE / "fixtures" / "watch_cost"


def fixture_root(tmp_path):
    run = tmp_path / "project" / "session" / "subagents" / "workflows" / "run_fixture"
    run.mkdir(parents=True)
    shutil.copy(FIXTURES / "journal.jsonl", run / "journal.jsonl")
    for agent in ("follow", "cards", "non-watcher"):
        shutil.copy(FIXTURES / (agent + ".jsonl"), run / ("agent-" + agent + ".jsonl"))
    return run


def run_root(tmp_path, run_id, *agents):
    """One run directory holding the named fixtures as agent transcripts (plus any meta)."""
    run = tmp_path / "project" / "session" / "subagents" / "workflows" / run_id
    run.mkdir(parents=True)
    for agent in agents:
        shutil.copy(FIXTURES / (agent + ".jsonl"), run / ("agent-" + agent + ".jsonl"))
        meta = FIXTURES / (agent + ".meta.json")
        if meta.exists():
            shutil.copy(meta, run / ("agent-" + agent + ".meta.json"))
    return run


def invoke(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True)


def test_exact_totals_per_role_and_model(tmp_path):
    fixture_root(tmp_path)
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert set(report) == {"run_fixture"}
    assert set(report["run_fixture"]) == {"follow", "cards"}
    follow = report["run_fixture"]["follow"]["claude-sonnet-5"]
    assert follow == {
        "agents": 1, "turns": 2, "input_tokens": 30, "output_tokens": 7,
        "cache_read_input_tokens": 300, "cache_write_5m_tokens": 1000,
        "cache_write_1h_tokens": 200, "cold_wakes": 1,
        "cold_cache_write_5m_tokens": 0, "cold_cache_write_1h_tokens": 200,
        "weighted_total": 1745.0,
    }
    assert report["run_fixture"]["cards"]["claude-haiku-5-5"] == {
        "agents": 1, "turns": 1, "input_tokens": 7, "output_tokens": 2,
        "cache_read_input_tokens": 50, "cache_write_5m_tokens": 40,
        "cache_write_1h_tokens": 0, "cold_wakes": 0,
        "cold_cache_write_5m_tokens": 0, "cold_cache_write_1h_tokens": 0,
        "weighted_total": 72.0,
    }


def test_text_has_one_row_per_run_and_model_never_summed_across_models(tmp_path):
    # WEIGHTED is per-model input equivalents with no cross-model price factor,
    # so Haiku and Sonnet figures must never be added into one number.
    fixture_root(tmp_path)
    proc = invoke(tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0].split() == [
        "RUN", "MODEL", "AGENTS", "TURNS", "INPUT", "OUTPUT", "CACHE_READ", "WRITE_5M",
        "WRITE_1H", "COLD", "COLD_5M", "COLD_1H", "WEIGHTED",
    ]
    haiku = ["claude-haiku-5-5", "1", "1", "7", "2", "50", "40", "0", "0", "0", "0", "72.00"]
    sonnet = ["claude-sonnet-5", "1", "2", "30", "7", "300", "1000", "200", "1", "0", "200", "1745.00"]
    assert [line.split() for line in lines[1:]] == [
        ["run_fixture", *haiku], ["run_fixture", *sonnet], ["TOTAL", *haiku], ["TOTAL", *sonnet],
    ]
    assert "1817.00" not in proc.stdout


def test_run_filters_are_repeatable(tmp_path):
    run = fixture_root(tmp_path)
    shutil.copytree(run, run.with_name("run_second"))
    shutil.copytree(run, run.with_name("run_excluded"))
    proc = invoke(tmp_path, "--json", "--run", "run_fixture", "--run", "run_second")
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)) == {"run_fixture", "run_second"}


@pytest.mark.parametrize("fresh_mtime,fresh_timestamp,expected", [
    (False, False, False), (True, False, True),
    (False, True, True), (True, True, True),
])
def test_since_uses_mtime_or_first_timestamp(tmp_path, fresh_mtime, fresh_timestamp, expected):
    run = fixture_root(tmp_path)
    for path in run.glob("agent-*.jsonl"):
        if fresh_timestamp:
            path.write_text(path.read_text().replace("2026-10-20", "2026-10-21"))
        stamp = datetime(2026, 10, 21 if fresh_mtime else 19, tzinfo=timezone.utc).timestamp()
        os.utime(path, (stamp, stamp))
    proc = invoke(tmp_path, "--json", "--since", "2026-10-21")
    assert proc.returncode == 0, proc.stderr
    assert bool(json.loads(proc.stdout)) is expected


def test_help_documents_all_weights_and_accounting_assumptions():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    for text in ("input=1.0", "cache_write_5m=1.25", "cache_write_1h=2.0", "cache_read=0.1",
                 "claude-haiku=5.0", "claude-sonnet=5.0", "claude-opus=5.0",
                 "not dollars", "message ID", "300", "aggregate-only", "UTC",
                 "kanban-watcher", "previous distinct assistant message", "never summed across models"):
        assert text in proc.stdout
    assert "including tool-result user lines" not in proc.stdout
    assert "first named subcommand" not in proc.stdout


def append_row(path, row):
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def test_synthetic_zero_usage_error_rows_are_not_api_turns(tmp_path):
    run = fixture_root(tmp_path)
    append_row(run / "agent-cards.jsonl", {
        "type": "assistant", "timestamp": "2026-10-20T00:10:00Z",
        "message": {"id": "error", "model": "<synthetic>",
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    })
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    models = json.loads(proc.stdout)["run_fixture"]["cards"]
    assert set(models) == {"claude-haiku-5-5"}
    assert models["claude-haiku-5-5"]["turns"] == 1


@pytest.mark.parametrize("role", ["follow", "wait-start", "events", "cards", "snapshot", "active",
                                  "scout", "wave_status"])
def test_all_roles_use_first_command_not_later_mentions(tmp_path, role):
    run = fixture_root(tmp_path)
    path = run / "agent-cards.jsonl"
    script = "wave_status.py" if role == "wave_status" else "kanban_live.py " + role
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["message"]["content"] = f"python3 '{script}' --board default; kanban_live.py follow"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    models = json.loads(proc.stdout)["run_fixture"][role]
    assert models["claude-haiku-5-5"]["agents"] == 1


@pytest.mark.parametrize("gap,cold", [(300, 0), (301, 1)])
def test_cold_wake_is_measured_from_previous_api_call_not_tool_result(tmp_path, gap, cold):
    # The previous assistant turn (the API call) is at T = 00:00:01. A tool_result
    # arrives at T+299, seconds before the next call, and must not reset the clock.
    run = fixture_root(tmp_path)
    path = run / "agent-cards.jsonl"
    append_row(path, {"type": "user", "timestamp": "2026-10-20T00:05:00Z",
                      "message": {"content": [{"type": "tool_result", "content": "ready"}]}})
    append_row(path, {"type": "assistant", "timestamp": f"2026-10-20T00:05:{gap - 299:02d}Z",
                      "message": {"id": "wake", "model": "claude-haiku-5-5",
                                  "usage": {"input_tokens": 1, "output_tokens": 1}}})
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    stats = json.loads(proc.stdout)["run_fixture"]["cards"]["claude-haiku-5-5"]
    assert stats["cold_wakes"] == cold


def test_unknown_model_fails_instead_of_inventing_weight(tmp_path):
    run = fixture_root(tmp_path)
    path = run / "agent-cards.jsonl"
    path.write_text(path.read_text().replace("claude-haiku-5-5", "unpriced-model"))
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 2
    assert not proc.stdout
    assert "no output ratio" in proc.stderr


def test_corrupt_journal_metadata_warns_but_does_not_lose_transcripts(tmp_path):
    run = fixture_root(tmp_path)
    with (run / "journal.jsonl").open("a") as stream:
        stream.write('{"type":"result","result":"interleaved {"type":"started"}\n')
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert "warning" in proc.stderr and "journal.jsonl:5" in proc.stderr
    assert json.loads(proc.stdout)["run_fixture"]["follow"]["claude-sonnet-5"]["weighted_total"] == 1745


def test_corrupt_watcher_transcript_fails_instead_of_reporting_partial_cost(tmp_path):
    run = fixture_root(tmp_path)
    with (run / "agent-cards.jsonl").open("a") as stream:
        stream.write('{"type":"assistant"\n')
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 2
    assert not proc.stdout
    assert "invalid JSON" in proc.stderr


def test_since_skips_stale_corrupt_tail_using_first_timestamp(tmp_path):
    run = fixture_root(tmp_path)
    for path in run.glob("agent-*.jsonl"):
        with path.open("a") as stream:
            stream.write('{"incomplete":\n')
        old = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
        os.utime(path, (old, old))
    proc = invoke(tmp_path, "--json", "--since", "2026-10-21")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}


def test_harness_relay_first_row_does_not_hide_the_computed_task(tmp_path):
    run = fixture_root(tmp_path)
    for agent in ("follow", "cards"):
        path = run / ("agent-" + agent + ".jsonl")
        original = path.read_text()
        first = {"type": "user", "timestamp": "2026-10-20T00:00:00Z",
                 "message": {"content": "[Workflow harness — user request] Please improve the workflow."}}
        path.write_text(json.dumps(first) + "\n" + original)
    proc = invoke(tmp_path, "--json", "--run", "run_fixture")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)["run_fixture"]
    assert report["follow"]["claude-sonnet-5"]["weighted_total"] == 1745
    assert report["cards"]["claude-haiku-5-5"]["agents"] == 1


def test_relay_row_then_computed_task_is_classified_from_the_task(tmp_path):
    run_root(tmp_path, "run_relay", "relay")
    proc = invoke(tmp_path, "--json", "--run", "run_relay")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"run_relay": {"cards": {"claude-haiku-5-5": {
        "agents": 1, "turns": 1, "input_tokens": 10, "output_tokens": 2,
        "cache_read_input_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
        "cold_wakes": 0, "cold_cache_write_5m_tokens": 0, "cold_cache_write_1h_tokens": 0,
        "weighted_total": 20.0}}}}


def test_explicit_run_with_no_watchers_warns_and_reports_zero(tmp_path):
    run_root(tmp_path, "run_none", "non-watcher", "reviewer")
    proc = invoke(tmp_path, "--json", "--run", "run_none")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"run_none": {}}
    assert "run_none: 0 watcher agents among 2 transcripts" in proc.stderr
    text = invoke(tmp_path, "--run", "run_none")
    assert text.returncode == 0, text.stderr
    assert text.stdout.splitlines()[1].split() == ["run_none", "-", *(["0"] * 10), "0.00"]


def test_missing_requested_run_fails_instead_of_fabricating_zero(tmp_path):
    fixture_root(tmp_path)
    proc = invoke(tmp_path, "--run", "run_does_not_exist")
    assert proc.returncode == 2
    assert not proc.stdout
    assert "requested run not found" in proc.stderr


def test_script_reference_without_watcher_role_is_excluded_with_warning(tmp_path):
    run = fixture_root(tmp_path)
    path = run / "agent-cards.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["message"]["content"] = "Review docs and run `python3 kanban_live.py --help`."
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert "warning" in proc.stderr and "no recognised role" in proc.stderr
    assert str(path) in proc.stderr
    assert set(json.loads(proc.stdout)["run_fixture"]) == {"follow"}


def test_role_is_the_subcommand_after_the_script_not_earlier_prose(tmp_path):
    run = fixture_root(tmp_path)
    path = run / "agent-cards.jsonl"
    path.write_text(path.read_text().replace("python3 kanban_live.py cards --board default",
                                          "Use events first: python3 kanban_live.py cards; then follow"))
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)["run_fixture"]) == {"follow", "cards"}


def test_card_title_role_word_before_command_does_not_pick_the_role(tmp_path):
    run_root(tmp_path, "run_titled", "titled")
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)["run_titled"]) == {"follow"}


def test_card_title_naming_the_script_does_not_pick_the_role(tmp_path):
    run = run_root(tmp_path, "run_title_script", "titled")
    path = run / "agent-titled.jsonl"
    path.write_text(path.read_text().replace('(\\"rehome scout, fixes\\")',
                                          '(\\"map kanban_live.py subcommands\\")'))
    assert "map kanban_live.py subcommands" in path.read_text()
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)["run_title_script"]) == {"follow"}


def test_scout_subcommand_is_a_role_not_excluded(tmp_path):
    run_root(tmp_path, "run_scout", "scout")
    proc = invoke(tmp_path, "--json", "--run", "run_scout")
    assert proc.returncode == 0, proc.stderr
    assert "warning" not in proc.stderr
    assert json.loads(proc.stdout)["run_scout"]["scout"]["claude-haiku-5-5"]["agents"] == 1


def test_reviewer_that_mentions_script_and_role_word_is_not_a_watcher(tmp_path):
    run_root(tmp_path, "run_review", "reviewer", "cards")
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["run_review"] == {"cards": {"claude-haiku-5-5": {
        "agents": 1, "turns": 1, "input_tokens": 7, "output_tokens": 2,
        "cache_read_input_tokens": 50, "cache_write_5m_tokens": 40, "cache_write_1h_tokens": 0,
        "cold_wakes": 0, "cold_cache_write_5m_tokens": 0, "cold_cache_write_1h_tokens": 0,
        "weighted_total": 72.0}}}


def test_meta_agent_type_is_the_primary_selector(tmp_path):
    # reviewer-cmd runs kanban_live.py in command form but its meta says it is a
    # build worker; meta-watcher never names the script but meta says kanban-watcher.
    run = run_root(tmp_path, "run_meta", "reviewer-cmd", "meta-watcher")
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)["run_meta"]) == {"(unknown)"}
    assert json.loads(proc.stdout)["run_meta"]["(unknown)"]["claude-haiku-5-5"]["agents"] == 1
    assert str(run / "agent-meta-watcher.jsonl") in proc.stderr
    assert "kanban-watcher" in proc.stderr


def test_corrupt_non_watcher_transcript_does_not_fail_the_report(tmp_path):
    # No user row, then a truncated line: classified as not-a-watcher at its first
    # reply, so the corrupt tail is never parsed.
    run_root(tmp_path, "run_corrupt", "cards", "corrupt-other")
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)["run_corrupt"]) == {"cards"}


def test_corrupt_line_before_classification_warns_with_path_and_skips(tmp_path):
    run = run_root(tmp_path, "run_corrupt", "cards")
    live = run / "agent-live.jsonl"
    live.write_text('{"type":"user","message":{"content":"Run python3 kanban_live.py fol')
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert str(live) in proc.stderr and "unclassified" in proc.stderr
    assert set(json.loads(proc.stdout)["run_corrupt"]) == {"cards"}


def test_corrupt_watcher_error_names_the_path_once(tmp_path):
    run = fixture_root(tmp_path)
    path = run / "agent-cards.jsonl"
    with path.open("a") as stream:
        stream.write('{"type":"assistant"\n')
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 2
    assert proc.stderr.count(str(path)) == 1


def test_realistic_tool_result_before_cold_wake_counts_the_write(tmp_path):
    run_root(tmp_path, "run_cold", "cold-wake")
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    stats = json.loads(proc.stdout)["run_cold"]["follow"]["claude-sonnet-5-5"]
    assert stats["cold_wakes"] == 1
    assert stats["cold_cache_write_5m_tokens"] == 9009


def test_model_switch_counts_agent_once_in_text_but_once_per_model_in_json(tmp_path):
    run = fixture_root(tmp_path)
    append_row(run / "agent-cards.jsonl", {
        "type": "assistant", "timestamp": "2026-10-20T00:00:02Z",
        "message": {"id": "switched", "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 1, "output_tokens": 1}},
    })
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    models = json.loads(proc.stdout)["run_fixture"]["cards"]
    assert {model: stats["agents"] for model, stats in models.items()} == {
        "claude-haiku-5-5": 1, "claude-opus-5-5": 1,
    }
    text = invoke(tmp_path)
    assert text.returncode == 0, text.stderr
    totals = {line.split()[1]: line.split() for line in text.stdout.splitlines()
              if line.startswith("TOTAL ")}
    assert set(totals) == {"claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5"}
    assert totals["claude-opus-5-5"][2:4] == ["1", "1"]
    assert totals["claude-opus-5-5"][-1] == "6.00"
    assert totals["claude-haiku-5-5"][2:4] == ["1", "1"]
    assert "1823.00" not in text.stdout


def test_idless_rows_are_individual_turns(tmp_path):
    run = fixture_root(tmp_path)
    row = {"type": "assistant", "timestamp": "2026-10-20T00:00:02Z",
           "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 1}}}
    for _ in range(2):
        append_row(run / "agent-cards.jsonl", row)
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["run_fixture"]["cards"]["claude-haiku-5-5"]["turns"] == 3


def test_inconsistent_cache_breakdown_fails(tmp_path):
    run = fixture_root(tmp_path)
    path = run / "agent-follow.jsonl"
    path.write_text(path.read_text().replace('"cache_creation_input_tokens":1000',
                                          '"cache_creation_input_tokens":999'))
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 2
    assert "breakdown disagrees" in proc.stderr
    assert not proc.stdout


@pytest.mark.parametrize("args", [("--since", "yesterday"), ("--since", "2026-13-01")])
def test_invalid_date_is_cli_error(tmp_path, args):
    fixture_root(tmp_path)
    proc = invoke(tmp_path, *args)
    assert proc.returncode == 2
    assert not proc.stdout


def test_missing_root_is_error(tmp_path):
    proc = invoke(tmp_path / "absent", "--json")
    assert proc.returncode == 2
    assert "root directory not found" in proc.stderr
    assert not proc.stdout


def test_report_opens_only_jsonl_in_read_mode_never_kanban_db(tmp_path, monkeypatch):
    run = fixture_root(tmp_path)
    (tmp_path / "kanban.db").write_bytes(b"not a database; must not be opened")
    module = runpy.run_path(str(SCRIPT))
    original_open = Path.open
    opened = []

    def read_only_open(path, mode="r", *args, **kwargs):
        assert mode == "r"
        assert path.suffix == ".jsonl"
        opened.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_only_open)
    report, _ = module["report"](tmp_path)
    assert report["run_fixture"]["follow"]["claude-sonnet-5"]["turns"] == 2
    assert set(opened) == {run / "journal.jsonl", run / "agent-follow.jsonl",
                           run / "agent-cards.jsonl", run / "agent-non-watcher.jsonl"}


def test_cold_5m_writes_are_subset_not_additional_charge(tmp_path):
    run = fixture_root(tmp_path)
    path = run / "agent-follow.jsonl"
    path.write_text(path.read_text().replace(
        '"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":200',
        '"ephemeral_5m_input_tokens":200,"ephemeral_1h_input_tokens":0'))
    proc = invoke(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    stats = json.loads(proc.stdout)["run_fixture"]["follow"]["claude-sonnet-5"]
    assert stats["cold_wakes"] == 1
    assert stats["cold_cache_write_5m_tokens"] == 200
    assert stats["cold_cache_write_1h_tokens"] == 0
    assert stats["cache_write_5m_tokens"] == 1200
    assert stats["weighted_total"] == 1595


def test_quoted_script_path_with_spaces_keeps_its_role():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "watch_cost_quoted", Path(__file__).resolve().parent.parent / "scripts" / "watch_cost.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.watcher_role("f", 'Run `python3 "a path/kanban_live.py" follow --board b`', False) == "follow"
    assert module.watcher_role("f", "Run python3 '/x y/wave_status.py' --db d", False) == "wave_status"
    # A quoted path may hold the other quote character.
    assert module.watcher_role("f", 'Run `python3 "it\'s here/kanban_live.py" follow`', False) == "follow"
    assert module.watcher_role("f", "Run python3 'say \"hi\"/kanban_live.py' events", False) == "events"
    assert module.watcher_role("f", "the card is named kanban_live.py follow", False) is None
