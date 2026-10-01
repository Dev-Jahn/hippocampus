#!/usr/bin/env python3
"""Digest of a single transcript (.jsonl) for the scribe: kept by role, bounded by one budget.

Reads a Claude Code transcript or a codex rollout and writes one line per entry in the same
vocabulary for both (USER/ASSIST/TOOL/RES/RES-ERR/COMPACTION), each behind the "[N]" number of
the transcript line it came from. `thinking` and codex `reasoning` are excluded.

What the scribe records comes from a few kinds of content, and those are kept whole, never cut
(DESIGN §3.5 step 2): user messages (task notifications included), assistant text, compaction
summaries, every hippo call and its output, Agent/Task/Workflow launches and their reports, and
the git commands that are outcome signals (commit, merge, tag, push, cherry-pick, revert) with
their output. Every other tool call is one line saying what was done — the tool and its target —
and its result is `ok` or the error's line. That bulk is tool I/O main already put in its own words.

One budget bounds the whole digest. Over it, entries are left out oldest-first, one-line ones
before whole ones, and an OMITTED line at the top says how many and how many chars.

stdlib only. Usage:
  digest_lite.py <transcript.jsonl> [--since-line N] [--until-line M] [--budget CHARS]

--since-line/--until-line bound the window to original line numbers (N, M].
The caller (scribe) pins the upper bound to the same line count it will store
as the cursor, so lines appended while the digest runs are left for the next
window instead of being summarized twice.

If the digest contains no substantive content (no TOOL line and no non-meta
USER line) after --since-line, nothing is printed and exit code is 0 — this
is the deterministic prefilter scribe uses to skip the clerk call entirely.
"""
import argparse
import json
import re
import sys

# The budget for one window's digest, in characters. Sized from the clerk's context: its
# smaller backend (claude, 200k tokens; codex gpt-6-luna has 258k) less ~50k tokens for the
# prompt, the rosters and the reply leaves ~150k tokens, and at 2 characters per token — under
# the 2.6-2.8 measured on real scribe payloads, because a digest without tool output is
# denser in CJK text — that is 300k characters.
DIGEST_BUDGET_CHARS = 300_000
# A one-line entry's display bound: it says what was done, so it never needs more.
ONE_LINE_CHARS = 300

USER_META_PAT = re.compile(
    r"<(local-command-caveat|command-name|local-command-stdout|command-message|command-args)>"
)
# Host-injected context on a codex user message — not the user's words.
CODEX_INJECTED = re.compile(
    r"^\s*(?:# AGENTS\.md instructions|<environment_context>|<recommended_plugins>"
    r"|<user_instructions>|<user_shell_command>)"
)
# hippo run as a command — at the start, after a separator or an opening quote, behind env
# assignments or a path; never a mere path or argument that contains the word.
HIPPO_CALL = re.compile(
    r"(?:^|[;&|(`\"'])\s*(?:\w+=\S*\s+)*(?:\S*/)?hippo(?=\s|$|[;&|)`\"'])|\bhippo_cli\.py\b",
    re.M,
)
GIT_OUTCOME = re.compile(
    r"\bgit\s+(?:-[Cc]\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*"
    r"(?:commit|merge|tag|push|cherry-pick|revert)(?![\w-])|\bgh\s+pr\s+merge\b"
)
# Claude Code tools whose call and result are kept whole: delegation, and the question whose
# answer is the user's own words.
WHOLE_TOOLS = {"Agent", "Task", "Workflow", "SendMessage", "AskUserQuestion"}
# codex's delegation tools (namespace `collaboration` on recent rollouts).
CODEX_AGENT_TOOLS = {"spawn_agent", "send_message", "followup_task", "wait_agent", "list_agents"}
EXIT_HEADER = re.compile(r"(?:Process exited with code|Exit code:?)\s*(-?\d+)")
CODEX_NOISE = re.compile(
    r"^(?:Chunk ID:|Wall time|Process exited with code|Original token count:|Output:$"
    r"|Exit code:?\s*-?\d+$|Script completed$)"
)


