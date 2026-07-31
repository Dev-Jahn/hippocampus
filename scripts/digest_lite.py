#!/usr/bin/env python3
"""Compact digest of a single Claude Code transcript (.jsonl) for LLM consumption.

Lightweight port of the 479MB-audit digest logic (see DESIGN.md §8): same
line formats (USER/ASSIST/TOOL/RES/RES-ERR/COMPACTION), same "[N]" original-
line-number prefix, same truncation rules. Dropped versus the audit tool:
stats collection, INDEX generation, directory traversal, plugin detection.
`thinking` blocks are still excluded.

stdlib only. Usage: digest_lite.py <transcript.jsonl> [--since-line N] [--until-line M]

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

USER_META_PAT = re.compile(
    r"<(local-command-caveat|command-name|local-command-stdout|command-message|command-args)>"
)


def trunc(s, n):
    if s is None:
        return ""
    s = str(s).replace("\r", "")
    return s if len(s) <= n else s[: n] + f" …[+{len(s) - n}ch]"


def one_line(s):
    return re.sub(r"\n+", " ⏎ ", s.strip())


def fmt_tool_use(name, inp):
    try:
        if name == "Bash":
            d = inp.get("description", "")
            c = one_line(trunc(inp.get("command", ""), 400))
            bg = " [bg]" if inp.get("run_in_background") else ""
            return f"Bash{bg}: {c}" + (f"  // {d}" if d else "")
        if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
            extra = ""
            if name == "Read" and inp.get("offset"):
                extra = f" off={inp.get('offset')} lim={inp.get('limit')}"
            return f"{name}: {inp.get('file_path', '')}{extra}"
        if name in ("Glob", "Grep"):
            return f"{name}: pattern={trunc(inp.get('pattern', ''), 120)} path={inp.get('path', '')}"
        if name in ("Task", "Agent"):
            return (
                f"{name}: [{inp.get('subagent_type', '')}|{inp.get('model', '')}] "
                f"{inp.get('description', '')} :: {one_line(trunc(inp.get('prompt', ''), 400))}"
            )
        if name == "TodoWrite":
            todos = inp.get("todos", [])
            return "TodoWrite: " + "; ".join(
                f"{t.get('status', '?')[:4]}:{trunc(t.get('content', ''), 60)}" for t in todos[:10]
            )
        if name == "Skill":
            return f"Skill: {inp.get('skill', '')} args={trunc(inp.get('args', ''), 200)}"
        return f"{name}: {one_line(trunc(json.dumps(inp, ensure_ascii=False), 300))}"
    except Exception:
        return f"{name}: <unparseable input>"


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
        return " ".join(
            b.get("text", "") for b in v if isinstance(b, dict) and b.get("text")
        )
    return ""


def digest_codex(path, since_line, until_line=0):
    """codex rollout JSONL → the same line vocabulary as the Claude digest.

    Sharing the output format is what lets the clerk prompt stay unaware of the host. reasoning
    is excluded for the same reason as Claude's thinking."""
    out = []
    has_content = False

    with open(path, encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f, 1):
            if i <= since_line:
                continue
            if until_line and i > until_line:
                break
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
            p = f"[{i}]"

            if kind == "user_message":
                txt = _codex_text(pl.get("message"))
                if txt and not USER_META_PAT.search(txt):
                    out.append(f"{p} USER: {one_line(trunc(txt, 2500))}")
                    has_content = True
            elif kind == "agent_message":
                out.append(f"{p} ASSIST: {one_line(trunc(_codex_text(pl.get('message')), 1500))}")
            elif kind in ("custom_tool_call", "function_call", "local_shell_call"):
                name = pl.get("name") or kind
                raw_in = pl.get("input") or pl.get("arguments") or pl.get("action") or ""
                if isinstance(raw_in, (dict, list)):
                    raw_in = json.dumps(raw_in, ensure_ascii=False)
                out.append(f"{p} TOOL {name}: {one_line(trunc(raw_in, 400))}")
                has_content = True
            elif kind in ("custom_tool_call_output", "function_call_output"):
                body = _codex_text(pl.get("output"))
                # codex has no separate failure type — trust only an explicit success field.
                err = pl.get("success") is False
                out.append(
                    f"{p} {'RES-ERR' if err else 'RES'}: {one_line(trunc(body, 1200 if err else 350))}"
                )
            elif kind == "patch_apply_end":
                files = ", ".join((pl.get("changes") or {}).keys())
                ok = pl.get("success") is not False
                out.append(f"{p} {'RES' if ok else 'RES-ERR'}: patch {files or '(no files)'}")
                has_content = True
            elif kind == "web_search_end":
                out.append(f"{p} TOOL WebSearch: {one_line(trunc(pl.get('query', ''), 300))}")
                has_content = True
            elif kind in ("compaction", "context_compaction", "compaction_trigger"):
                out.append(f"{p} COMPACTION: {one_line(trunc(_codex_text(pl.get('content')), 800))}")
            # reasoning, token_count, task_started and friends are not one of the six line
            # formats: dropped.

    return out, has_content


