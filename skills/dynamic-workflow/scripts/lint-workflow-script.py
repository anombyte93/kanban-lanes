#!/usr/bin/env python3
"""lint-workflow-script.py [--require-header[=TEXT]] SCRIPT.js

Refuse an unrouted Claude Code Workflow (dynamic-workflow) script before you launch it.

Rules (see ../SKILL.md):
  rule0  the script parses (node --check on the body wrapped the way the runtime runs it)
  rule1  every agent() call carries `model` or `agentType`, so no stage inherits the session model
  rule2  the top-tier model (TOP_TIER below) appears on at most one call, and only on a
         judge/verify/synth stage
  rule3  OFF BY DEFAULT. With --require-header, line 1 must start with a marker comment.
         Bare --require-header wants '// WORKFLOW-EXEMPT:'. --require-header=TEXT (or
         --require-header TEXT before the script path) wants TEXT; '// ' is added when TEXT does
         not start with '//'. An empty TEXT ('' or '=') is a usage error. Use it when a
         PreToolUse hook of yours refuses Workflow scripts that lack such a line.
  rule4  `export const meta = {...}` is the first statement; no Date.now(), Math.random(),
         new Date()
  rule7  WARN only, never changes the exit code: a prompt that runs a package install, or names a
         JS test/build runner, with no line about reusing dependencies

Exit codes: 0 PASS, 1 FAIL, 2 UNKNOWN (unreadable file, usage error, or `node` missing so rule0
could not run). Stdlib only, plus the `node` binary for rule0.

Why rule7 exists: in one run, on one machine, verify lanes that copied a tree and then installed
into the copy cost over 1 GB each, and a day of those rounds filled more than 10 GB of scratch
space. Most of those prompts never said "install". They named vitest or tsc, the lane found no
node_modules, and it installed on its own. A hard-link copy (cp -al) of a node_modules that size
costs tens of MB of directory entries.

Why rule0 exists: the other rules read text, not code. A prompt holding an unescaped backtick
inside a template literal passed them, and the Workflow tool then refused the script with
"Script parse error: Unexpected token".
"""
import os, re, shutil, subprocess, sys, tempfile

# The most expensive model alias. Change it here if your model names differ.
TOP_TIER = "fable"
DEFAULT_HEADER = "// WORKFLOW-EXEMPT:"


def strip_template_literals(src: str) -> str:
    # Template literals hold the prompts (parentheses, quotes, anything). Blank them so the paren
    # matcher below only sees code. ${...} inside them is blanked too (it is prompt text).
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "`":
            j = i + 1
            while j < n and src[j] != "`":
                j += 2 if src[j] == "\\" else 1
            out.append("`T`"); i = j + 1
        else:
            out.append(c); i += 1
    return "".join(out)


def template_literals(src: str):
    """Yield (start, text) for each template literal in the raw source (the prompts)."""
    i, n = 0, len(src)
    while i < n:
        if src[i] == "`":
            j = i + 1
            while j < n and src[j] != "`":
                j += 2 if src[j] == "\\" else 1
            yield i, src[i + 1:j]
            i = j + 1
        else:
            i += 1


INSTALL = re.compile(r"\b(npm\s+(?:install|ci|i)|pnpm\s+(?:install|i)|yarn\s+install|bun\s+install)\b")
REUSE = re.compile(
    r"cp\s+-[a-zA-Z]*l|--link\b|hard[- ]?link|ln\s+-s\w*\s+\S*node_modules|shared\s+node_modules"
    r"|--store-dir|reus\w*\s+(?:the\s+)?(?:\w+\s+)?(?:node_modules|dependencies|deps)", re.I)
JS_RUN = re.compile(r"\b(vitest|jest|tsc|npm\s+(?:test|run\s+[\w:-]+)|next\s+build|playwright|(?:pnpm|yarn)\s+(?:test|run\s+[\w:-]+))\b")
NEGATED = re.compile(r"\b(?:do not|don't|never|no need to|instead of|without|not)\b[^.;\n]*$", re.I)


def install_warnings(src: str):
    """rule7: WARN for each prompt literal that runs a package install with no reuse recipe in
    itself or in a ${NAME} it interpolates (one level: `const NAME = `...``)."""
    lits = list(template_literals(src))
    defs = {}
    for start, text in lits:
        m = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*$", src[max(0, start - 80):start])
        if m:
            defs[m.group(1)] = text
    warns = []
    for start, text in lits:
        if REUSE.search(text) or any(REUSE.search(defs.get(name, "")) for name in re.findall(r"\$\{\s*(\w+)\s*\}", text)):
            continue
        line = src.count("\n", 0, start) + 1
        recipe = ("(cp -al hard-link copy, symlinked shared node_modules when the lockfile is identical, "
                  "or pnpm --store-dir); see SKILL.md rule 7")
        addressed, warned = False, False
        for m in INSTALL.finditer(text):
            if NEGATED.search(text[max(0, m.start() - 60):m.start()]):
                addressed = True  # "do not run npm install" is itself a reuse instruction
                continue
            cmd = re.sub(r"\s+", " ", m.group(1))
            warns.append(f"WARN rule7: prompt text at line {line} runs '{cmd}' without a dependency-reuse recipe {recipe}")
            warned = True
            break
        if warned or addressed:
            continue
        m = JS_RUN.search(text)
        if m:
            cmd = re.sub(r"\s+", " ", m.group(1))
            warns.append(f"WARN rule7: prompt text at line {line} names '{cmd}' but says nothing about dependencies; "
                         f"a lane that copies the tree will reinstall node_modules. Add a reuse line {recipe}")
    return warns


