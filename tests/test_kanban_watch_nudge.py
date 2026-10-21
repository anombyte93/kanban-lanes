"""Tests for hooks/kanban-watch-nudge.py (stdlib + pytest; runs the hook as a subprocess)."""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "kanban-watch-nudge.py"


def run(command, stdout, tool="Bash", env=None):
    event = {"tool_name": tool, "tool_input": {"command": command},
             "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False}}
    res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True,
                         text=True, timeout=30, env=env)
    assert res.returncode == 0, res.stderr
    if not res.stdout.strip():
        return None
    return json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


def test_text_create_reports_the_created_id():
    note = run("hermes kanban create 'Build x' --assignee worker", "Created t_1a2b3c4d  (ready, assignee=worker)\n")
    assert "t_1a2b3c4d" in note
    assert "board default" in note


def test_body_ids_are_ignored_in_text_mode():
    cmd = "hermes kanban create 'Fix y' --body 'follow up on t_deadbeef and t_cafef00d' --parent t_00000001"
    note = run(cmd, "Created t_1a2b3c4d  (todo, assignee=-)\n")
    assert "t_1a2b3c4d" in note
    for other in ("t_deadbeef", "t_cafef00d", "t_00000001"):
        assert other not in note


def test_body_ids_are_ignored_in_json_mode():
    task = {"id": "t_1a2b3c4d", "title": "Fix y", "body": "see t_deadbeef", "parents": ["t_00000001"],
            "status": "todo"}
    note = run("hermes kanban --board alpha create 'Fix y' --body 'see t_deadbeef' --json",
               json.dumps(task, indent=2))
    assert "t_1a2b3c4d" in note
    assert "t_deadbeef" not in note and "t_00000001" not in note
    assert "board alpha" in note


def test_only_body_ids_means_no_note():
    # The create failed: nothing printed but an error that quotes the body.
    assert run("hermes kanban create x --body 'about t_deadbeef'", "error: assignee unknown (body: about t_deadbeef)\n") is None


def test_swarm_text_and_json():
    text = "Swarm root: t_aaaaaaa1\nWorkers: t_aaaaaaa2, t_aaaaaaa3\nVerifier: t_aaaaaaa4\nSynthesizer: t_aaaaaaa5\n"
    note = run("hermes kanban swarm 'goal t_deadbeef' --worker a:x --verifier v --synthesizer s", text)
    for i in range(1, 6):
        assert f"t_aaaaaaa{i}" in note
    assert "t_deadbeef" not in note
    js = json.dumps({"root_id": "t_bbbbbbb1", "worker_ids": ["t_bbbbbbb2"], "verifier_id": "t_bbbbbbb3",
                     "synthesizer_id": "t_bbbbbbb4"})
    note = run("hermes kanban swarm goal --json --worker a:x --verifier v --synthesizer s", js)
    for i in range(1, 5):
        assert f"t_bbbbbbb{i}" in note


def task(tid, **extra):
    """The shape `hermes kanban create --json` prints: the task itself, flat."""
    return dict({"id": tid, "title": "x", "status": "todo"}, **extra)


def test_several_json_values_from_a_loop():
    out = json.dumps(task("t_11111111", body="t_99999999")) + "\n" + json.dumps(task("t_22222222")) + "\n"
    note = run("for t in a b; do hermes kanban create $t --json; done", out)
    assert "t_11111111" in note and "t_22222222" in note and "t_99999999" not in note


def test_twelve_hex_ids_are_reported():
    note = run("hermes kanban create x --json", json.dumps(task("t_1a2b3c4d5e6f")))
    assert "t_1a2b3c4d5e6f" in note
    note = run("hermes kanban create x", "Created t_1a2b3c4d5e6f  (ready, assignee=-)\n")
    assert "t_1a2b3c4d5e6f" in note


def test_a_later_show_is_not_a_new_card():
    # show --json wraps the task: {"task": {...}, "parents": [...], ...}
    shown = {"task": task("t_00000001"), "latest_summary": None, "parents": [], "children": []}
    out = json.dumps(task("t_1a2b3c4d")) + "\n" + json.dumps(shown) + "\n"
    note = run("hermes kanban create x --json && hermes kanban show t_00000001 --json", out)
    assert "t_1a2b3c4d" in note and "t_00000001" not in note


def test_a_later_task_shaped_value_is_capped_by_the_call_count():
    # One create call, two task-shaped values: only the first is the create result.
    out = json.dumps(task("t_1a2b3c4d")) + "\n" + json.dumps(task("t_00000001")) + "\n"
    note = run("hermes kanban create x --json && some-other-tool --json", out)
    assert "t_1a2b3c4d" in note and "t_00000001" not in note


def test_a_later_list_is_not_a_new_card():
    out = json.dumps(task("t_1a2b3c4d")) + "\n" + json.dumps([task("t_00000001"), task("t_00000002")]) + "\n"
    note = run("hermes kanban create x --json; hermes kanban list --json", out)
    assert "t_1a2b3c4d" in note and "t_00000001" not in note and "t_00000002" not in note


def test_silent_for_other_commands_and_help():
    assert run("hermes kanban list", "Created t_1a2b3c4d\n") is None
    assert run("hermes kanban create --help", "Created t_1a2b3c4d\n") is None
    assert run("ls", "Created t_1a2b3c4d\n", tool="Read") is None


def test_board_variable_is_named_not_guessed():
    note = run('hermes kanban --board "$BOARD" create x', "Created t_1a2b3c4d  (ready, assignee=-)\n")
    assert "<the board in $BOARD>" in note


def test_workflow_path_honours_claude_config_dir(tmp_path):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp_path))
    note = run("hermes kanban create x", "Created t_1a2b3c4d\n", env=env)
    assert os.path.join(str(tmp_path), "workflows", "kanban-watch.js") in note


def test_bad_stdin_is_ignored():
    res = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and res.stdout == ""