def digest(path, since_line, until_line=0):
    if detect_format(path) == "codex":
        return digest_codex(path, since_line, until_line)
    out = []
    has_content = False

    with open(path, encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f, 1):
            if i <= since_line:
                continue
            if until_line and i > until_line:
                break
            try:
                rec = json.loads(raw)
            except Exception:
                continue  # malformed line: dropped, not worth a marker in the lite digest

            t = rec.get("type", "?")
            p = f"[{i}]"

            if t == "summary":
                out.append(f"{p} COMPACTION: {one_line(trunc(rec.get('summary', ''), 600))}")
                continue

            if t == "system" and rec.get("subtype") == "compact_boundary":
                out.append(f"{p} COMPACTION: (boundary)")
                continue

            if t == "user":
                msg = rec.get("message", {})
                content = msg.get("content")

                if rec.get("isCompactSummary"):
                    txt = (
                        content
                        if isinstance(content, str)
                        else " ".join(
                            b.get("text", "")
                            for b in (content or [])
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    )
                    out.append(f"{p} COMPACTION: {one_line(trunc(txt, 800))}")
                    continue

                texts = (
                    [content]
                    if isinstance(content, str)
                    else [
                        b.get("text", "")
                        for b in (content or [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                )
                for txt in texts:
                    if USER_META_PAT.search(txt):
                        continue  # slash-command wrapper noise: not "real" content
                    out.append(f"{p} USER: {one_line(trunc(txt, 2500))}")
                    has_content = True

                for blk in content or []:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                        continue
                    is_err = blk.get("is_error", False)
                    c = blk.get("content")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                    c = c or ""
                    if is_err:
                        out.append(f"{p} RES-ERR: {one_line(trunc(c, 1200))}")
                    else:
                        out.append(f"{p} RES: {one_line(trunc(c, 350))}")
                continue

            if t == "assistant":
                for blk in (rec.get("message", {}).get("content") or []):
                    bt = blk.get("type")
                    if bt == "text":
                        out.append(f"{p} ASSIST: {one_line(trunc(blk.get('text', ''), 1500))}")
                    elif bt == "tool_use":
                        name = blk.get("name", "?")
                        out.append(f"{p} TOOL {fmt_tool_use(name, blk.get('input') or {})}")
                        has_content = True
                    # bt == "thinking": excluded by design
                continue

            # everything else (hook attachments, system messages, mode/todo
            # snapshots, ...) is not one of the six kept line formats: dropped.

    return out, has_content


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("transcript", help="path to a single .jsonl transcript file")
    ap.add_argument("--since-line", type=int, default=0, help="only digest lines after this original line number")
    ap.add_argument("--until-line", type=int, default=0, help="stop after this original line number (0 = no bound)")
    args = ap.parse_args()

    try:
        out, has_content = digest(args.transcript, args.since_line, args.until_line)
    except FileNotFoundError:
        print(f"digest_lite: no such file: {args.transcript}", file=sys.stderr)
        sys.exit(1)

    if not has_content:
        sys.exit(0)

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