def agent_calls(code: str):
    """Yield (start, argtext) for each agent( ... ) call with balanced parens."""
    for m in re.finditer(r"(?<![\w.])agent\s*\(", code):
        depth, j = 1, m.end()
        while j < len(code) and depth:
            if code[j] == "(":
                depth += 1
            elif code[j] == ")":
                depth -= 1
            j += 1
        yield m.start(), code[m.end():j - 1]


def syntax_check(src: str):
    """Parse the script the way the runtime runs it: an async function body (top-level await and
    `return` are legal) whose `export const meta` is a plain const. Returns None when it parses,
    an error line when it does not, or "UNKNOWN: ..." when node is unavailable."""
    node = shutil.which("node")
    if not node:
        return "UNKNOWN: node not found, rule0 (syntax) not checked"
    body = re.sub(r"^(\s*)export\s+const\s+meta\b", r"\1const meta", src, count=1, flags=re.M)
    wrapped = "async function __workflow__() {\n" + body + "\n}\n"
    fd, path = tempfile.mkstemp(suffix=".mjs")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(wrapped)
        res = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)
    if res.returncode == 0:
        return None
    err = res.stderr
    m = re.search(re.escape(path) + r":(\d+)", err)
    where = f" at line {int(m.group(1)) - 1}" if m else ""
    msg = next((l.strip() for l in err.splitlines() if "Error" in l), err.strip().splitlines()[-1] if err.strip() else "parse failed")
    return f"rule0: script does not parse{where}: {msg}"


def header_text(text):
    """Normalise a --require-header value to the line-1 prefix it demands. An empty value is a
    usage error in both forms ('--require-header ""' and '--require-header='), so it can never
    turn into a bare '// ' that any comment satisfies."""
    text = text.strip()
    if not text or text == "//":
        raise ValueError("--require-header TEXT is empty; give a marker or use the bare flag")
    return text if text.startswith("//") else "// " + text


def lint(src: str, require_header=None):
    fails = []
    lines = src.splitlines()
    if not lines:
        return ["UNKNOWN: empty script"], 2
    syntax = syntax_check(src)
    unknown_syntax = syntax is not None and syntax.startswith("UNKNOWN")
    if syntax and not unknown_syntax:
        fails.append(syntax)
    if require_header is not None and not lines[0].startswith(require_header):
        fails.append(f"rule3: line 1 must start with '{require_header}' (asked for by --require-header)")
    code = strip_template_literals(src)
    body = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    first_stmt = re.search(r"\S", body)
    if not first_stmt or not body[first_stmt.start():].startswith("export const meta"):
        fails.append("rule4: 'export const meta = {...}' must be the first statement")
    for bad in ("Date.now(", "Math.random(", "new Date("):
        if bad in code:
            fails.append(f"rule4: {bad}) is not available in workflow scripts")
    calls = list(agent_calls(code))
    if not calls:
        fails.append("UNKNOWN: no agent() calls found")
        return fails, 2
    top = []
    for start, args in calls:
        line = code.count("\n", 0, start) + 1
        has_model = re.search(r"\bmodel\s*:", args) is not None
        has_type = re.search(r"\bagentType\s*:", args) is not None
        if not (has_model or has_type):
            fails.append(f"rule1: agent() at line {line} has no model/agentType (inherits the session model)")
        if re.search(r"\bmodel\s*:\s*['\"]" + re.escape(TOP_TIER) + r"['\"]", args):
            label = re.search(r"\blabel\s*:\s*['\"]([^'\"]*)", args)
            top.append((line, label.group(1) if label else ""))
    if len(top) > 1:
        fails.append(f"rule2: {TOP_TIER} on more than one agent() call: lines " + ", ".join(str(l) for l, _ in top))
    for line, label in top:
        if not re.search(r"judge|verify|synth", label, re.I):
            fails.append(f"rule2: {TOP_TIER} at line {line} is not a judge/verify/synth stage (label {label!r})")
    warns = install_warnings(src)
    if unknown_syntax:
        return fails + [syntax] + warns, (1 if fails else 2)
    return fails + warns, (1 if fails else 0)


USAGE = "usage: lint-workflow-script.py [--require-header[=TEXT]] SCRIPT.js"


def parse_args(argv):
    """Return (script_path, require_header or None). The script path is always the last argument,
    so a bare --require-header never swallows it. Raises ValueError on bad usage."""
    argv = list(argv)
    if argv and argv[0] in ("-h", "--help"):
        raise ValueError("help")
    if not argv:
        raise ValueError("no script given")
    script = argv.pop()
    if script.startswith("--"):
        raise ValueError("the script path must be the last argument")
    header = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--require-header":
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                header = header_text(argv[i + 1]); i += 2
            else:
                header = DEFAULT_HEADER; i += 1
        elif a.startswith("--require-header="):
            header = header_text(a.split("=", 1)[1])
            i += 1
        else:
            raise ValueError(f"unknown argument {a!r}")
    return script, header


def main():
    try:
        path, header = parse_args(sys.argv[1:])
    except ValueError as e:
        print(USAGE)
        if str(e) != "help":
            print(f"UNKNOWN: {e}")
        return 2
    try:
        src = open(path, encoding="utf-8").read()
    except OSError as e:
        print(f"UNKNOWN: {e}"); return 2
    fails, rc = lint(src, header)
    n = len(list(agent_calls(strip_template_literals(src))))
    print(("PASS" if rc == 0 else "FAIL" if rc == 1 else "UNKNOWN") + f": {path} ({n} agent() calls)")
    for f in fails:
        print("  - " + f)
    return rc


if __name__ == "__main__":
    sys.exit(main())