def one_line(s):
    return re.sub(r"\n+", " ⏎ ", str(s or "").replace("\r", "").strip())


def clip(s, n=ONE_LINE_CHARS):
    s = one_line(s)
    return s if len(s) <= n else s[:n] + " …"


def whole_command(cmd):
    """A shell command whose call and output the scribe reads whole: a hippo call, or a git
    command that is an outcome signal — wherever it sits in the command."""
    return bool(HIPPO_CALL.search(cmd) or GIT_OUTCOME.search(cmd))


def first_line(s):
    lines = [ln for ln in str(s or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return lines[0].strip() + (f" …(+{len(lines) - 1} lines)" if len(lines) > 1 else "")


def error_line(body, code=None):
    """The one line an error is shown by. A shell exit carries its verdict last (a test
    summary, a traceback's exception), so behind an exit code it is the last line; any other
    error is its first."""
    lines = [ln.strip() for ln in str(body or "").splitlines() if ln.strip()]
    if code is None and lines:
        m = re.fullmatch(r"Exit code (-?\d+)", lines[0])
        if m:
            code, lines = m.group(1), lines[1:]
    if code is not None:
        lines = [ln for ln in lines if not CODEX_NOISE.match(ln)]
        return clip(f"exit {code}" + (f": {lines[-1]}" if lines else ""))
    return clip(lines[0] if lines else "(no output)")


class Window:
    """The digest of one window: entries in transcript order, each kept whole or one-line."""

    def __init__(self):
        self.entries = []  # (text, whole)
        self.has_content = False

    def add(self, n, text, whole=True):
        self.entries.append((f"[{n}] {text}", whole))

    def result(self, n, body, err, whole, code=None):
        if whole:
            self.add(n, f"{'RES-ERR' if err else 'RES'}: {one_line(body)}")
        else:
            self.add(n, f"RES-ERR: {error_line(body, code)}" if err else "RES: ok", whole=False)

    def lines(self, budget):
        """The digest's lines within `budget` characters. Over it, entries are left out
        oldest-first — one-line ones, then whole ones — and the first line says what went; the
        budget holds that line too. An entry longer than the budget by itself goes first."""
        size = sum(len(t) + 1 for t, _ in self.entries)
        if not budget or size <= budget:
            return [t for t, _ in self.entries]
        gone = {False: [0, 0], True: [0, 0]}  # one-line / whole → [entries, chars]

        def marker():
            return (
                f"OMITTED: over this window's {budget}-char digest budget, the oldest "
                f"{gone[False][0]} one-line entries ({gone[False][1]} chars) and "
                f"{gone[True][0]} whole entries ({gone[True][1]} chars) are left out"
            )

        out = set()
        order = [i for i, (t, _) in enumerate(self.entries) if len(t) + 1 > budget]
        order += [i for i, (_, w) in enumerate(self.entries) if not w]
        order += [i for i, (_, w) in enumerate(self.entries) if w]
        for i in order:
            if size + len(marker()) + 1 <= budget:
                break
            if i not in out:
                t, w = self.entries[i]
                out.add(i)
                size -= len(t) + 1
                gone[w][0] += 1
                gone[w][1] += len(t) + 1
        return [marker()] + [t for i, (t, _) in enumerate(self.entries) if i not in out]


# --- Claude Code -------------------------------------------------------------


def _text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text"
    )


def whole_tool(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    return name in WHOLE_TOOLS or (name == "Bash" and whole_command(str(inp.get("command", ""))))


def fmt_whole_tool(name, inp):
    if name == "Bash":
        bg = " [bg]" if inp.get("run_in_background") else ""
        d = inp.get("description", "")
        return f"Bash{bg}: {one_line(inp.get('command', ''))}" + (f"  // {d}" if d else "")
    if name in ("Task", "Agent"):
        return (
            f"{name}: [{inp.get('subagent_type', '')}|{inp.get('model', '')}] "
            f"{inp.get('description', '')} :: {one_line(inp.get('prompt', ''))}"
        )
    if name == "Workflow":
        label = inp.get("scriptPath") or inp.get("resumeFromRunId") or ""
        return f"Workflow: {label} :: {one_line(inp.get('script', ''))}"
    return f"{name}: {one_line(json.dumps(inp, ensure_ascii=False))}"


def fmt_brief_tool(name, inp):
    """What a tool call did, in one line: the tool and its target, never its content."""
    if name == "Bash":
        bg = " [bg]" if inp.get("run_in_background") else ""
        d = inp.get("description", "")
        return clip(f"Bash{bg}: {first_line(inp.get('command', ''))}" + (f"  // {d}" if d else ""))
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        extra = f" off={inp.get('offset')} lim={inp.get('limit')}" if inp.get("offset") else ""
        return clip(f"{name}: {path}{extra}")
    if name in ("Glob", "Grep"):
        return clip(f"{name}: pattern={inp.get('pattern', '')} path={inp.get('path', '')}")
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        counts = {}
        for t in todos:
            s = t.get("status", "?") if isinstance(t, dict) else "?"
            counts[s] = counts.get(s, 0) + 1
        return clip("TodoWrite: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    if name == "Skill":
        return clip(f"Skill: {inp.get('skill', '')} args={inp.get('args', '')}")
    if name == "WebFetch":
        return clip(f"WebFetch: {inp.get('url', '')}")
    if name in ("WebSearch", "ToolSearch"):
        return clip(f"{name}: {inp.get('query', '')}")
    return clip(f"{name}: {json.dumps(inp, ensure_ascii=False)}")


def digest_claude(path, since_line, until_line=0):
    w = Window()
    whole_ids = set()  # tool_use ids whose call is kept whole, so their result is too

    with open(path, encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f, 1):
            if until_line and i > until_line:
                break
            if i <= since_line:
                # A call before the window can be answered inside it (a foreground agent's
                # report): learn which earlier calls were whole, and nothing else.
                if '"tool_use"' in raw:
                    try:
                        rec = json.loads(raw)
                    except Exception:
                        continue
                    for blk in (rec.get("message") or {}).get("content") or []:
                        if (isinstance(blk, dict) and blk.get("type") == "tool_use"
                                and whole_tool(blk.get("name"), blk.get("input") or {})):
                            whole_ids.add(blk.get("id"))
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue  # malformed line: dropped, not worth a marker in the lite digest
            if not isinstance(rec, dict):
                continue

            t = rec.get("type", "?")

            if t == "summary":
                w.add(i, f"COMPACTION: {one_line(rec.get('summary', ''))}")
                continue

            if t == "system" and rec.get("subtype") == "compact_boundary":
                w.add(i, "COMPACTION: (boundary)")
                continue

            if t == "attachment":
                # A prompt queued while main was mid-turn: the user's words or a task
                # notification, which reach no user line of their own.
                att = rec.get("attachment") or {}
                if isinstance(att, dict) and att.get("type") == "queued_command":
                    txt = _text_of(att.get("prompt"))
                    if txt.strip() and not USER_META_PAT.search(txt):
                        w.add(i, f"USER: {one_line(txt)}")
                        w.has_content = True
                continue

            if t == "user":
                content = (rec.get("message") or {}).get("content")

                if rec.get("isCompactSummary"):
                    w.add(i, f"COMPACTION: {one_line(_text_of(content))}")
                    continue

                # isMeta text is the host's (a loaded skill's body, a caveat), not the user's.
                texts = [] if rec.get("isMeta") else (
                    [content] if isinstance(content, str) else [
                        b.get("text", "") for b in (content or [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                )
                for txt in texts:
                    if USER_META_PAT.search(txt):
                        continue  # slash-command wrapper noise: not "real" content
                    w.add(i, f"USER: {one_line(txt)}")
                    w.has_content = True

                for blk in content if isinstance(content, list) else []:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                        continue
                    w.result(i, _text_of(blk.get("content")), bool(blk.get("is_error")),
                             blk.get("tool_use_id") in whole_ids)
                continue

            if t == "assistant":
                for blk in (rec.get("message") or {}).get("content") or []:
                    if not isinstance(blk, dict):
                        continue
                    bt = blk.get("type")
                    if bt == "text":
                        w.add(i, f"ASSIST: {one_line(blk.get('text', ''))}")
                    elif bt == "tool_use":
                        name, inp = blk.get("name", "?"), blk.get("input") or {}
                        if not isinstance(inp, dict):
                            inp = {"input": inp}
                        if whole_tool(name, inp):
                            whole_ids.add(blk.get("id"))
                            w.add(i, f"TOOL {fmt_whole_tool(name, inp)}")
                        else:
                            w.add(i, f"TOOL {fmt_brief_tool(name, inp)}", whole=False)
                        w.has_content = True
                    # bt == "thinking": excluded by design
                continue

            # everything else (hook attachments, system messages, mode/todo
            # snapshots, ...) is not one of the six kept line formats: dropped.

    return w


# --- codex -------------------------------------------------------------------


def detect_format(path):
    """Tell a codex rollout from a Claude transcript by looking at the first lines.

    Both are JSONL, but the record shapes differ: codex has session_meta/response_item/event_msg
    records carrying a `payload`, Claude has user/assistant records carrying a `message`."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for _, raw in zip(range(20), f):
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") in ("session_meta", "response_item", "event_msg", "turn_context"):
                return "codex"
            if rec.get("type") in ("user", "assistant", "summary"):
                return "claude"
    return "claude"


def _codex_text(v):
    """codex content is either a string or a list of {type: input_text|output_text, text}."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(
            b.get("text", "") for b in v if isinstance(b, dict) and b.get("text")
        )
    return ""


def _codex_commands(name, raw):
    """The shell commands a codex tool call runs, as plain strings."""
    if isinstance(raw, dict):  # local_shell_call's action
        raw = {"command": raw.get("command")}
    elif isinstance(raw, str) and name == "exec":
        # a JS script calling tools.exec_command({cmd:"…"}) — each literal decoded
        out = []
        for lit in re.findall(r'\bcmd\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
            try:
                out.append(json.loads(f'"{lit}"'))
            except Exception:
                out.append(lit)
        return out
    elif isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, dict):
        return []
    cmd = raw.get("cmd") or raw.get("command")
    if isinstance(cmd, list):
        cmd = cmd[-1] if len(cmd) > 2 and cmd[-2] in ("-c", "-lc") else " ".join(map(str, cmd))
    return [cmd] if isinstance(cmd, str) and cmd else []


def _codex_call(pl):
    """→ (whole, text) for a codex tool call."""
    name = pl.get("name") or pl.get("type")
    raw = pl.get("input") or pl.get("arguments") or pl.get("action") or ""
    raw_s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if pl.get("namespace") == "collaboration" or name in CODEX_AGENT_TOOLS:
        return True, f"{name}: {one_line(raw_s)}"
    cmds = _codex_commands(name, raw)
    if any(whole_command(c) for c in cmds):
        return True, f"{name}: {one_line(raw_s)}"
    if cmds:
        more = f" (+{len(cmds) - 1} commands)" if len(cmds) > 1 else ""
        return False, clip(f"{name}: {first_line(cmds[0])}{more}")
    if name == "apply_patch":
        files = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", raw_s, re.M)
        return False, clip(f"{name}: {', '.join(files) or '(no files)'}")
    return False, clip(f"{name}: {raw_s}")


def _codex_failure(body):
    """→ (exit code, the failing output) when a shell result reports a nonzero exit, else None.
    A JS exec result carries one JSON line per command; the one that failed is what is shown."""
    for ln in body.splitlines():
        if ln.startswith("{") and '"exit_code"' in ln:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            v = r.get("value", r) if isinstance(r, dict) else None
            if isinstance(v, dict) and v.get("exit_code") not in (0, None):
                return v["exit_code"], str(v.get("output", ""))
    m = EXIT_HEADER.search(body)
    if m and int(m.group(1)) != 0:
        return int(m.group(1)), body
    return None


def digest_codex(path, since_line, until_line=0):
    """codex rollout JSONL → the same line vocabulary as the Claude digest.

    Sharing the output format is what lets the clerk prompt stay unaware of the host. reasoning
    is excluded for the same reason as Claude's thinking. A message reaches the rollout as a
    `response_item` (every codex version) and on older ones also as an `event_msg` right beside
    it; the second of such a pair is the same message and is not repeated."""
    w = Window()
    whole_ids = set()
    prev_msg = None  # (kind, text, record type) of the last message line

    def message(i, kind, txt, source):
        nonlocal prev_msg
        if prev_msg and prev_msg[:2] == (kind, txt) and prev_msg[2] != source:
            prev_msg = None
            return
        prev_msg = (kind, txt, source)
        w.add(i, f"{kind}: {one_line(txt)}")
        if kind == "USER":
            w.has_content = True

    with open(path, encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f, 1):
            if until_line and i > until_line:
                break
            if i <= since_line and '"call_id"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            pl = rec.get("payload")
            if not isinstance(pl, dict):
                continue
            kind = pl.get("type")

            if kind in ("custom_tool_call", "function_call", "local_shell_call"):
                whole, text = _codex_call(pl)
                if whole:
                    whole_ids.add(pl.get("call_id"))
                if i > since_line:
                    w.add(i, f"TOOL {text}", whole=whole)
                    w.has_content = True
                continue
            if i <= since_line:
                continue

            if kind == "user_message":
                txt = _codex_text(pl.get("message"))
                if txt and not USER_META_PAT.search(txt):
                    message(i, "USER", txt, rec.get("type"))
            elif kind == "agent_message":
                message(i, "ASSIST", _codex_text(pl.get("message") or pl.get("content")),
                        rec.get("type"))
            elif kind == "message" and pl.get("role") in ("user", "assistant"):
                txt = _codex_text(pl.get("content"))
                if pl["role"] == "assistant":
                    if txt.strip():
                        message(i, "ASSIST", txt, rec.get("type"))
                elif txt.strip() and not CODEX_INJECTED.match(txt) and not USER_META_PAT.search(txt):
                    message(i, "USER", txt, rec.get("type"))
            elif kind in ("custom_tool_call_output", "function_call_output"):
                body = _codex_text(pl.get("output"))
                whole = pl.get("call_id") in whole_ids
                # an explicit success field, else (one-line results only) a nonzero exit
                failed = None if whole else _codex_failure(body)
                code, shown = failed or (None, body)
                w.result(i, shown, pl.get("success") is False or failed is not None, whole, code)
            elif kind == "patch_apply_end":
                files = ", ".join((pl.get("changes") or {}).keys())
                ok = pl.get("success") is not False
                w.add(i, clip(f"{'RES' if ok else 'RES-ERR'}: patch {files or '(no files)'}"),
                      whole=False)
                w.has_content = True
            elif kind == "web_search_end":
                w.add(i, clip(f"TOOL WebSearch: {pl.get('query', '')}"), whole=False)
                w.has_content = True
            elif kind in ("compaction", "context_compaction", "compaction_trigger"):
                w.add(i, f"COMPACTION: {one_line(_codex_text(pl.get('content')))}")
            # reasoning, token_count, task_started and friends are not one of the six line
            # formats: dropped.

    return w


def digest(path, since_line, until_line=0):
    if detect_format(path) == "codex":
        return digest_codex(path, since_line, until_line)
    return digest_claude(path, since_line, until_line)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("transcript", help="path to a single .jsonl transcript file")
    ap.add_argument("--since-line", type=int, default=0, help="only digest lines after this original line number")
    ap.add_argument("--until-line", type=int, default=0, help="stop after this original line number (0 = no bound)")
    ap.add_argument("--budget", type=int, default=DIGEST_BUDGET_CHARS,
                    help=f"the digest's size bound in characters (default {DIGEST_BUDGET_CHARS}; 0 = none)")
    args = ap.parse_args()

    try:
        w = digest(args.transcript, args.since_line, args.until_line)
    except FileNotFoundError:
        print(f"digest_lite: no such file: {args.transcript}", file=sys.stderr)
        sys.exit(1)

    if not w.has_content:
        sys.exit(0)

    sys.stdout.write("\n".join(w.lines(args.budget)) + "\n")


if __name__ == "__main__":
    main()
