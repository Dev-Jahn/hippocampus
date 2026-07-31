# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""hippo CLI — ledger·task·directive 기록과 clerk 파이프라인 (DESIGN.md §3.2·3.3·3.5)."""

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CLERKS = ROOT / "clerks"
SCRIPTS = ROOT / "scripts"
TASK_STATUSES = ("pending", "active", "done", "dropped")
OPEN_STATUSES = ("pending", "active")
SCRIBE_TIMEOUT = 120  # DESIGN §3.5.4
DISTILL_TIMEOUT = 300
REAP_GRACE = 15  # clerk_run.sh owns the real deadline; we outlive it to read its rc
LOCK_WAIT = 3.0  # 마지막 턴의 꼬리를 잃지 않도록 짧게 블로킹 재시도한다
SUBSTANTIVE = re.compile(r"^(?:\[\d+\]\s*)?(TOOL|USER)\b")
WORKLOG_ENTRY = re.compile(r"^- (\d\d:\d\d) (.*)$")

# --- ledger 스키마 (DESIGN §3.2 — 정확히 이대로) -----------------------------

REQUIRED = {
    "dispatch": ("id", "kind", "exec", "scope"),
    "outcome": ("ref", "result"),
    "review": ("id", "base", "source", "findings"),
    "review-status": ("ref", "addressed"),
    "directive": ("id", "state"),
    "clerk": ("name", "ok"),
}
# ev별 허용 키 whitelist — 이 밖의 키는 거부한다. 스키마(§3.2)에 없는 필드가
# ledger에 들어오면 파생 집계(distiller)가 조용히 오염된다.
ALLOWED = {
    "dispatch": ("id", "kind", "exec", "scope", "task"),
    "outcome": ("ref", "result", "attr", "rework", "by", "note"),
    "review": ("id", "base", "source", "findings"),
    "review-status": ("ref", "addressed", "at"),
    "directive": ("id", "text", "scope", "state"),
    "clerk": ("name", "ok", "ms", "tokens"),
}
# 기록기만 스탬프하는 필드. 호출자(=clerk 출력·log raw·환경변수)가 반입하면 거부한다:
# scribe는 untrusted transcript를 읽으므로 시각·출처를 위조할 수 있으면 안 된다.
WRITER_ONLY = ("t", "src")
SRC_VALUES = ("scribe", "cli", "wrapper")  # DESIGN §3.2
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
ENUMS = {
    ("outcome", "result"): {"accepted", "revised", "refuted", "no-go", "lost"},
    ("outcome", "attr"): {"work", "brief", "harness"},
    ("directive", "scope"): {"turn", "phase", "durable"},
    ("directive", "state"): {"active", "retracted", "expired"},
}
INT_FIELDS = ("findings", "rework", "ms", "tokens")


def validate_event(e):
    """검증 실패 사유 문자열, 통과면 None."""
    if not isinstance(e, dict):
        return "이벤트는 JSON 객체여야 합니다"
    ev = e.get("ev")
    if ev not in REQUIRED:
        return f"미지의 ev: {ev!r} (허용: {', '.join(sorted(REQUIRED))})"
    for f in WRITER_ONLY:
        if f in e:
            return f"ev={ev}: {f}는 기록기가 스탬프하는 필드다 — 호출자가 지정할 수 없다"
    unknown = sorted(k for k in e if k != "ev" and k not in ALLOWED[ev])
    if unknown:
        return (
            f"ev={ev}: 허용되지 않은 키: {', '.join(unknown)} "
            f"(허용: {', '.join(ALLOWED[ev])})"
        )
    for f in REQUIRED[ev]:
        if e.get(f) is None or (isinstance(e[f], str) and not e[f].strip()):
            return f"ev={ev}: 필수 필드 누락: {f}"
    if ev == "directive" and e["state"] == "active":
        for f in ("text", "scope"):
            if not e.get(f):
                return f"ev=directive state=active: 필수 필드 누락: {f}"
    for (evn, field), allowed in ENUMS.items():
        if ev == evn and field in e and e[field] not in allowed:
            return f"ev={ev}: {field}={e[field]!r} — 허용: {', '.join(sorted(allowed))}"
    for f in INT_FIELDS:
        if f in e and (isinstance(e[f], bool) or not isinstance(e[f], int)):
            return f"ev={ev}: {f}는 정수여야 합니다"
    if ev == "clerk" and not isinstance(e["ok"], bool):
        return "ev=clerk: ok는 true/false여야 합니다"
    # review.base는 SHA pinning의 전부다(§3.2) — "unknown" 같은 자리채움을 막는다.
    if ev == "review" and not SHA_RE.match(str(e["base"])):
        return f"ev=review: base는 7~40자 hex sha여야 합니다: {e['base']!r}"
    return None


# --- 기본 유틸 ----------------------------------------------------------------


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text, limit):
    """주입 표면(§6)에 들어가는 문자열을 한 줄로 접고 자른다.

    directive의 text는 scribe가 untrusted transcript에서 읽어 쓴 값이다 —
    여러 줄이거나 아주 길면 상주 표면 자체를 밀어낼 수 있다."""
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


ACTIVE_PARSER = None  # 오류 메시지에 동봉할 usage (m7 — main()이 파싱 후 채운다)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    if ACTIVE_PARSER is not None:
        print(ACTIVE_PARSER.format_usage().rstrip(), file=sys.stderr)
    sys.exit(code)


def find_hippo():
    """cwd에서 위로 .hippo/ 탐색 (git root와 $HOME이 상한)."""
    d = Path.cwd().resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    for p in [d, *d.parents]:
        if (p / ".hippo").is_dir():
            return p / ".hippo"
        if (p / ".git").exists():
            break
        if p == home:  # 홈 위로는 올라가지 않는다 — 남의 프로젝트를 주울 수 있다
            break
    return None


def resolve_src(src=None):
    """src ∈ scribe|cli|wrapper (DESIGN §3.2). scripts/dispatch.sh만 HIPPO_SRC로
    자신을 wrapper라 선언한다 — 그 값도 whitelist 밖이면 조용히 넘기지 않고 죽는다."""
    v = src or os.environ.get("HIPPO_SRC") or "cli"
    if v not in SRC_VALUES:
        die(f"src는 {'|'.join(SRC_VALUES)} 중 하나여야 합니다: {v!r} (HIPPO_SRC 확인)")
    return v


def append_event(hp, e, src=None):
    # t/src는 기록기가 무조건 스탬프한다: 호출자가 실어 보낸 값은 여기서 버려지고
    # (validate_event는 애초에 거부한다) 검증된 값으로 대체된다.
    body = {k: v for k, v in e.items() if k not in WRITER_ONLY}
    rec = {"t": now_iso(), **body, "src": resolve_src(src)}
    with (hp / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_ledger(hp):
    p = hp / "ledger.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def directives(hp):
    """id → 현재 상태 (같은 id의 마지막 이벤트가 진실 — 파생 파일 없음)."""
    cur = {}
    for e in read_ledger(hp):
        if e.get("ev") != "directive" or not e.get("id"):
            continue
        d = cur.setdefault(e["id"], {"id": e["id"]})
        d.update({k: v for k, v in e.items() if k not in ("ev", "src")})
    return cur


def tasks_load(hp):
    p = hp / "tasks.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None
    data = data or {}
    if not isinstance(data, dict) or not isinstance(data.get("tasks", []), list):
        die(f"tasks.yaml 형식 오류: {{tasks: [...]}} 여야 합니다 ({p})")
    data.setdefault("tasks", [])
    return data


def tasks_save(hp, data):
    p = hp / "tasks.yaml"
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def find_task(data, tid):
    for t in data["tasks"]:
        if t.get("id") == tid:
            return t
    return None


def config(hp):
    p = hp / "config.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def clerk_env(hp, timeout):
    env = dict(os.environ)
    env["HIPPO_CLERK_TIMEOUT"] = str(timeout)
    backend = (config(hp).get("clerk") or {}).get("backend")
    if backend:  # config.yaml > $HIPPO_CLERK_BACKEND (DESIGN §3.5.4)
        env["HIPPO_CLERK_BACKEND"] = str(backend)
    return env


def run_clerk(hp, prompt_path, input_text, timeout):
    """clerk_run.sh 호출 → (stdout, stderr, rc, ms, tokens).
    인프라 부재·실패는 예외 없이 오류로 종료. tokens는 문자수/4 추정치(m2)."""
    script = SCRIPTS / "clerk_run.sh"
    if not script.exists():
        die(f"clerk 실행기가 없습니다: {script}")
    if not prompt_path.exists():
        die(f"clerk 프롬프트가 없습니다: {prompt_path}")
    prompt_text = prompt_path.read_text(encoding="utf-8")
    # 스크래치 파일은 .hippo/ 밖(시스템 임시 디렉터리)에 둔다 — .hippo/의
    # 내용물은 §3.1에 문서화된 것만이어야 한다.
    fd, tmp_name = tempfile.mkstemp(prefix="hippo-clerk-", suffix=".txt")
    tmp = Path(tmp_name)
    os.close(fd)
    tmp.write_text(input_text, encoding="utf-8")
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [str(script), str(prompt_path), str(tmp)],
            capture_output=True,
            text=True,
            timeout=timeout + REAP_GRACE,
            env=clerk_env(hp, timeout),
        )
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", f"timeout {timeout}s", -1
    finally:
        tmp.unlink(missing_ok=True)
    tokens = (len(prompt_text) + len(input_text) + len(out)) // 4
    return out, err, rc, int((time.monotonic() - t0) * 1000), tokens


def dump_failure(hp, kind, text):
    d = hp / "failures"
    d.mkdir(exist_ok=True)
    # 초 단위 타임스탬프만으로는 같은 초에 난 두 실패가 서로를 덮어쓴다.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    p = d / f"{stamp}-{kind}-{os.getpid()}-{uuid.uuid4().hex[:6]}.txt"
    p.write_text(text, encoding="utf-8")
    return p


def extract_json(text):
    """앞뒤 잡음·코드펜스를 허용하는 관대한 JSON 객체 추출."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


# --- 명령 --------------------------------------------------------------------


def cmd_init(_args):
    hp = Path.cwd() / ".hippo"
    if hp.is_dir():
        print(f"이미 있습니다: {hp}")
        return
    hp.mkdir(parents=True)
    (hp / "failures").mkdir()
    (hp / "ledger.jsonl").touch()
    tasks_save(hp, {"tasks": []})
    print(f"생성: {hp}")


DIRECTIVE_BUDGET = 1600  # 주입 표면의 directive 블록 글자 예산 (DESIGN §6)


def status_lines(hp):
    """DESIGN §6 상주 표면 (헤더 + 살아있는 지시 + last)."""
    data = tasks_load(hp)
    n_open = sum(1 for t in data["tasks"] if t.get("status") in OPEN_STATUSES)
    live = [d for d in directives(hp).values() if d.get("state") == "active"]

    def stamp(name):
        p = hp / name
        return (
            datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d")
            if p.exists()
            else "—"
        )

    lines = [
        (
            f"[hippo] tasks {n_open} open · directives {len(live)} live "
            f"· priors {stamp('PRIORS.md')} · worklog {stamp('worklog.md')}"
        )
    ]
    # durable 먼저, 그리고 절대 phase/turn에 밀려 접히지 않는다: 수명이 없는 지시가
    # 세션 시작에 안 보이면 그 지시는 사실상 없는 것이다 (원칙 9의 반대 방향 사고).
    # 상한은 줄 수가 아니라 글자 예산 — 긴 지시 한 줄과 짧은 지시 한 줄이 같은 비용일
    # 이유가 없다.
    ordered = [d for d in live if d.get("scope") == "durable"] + [
        d for d in live if d.get("scope") != "durable"
    ]
    used, shown = 0, 0
    for d in ordered:
        durable = d.get("scope") == "durable"
        line = f"· live({d.get('scope', '?')}): {one_line(d.get('text', ''), 200 if durable else 80)}"
        if shown and used + len(line) > DIRECTIVE_BUDGET:
            break
        lines.append(line)
        used += len(line)
        shown += 1
    if shown < len(ordered):
        lines.append(f"· (+{len(ordered) - shown} more — `hippo directive` 로 전문)")
    p = hp / "worklog.md"
    if p.exists():
        # 마지막 날짜 섹션 안에서, scribe가 쓴 "- HH:MM …" 항목만 본다.
        # (중첩 불릿이나 사람이 쓴 자유 불릿을 last로 오픽업하지 않는다.)
        wl = p.read_text(encoding="utf-8").splitlines()
        starts = [i for i, ln in enumerate(wl) if ln.startswith("## ")]
        section = wl[starts[-1] + 1 :] if starts else wl
        entries = [m.group(2).strip() for m in map(WORKLOG_ENTRY.match, section) if m]
        if entries:
            lines.append(f"· last: {one_line(entries[-1], 120)}")
    return lines


def cmd_status(args):
    hp = args.hp
    print("\n".join(status_lines(hp)))
    if args.inject:
        return
    open_tasks = [
        t for t in tasks_load(hp)["tasks"] if t.get("status") in OPEN_STATUSES
    ]
    if open_tasks:
        print("\ntasks:")
        for t in open_tasks:
            print(f"  {t['id']}  [{t.get('status', '?')}]  {t.get('title', '')}")


def cmd_task_add(args):
    data = tasks_load(args.hp)
    if find_task(data, args.id):
        die(f"이미 있는 task id: {args.id}")
    t = {
        "id": args.id,
        "title": args.title,
        "status": args.status,
        "notes": [args.notes] if args.notes else [],
        "deps": [d.strip() for d in args.deps.split(",") if d.strip()]
        if args.deps
        else [],
        "updated": now_iso(),
    }
    if t["status"] not in TASK_STATUSES:
        die(f"status는 {'|'.join(TASK_STATUSES)} 중 하나여야 합니다: {t['status']}")
    data["tasks"].append(t)
    tasks_save(args.hp, data)
    print(f"added {args.id}")


def cmd_task_set(args):
    data = tasks_load(args.hp)
    t = find_task(data, args.id)
    if not t:
        die(f"없는 task id: {args.id}")
    field, value = args.field, args.value
    if field not in ("title", "status", "notes", "deps"):
        die("field는 title|status|notes|deps 중 하나여야 합니다")
    if field == "status" and value not in TASK_STATUSES:
        die(f"status는 {'|'.join(TASK_STATUSES)} 중 하나여야 합니다: {value}")
    if field == "deps":
        t["deps"] = [d.strip() for d in value.split(",") if d.strip()]
    elif field == "notes":
        t["notes"] = [value] if value else []
    else:
        t[field] = value
    t["updated"] = now_iso()
    tasks_save(args.hp, data)
    print(f"{args.id}.{field} = {value}")


def cmd_task_done(args):
    data = tasks_load(args.hp)
    t = find_task(data, args.id)
    if not t:
        die(f"없는 task id: {args.id}")
    t["status"] = "done"
    if args.note:
        t.setdefault("notes", [])
        if not isinstance(t["notes"], list):
            t["notes"] = [t["notes"]]
        t["notes"].append(args.note)
    t["updated"] = now_iso()
    tasks_save(args.hp, data)
    print(f"done {args.id}")


def cmd_task_drop(args):
    data = tasks_load(args.hp)
    t = find_task(data, args.id)
    if not t:
        die(f"없는 task id: {args.id}")
    t["status"] = "dropped"
    t["updated"] = now_iso()
    tasks_save(args.hp, data)
    print(f"dropped {args.id}")


def cmd_task_list(args):
    tasks = tasks_load(args.hp)["tasks"]
    if not args.all:
        want = (
            [s.strip() for s in args.status.split(",") if s.strip()]
            if args.status
            else list(OPEN_STATUSES)
        )
        bad = [s for s in want if s not in TASK_STATUSES]
        if bad:
            die(f"미지의 status: {', '.join(bad)} (허용: {'|'.join(TASK_STATUSES)})")
        tasks = [t for t in tasks if t.get("status") in want]
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return
    for t in tasks:
        print(f"{t.get('id')}  [{t.get('status', '?')}]  {t.get('title', '')}")


def cmd_task_show(args):
    t = find_task(tasks_load(args.hp), args.id)
    if not t:
        die(f"없는 task id: {args.id}")
    if args.json:
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return
    print(yaml.safe_dump(t, allow_unicode=True, sort_keys=False).rstrip())


def log_and_print(hp, e):
    err = validate_event(e)
    if err:
        die(f"검증 실패: {err}")
    rec = append_event(hp, e)
    print(json.dumps(rec, ensure_ascii=False))


def cmd_log(args):
    e = {"ev": args.ev}
    if args.ev == "dispatch":
        e.update(id=args.id, kind=args.kind, exec=args.exec, scope=args.scope)
        if args.task:
            e["task"] = args.task
    elif args.ev == "outcome":
        e.update(ref=args.ref, result=args.result)
        for k in ("attr", "rework", "by", "note"):
            if getattr(args, k) is not None:
                e[k] = getattr(args, k)
    elif args.ev == "review":
        e.update(id=args.id, base=args.base, source=args.source, findings=args.findings)
    elif args.ev == "review-status":
        e.update(ref=args.ref, addressed=args.addressed)
        if args.at:
            e["at"] = args.at
    elif args.ev == "directive":
        did = args.id
        if not did:
            if args.state != "active" or not args.text:
                die("directive: --id 생략은 --text가 있는 신규(active) 지시만 가능하다")
            slug = re.sub(r"[^a-z0-9]+", "-", args.text.lower()).strip("-")[:20]
            did = f"{slug or 'directive'}-{hashlib.sha1(args.text.encode()).hexdigest()[:4]}"
        e.update(id=did, state=args.state)
        if args.text:
            e["text"] = args.text
        if args.scope:
            e["scope"] = args.scope
    log_and_print(args.hp, e)


def cmd_log_raw(args):
    try:
        e = json.loads(args.json)
    except json.JSONDecodeError as ex:
        die(f"JSON 파싱 실패: {ex}")
    log_and_print(args.hp, e)


def cmd_directive_retract(args):
    d = directives(args.hp).get(args.id)
    if not d:
        die(f"없는 directive id: {args.id}")
    log_and_print(args.hp, {"ev": "directive", "id": args.id, "state": "retracted"})


def cmd_directive_list(args):
    ds = list(directives(args.hp).values())
    if args.active:
        ds = [d for d in ds if d.get("state") == "active"]
    if args.json:
        print(json.dumps(ds, ensure_ascii=False, indent=2))
        return
    for d in ds:
        print(
            f"{d['id']}  [{d.get('state', '?')}/{d.get('scope', '?')}]  {d.get('text', '')}"
        )


def cmd_log_tail(args):
    # 주의: `log` 서브파서의 dest가 ev라서 args.ev == "tail"이다 — 필터 플래그
    # `--ev`는 dest=ev_filter로 비켜서 받는다 (표면은 구 `ledger tail`과 동일).
    p = args.hp / "ledger.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    if args.ev_filter:
        keep = []
        for ln in lines:
            try:
                if json.loads(ln).get("ev") == args.ev_filter:
                    keep.append(ln)
            except json.JSONDecodeError:
                pass
        lines = keep
    for ln in lines[-args.n :]:
        print(ln)


def cmd_prior_show(args):
    p = args.hp / "PRIORS.md"
    print(
        p.read_text(encoding="utf-8").rstrip()
        if p.exists()
        else "아직 없음 — hippo prior distill"
    )


def cmd_distill(args):
    hp = args.hp
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    kept = []
    for e in read_ledger(hp):
        try:
            t = datetime.strptime(e.get("t", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if t >= cutoff:
            kept.append(json.dumps(e, ensure_ascii=False))
    priors = (
        (hp / "PRIORS.md").read_text(encoding="utf-8")
        if (hp / "PRIORS.md").exists()
        else "(없음)"
    )
    payload = (
        f"# ledger 이벤트 (최근 {args.days}일, {len(kept)}건)\n\n"
        + "\n".join(kept)
        + "\n\n# 현재 PRIORS.md\n\n"
        + priors
        + "\n"
    )
    out, err, rc, ms, tokens = run_clerk(
        hp, CLERKS / "distiller.md", payload, DISTILL_TIMEOUT
    )
    meter = {"ev": "clerk", "name": "distiller", "ms": ms, "tokens": tokens}
    if rc != 0 or not out.strip():
        p = dump_failure(
            hp, "distill", f"rc={rc}\n--- stderr ---\n{err}\n--- stdout ---\n{out}"
        )
        append_event(hp, {**meter, "ok": False})
        die(f"distill 실패 (rc={rc}) — 덤프: {p}")
    tmp = hp / "PRIORS.md.tmp"
    tmp.write_text(out.strip() + "\n", encoding="utf-8")
    os.replace(tmp, hp / "PRIORS.md")
    append_event(hp, {**meter, "ok": True})
    print(f"PRIORS.md 재생성 ({len(kept)}건 / {args.days}일, {ms}ms)")


# --- scribe (DESIGN §3.5) -----------------------------------------------------


def worklog_append(hp, text):
    p = hp / "worklog.md"
    now = datetime.now()
    hdr = f"## {now.strftime('%Y-%m-%d')}"
    entry = f"- {now.strftime('%H:%M')} {text}"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    if hdr in lines:
        i = lines.index(hdr) + 1
        while i < len(lines) and not lines[i].startswith("## "):
            i += 1
        while i > 0 and not lines[i - 1].strip():
            i -= 1
        lines.insert(i, entry)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [hdr, "", entry]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_cursors(hp):
    """읽기 실패는 커서 전체를 잃는 것보다 낫다 — 원문을 덤프하고 빈 상태로 시작한다."""
    p = hp / "cursors.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as ex:
        dump_failure(hp, "cursors", f"{type(ex).__name__}: {ex}\n")
        return {}
    if not isinstance(data, dict):
        dump_failure(hp, "cursors", f"cursors.json이 객체가 아님: {data!r}\n")
        return {}
    return data


def save_cursors(hp, cursors):
    # tmp + os.replace: 중간에 죽어도 반쯤 쓰인 cursors.json이 남지 않는다.
    # (tmp를 .hippo/ 안에 두는 이유는 os.replace가 같은 파일시스템을 요구하기 때문)
    p = hp / "cursors.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cursors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, p)


DISPATCH_USAGE = (
    "usage: hippo dispatch --kind <kind> --scope <scope> [--task <task-id>] "
    "[--] <codex exec args...>\n"
    "       -- 이후 인자는 wrapper flag와 같은 형태여도 모두 codex exec로 전달됩니다"
)


def split_dispatch_argv(argv):
    """dispatch 자기 flag만 떼고 나머지는 codex exec로 통째 넘긴다.

    argparse를 쓰지 않는 이유: 남은 인자는 codex의 문법(-m, -c k=v, -C dir …)이라
    이 파서가 해석하려 들면 안 된다. `--` 이후는 wrapper flag와 같은 형태여도 전부 통과."""
    kind = scope = task = ""
    rest = []
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            rest.extend(argv[i + 1 :])
            break
        for name, key in (("--kind", "kind"), ("--scope", "scope"), ("--task", "task")):
            if a == name:
                if i + 1 >= n:
                    die(f"dispatch: {name} 에 값이 없습니다\n{DISPATCH_USAGE}", 2)
                val, i = argv[i + 1], i + 2
                break
            if a.startswith(name + "="):
                val, i = a[len(name) + 1 :], i + 1
                break
        else:
            rest.append(a)
            i += 1
            continue
        if key == "kind":
            kind = val
        elif key == "scope":
            scope = val
        else:
            task = val
    if not kind or not scope:
        die(DISPATCH_USAGE, 2)
    return kind, scope, task, rest


def exec_label(rest):
    """codex로 갈 인자에서 model/effort를 읽는다(소비하지 않는다) — 자기 argv에서
    이미 아는 지점이 곧 자동 수집 지점이다(원칙 6)."""
    model = effort = ""
    for i, a in enumerate(rest):
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        if a in ("-m", "--model"):
            model = nxt
        elif a == "-c" and nxt.startswith("model_reasoning_effort="):
            effort = nxt[len("model_reasoning_effort=") :].strip('"')
    return f"codex/{model or 'unset'}/{effort or 'unset'}"


def run_dispatch(argv):
    """DESIGN §3.6. 기록에 실패해도 발사는 막지 않는다 — 이 표면의 본업은 codex 실행이고
    ledger는 부수 효과다. 다만 기록이 없어진 사실은 반드시 소리를 낸다."""
    kind, scope, task, rest = split_dispatch_argv(argv)
    did = "d" + os.urandom(16).hex()
    hp = find_hippo()
    if hp is None:
        print("dispatch: .hippo/ 없음 — dispatch 기록을 생략합니다", file=sys.stderr)
    else:
        e = {"ev": "dispatch", "id": did, "kind": kind, "exec": exec_label(rest), "scope": scope}
        if task:
            e["task"] = task
        bad = validate_event(e)
        if bad:
            print(f"dispatch: 기록 실패 ({bad}) — 위임은 계속 진행합니다", file=sys.stderr)
        else:
            # 원장 줄은 stderr로: stdout 첫 줄은 dispatch id의 자리다(§3.6).
            print(json.dumps(append_event(hp, e, src="wrapper"), ensure_ascii=False), file=sys.stderr)
    print(f"dispatch:{did}", flush=True)
    # stdin을 닫고 넘긴다 — 열린 채로 두면 codex exec가 입력을 기다리며 멈춘다.
    with open(os.devnull) as devnull:
        os.dup2(devnull.fileno(), 0)
    try:
        os.execvp("codex", ["codex", "exec", *rest])
    except OSError as err:
        die(f"dispatch: codex 실행 실패: {err}", 127)


def cmd_scribe(args):
    hp = args.hp
    lock = (hp / "scribe.lock").open("w")
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            # 세션 마지막 턴의 패자는 "다음 실행"이 없어 꼬리를 영구히 잃는다.
            # 그래서 잠깐 기다려 본 뒤에만 포기한다.
            if time.monotonic() >= deadline:
                return
            time.sleep(0.1)

    transcript = Path(args.transcript)
    if not transcript.exists():
        die(f"transcript가 없습니다: {transcript}")
    cursors = load_cursors(hp)
    since = int(cursors.get(args.session, 0))
    end = sum(1 for _ in transcript.open("r", encoding="utf-8", errors="replace"))

    digest_py = SCRIPTS / "digest_lite.py"
    if not digest_py.exists():
        die(f"digest 스크립트가 없습니다: {digest_py}")
    r = subprocess.run(
        # sys.executable, not "python3": under `uv run --script` there is no
        # guarantee a python3 sits on PATH. digest_lite is stdlib-only.
        # --until-line: 커서를 end로 전진시킬 것이므로 다이제스트도 정확히 end에서
        # 끊는다. 안 그러면 라인 세기와 digest 사이에 추가된 라인이 이번에 요약되고
        # 다음 실행에서 다시 요약된다(창 경계 중복).
        [
            sys.executable,
            str(digest_py),
            str(transcript),
            "--since-line",
            str(since),
            "--until-line",
            str(end),
        ],
        capture_output=True,
        text=True,
        timeout=SCRIBE_TIMEOUT,
    )
    if r.returncode != 0:
        die(f"digest 실패 (rc={r.returncode}): {r.stderr.strip()}")
    digest = r.stdout

    def save_cursor():
        cursors[args.session] = end
        save_cursors(hp, cursors)

    # 3. 결정적 프리필터: 실질 활동이 없으면 모델 호출 자체를 생략
    # (digest 라인 형식: "[123] TOOL Bash: …" / "[124] USER: …")
    if not any(SUBSTANTIVE.match(ln) for ln in digest.splitlines()):
        save_cursor()
        return

    out, err, rc, ms, tokens = run_clerk(
        hp, CLERKS / "turn-scribe.md", digest, SCRIBE_TIMEOUT
    )
    meter = {"ev": "clerk", "name": "turn-scribe", "ms": ms, "tokens": tokens}

    def fail(reason):
        p = dump_failure(
            hp,
            "scribe",
            f"{reason}\nrc={rc}\n--- stderr ---\n{err}\n--- stdout ---\n{out}",
        )
        # DESIGN §3.5.6 — 실패해도 커서는 전진한다. 이 구간의 기록은 failures/의
        # 덤프이고(그것이 곧 기록이다), 커서를 세워두면 같은 입력을 매 턴 다시
        # 모델에 태워 무한히 재과금한다.
        save_cursor()
        append_event(hp, {**meter, "ok": False}, src="scribe")
        die(f"scribe 실패: {reason} — 덤프: {p}")

    if rc != 0:
        fail(f"clerk rc={rc}")
    obj = extract_json(out)
    if obj is None:
        fail("JSON 객체를 찾지 못함")
    if not isinstance(obj.get("worklog", ""), str) or not isinstance(
        obj.get("events", []), list
    ):
        fail("worklog는 문자열, events는 배열이어야 함")
    events = obj.get("events", [])
    for e in events:
        verr = validate_event(e)
        if verr:
            fail(f"이벤트 검증 실패: {verr}")

    for e in events:
        append_event(hp, e, src="scribe")
    if obj.get("worklog", "").strip():
        worklog_append(hp, obj["worklog"].strip())
    save_cursor()
    append_event(hp, {**meter, "ok": True}, src="scribe")


# --- argparse -----------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="hippo",
        description=(
            "hippo — 관측·기억 배경 기관. "
            "기록은 `log <이벤트>` 한 문으로 들어가고, 맨몸 `hippo log`는 "
            "최근 기록 조회, `directive`·`prior`는 원장에서 매번 재계산되는 "
            "파생 뷰다."
        ),
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help=".hippo/ 생성 (cwd)").set_defaults(fn=cmd_init)

    s = sub.add_parser("status", help="한 덩어리 요약")
    s.add_argument(
        "--inject", action="store_true", help="SessionStart 훅 주입 형식 (§6)"
    )
    s.set_defaults(fn=cmd_status)

    t = sub.add_parser("task", help="작업 레지스트리").add_subparsers(dest="sub")
    a = t.add_parser("add", help="task 추가")
    a.add_argument("id")
    a.add_argument("--title", required=True)
    a.add_argument("--status", default="pending", help="|".join(TASK_STATUSES))
    a.add_argument("--notes")
    a.add_argument("--deps", help="콤마 구분")
    a.set_defaults(fn=cmd_task_add)
    a = t.add_parser("set", help="필드 변경 (title|status|notes|deps)")
    a.add_argument("id")
    a.add_argument("field")
    a.add_argument("value")
    a.set_defaults(fn=cmd_task_set)
    a = t.add_parser("done", help="완료 처리")
    a.add_argument("id")
    a.add_argument("--note", help="notes에 append")
    a.set_defaults(fn=cmd_task_done)
    a = t.add_parser("list", help="목록 (기본: pending+active)")
    a.add_argument("--status", help="콤마 다중 필터")
    a.add_argument("--all", action="store_true")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_task_list)
    a = t.add_parser("show", help="단건 조회")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_task_show)
    a = t.add_parser("drop", help="status=dropped")
    a.add_argument("id")
    a.set_defaults(fn=cmd_task_drop)

    lg = sub.add_parser("log", help="ledger 이벤트 기록 (fail-closed 검증)")
    lsub = lg.add_subparsers(dest="ev")
    a = lsub.add_parser("dispatch", help="위임 발사")
    a.add_argument("--id", required=True)
    a.add_argument("--kind", required=True)
    a.add_argument("--exec", required=True, help="vehicle/model/effort")
    a.add_argument("--scope", required=True)
    a.add_argument("--task")
    a.set_defaults(fn=cmd_log)
    a = lsub.add_parser("outcome", help="위임 판정")
    a.add_argument("--ref", required=True)
    a.add_argument(
        "--result", required=True, choices=sorted(ENUMS[("outcome", "result")])
    )
    a.add_argument("--attr", choices=sorted(ENUMS[("outcome", "attr")]))
    a.add_argument("--rework", type=int)
    a.add_argument("--by")
    a.add_argument("--note")
    a.set_defaults(fn=cmd_log)
    a = lsub.add_parser("review", help="외부 리뷰 회신")
    a.add_argument("--id", required=True)
    a.add_argument("--base", required=True, help="리뷰 대상 커밋 sha")
    a.add_argument("--source", required=True)
    a.add_argument("--findings", required=True, type=int)
    a.set_defaults(fn=cmd_log)
    a = lsub.add_parser("review-status", help="리뷰 반영 상태")
    a.add_argument("--ref", required=True)
    a.add_argument("--addressed", required=True, help="예: full|partial|none")
    a.add_argument("--at", help="반영 커밋 sha")
    a.set_defaults(fn=cmd_log)
    a = lsub.add_parser("raw", help="JSON 한 줄을 검증 후 append")
    a.add_argument("json")
    a.set_defaults(fn=cmd_log_raw)
    a = lsub.add_parser("tail", help="마지막 N행 (맨몸 `hippo log`의 기본)")
    a.add_argument("-n", type=int, default=20)
    a.add_argument("--ev", dest="ev_filter", help="ev 타입 필터")
    a.set_defaults(fn=cmd_log_tail)

    d = sub.add_parser("directive", help="운영 지시 (원장 파생 뷰)").add_subparsers(
        dest="sub"
    )
    a = d.add_parser("add", help="지시 기록 (ev=directive)")
    a.add_argument("--id", help="생략 시 --text에서 자동 생성 (DESIGN §3.3 auto id)")
    a.add_argument("--text")
    a.add_argument("--scope", choices=sorted(ENUMS[("directive", "scope")]))
    a.add_argument(
        "--state", default="active", choices=sorted(ENUMS[("directive", "state")])
    )
    a.set_defaults(fn=cmd_log, ev="directive")
    a = d.add_parser("list", help="목록 (ledger에서 유도)")
    a.add_argument("--active", action="store_true")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_directive_list)
    a = d.add_parser("retract", help="directive 철회")
    a.add_argument("id")
    a.set_defaults(fn=cmd_directive_retract)

    pr = sub.add_parser("prior", help="증류 표면").add_subparsers(dest="sub")
    pr.add_parser("show", help="PRIORS.md 출력").set_defaults(fn=cmd_prior_show)
    a = pr.add_parser("distill", help="distiller clerk 실행 → PRIORS.md 재생성")
    a.add_argument("--days", type=int, default=14)
    a.set_defaults(fn=cmd_distill)

    # dispatch는 main()이 argparse 앞에서 가로챈다 (남은 인자가 codex의 문법이라
    # 이 파서가 해석하면 안 된다). 여기 등록은 목록·--help 노출 전용이다.
    sub.add_parser(
        "dispatch",
        help="codex exec 발사 + ev:dispatch 자동 기록",
        usage=DISPATCH_USAGE.splitlines()[0].removeprefix("usage: "),
        description=DISPATCH_USAGE,
    )

    a = sub.add_parser("scribe", help="Stop 훅이 detached로 부르는 내부 표면")
    a.add_argument("--transcript", required=True)
    a.add_argument("--session", required=True)
    a.set_defaults(fn=cmd_scribe)

    tag_parsers(p)
    return p


def tag_parsers(p):
    """각 서브파서가 자기 자신을 기본값(_parser)으로 싣게 한다 — die()가 '지금 쓰고
    있는 명령'의 usage를 오류에 동봉할 수 있도록(m7)."""
    for act in p._actions:
        if isinstance(act, argparse._SubParsersAction):
            for sp in act.choices.values():
                sp.set_defaults(_parser=sp)
                tag_parsers(sp)


BARE_DEFAULT = {"task": "list", "log": "tail", "directive": "list", "prior": "show"}


def insert_default_sub(argv):
    """bare-noun 기본: noun만 맨몸으로 부르면 기본 조회 sub를 삽입한다
    (task→list, log→tail, directive→list, prior→show).

    삽입 조건: argv[0]가 noun이고, 다음 토큰이 없거나 '-'로 시작하되
    -h/--help가 아닐 때. `hippo task -h`는 noun 자신의 help가 떠야 하고,
    `hippo log raw '{…}'`처럼 '-'로 시작하지 않는 인자는 건드리지 않는다."""
    if argv and argv[0] in BARE_DEFAULT:
        nxt = argv[1] if len(argv) > 1 else None
        if nxt is None or (nxt.startswith("-") and nxt not in ("-h", "--help")):
            return [argv[0], BARE_DEFAULT[argv[0]], *argv[1:]]
    return argv


def parse_args_quietly(parser, argv):
    """.hippo 밖에서는 argparse의 usage+rc2도 새어나오면 안 된다(§3.1 완전 무음).
    -h/--help(정상 종료 rc 0)는 어디서든 그대로 출력한다(§3.3)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args(argv)
    except SystemExit as ex:
        code = ex.code if isinstance(ex.code, int) else 1
        if code != 0 and find_hippo() is None:
            sys.exit(0)
        sys.stdout.write(out.getvalue())
        sys.stderr.write(err.getvalue())
        raise
    sys.stdout.write(out.getvalue())
    sys.stderr.write(err.getvalue())
    return args


def main():
    global ACTIVE_PARSER
    argv = sys.argv[1:]
    # dispatch는 .hippo/ 없는 곳에서도 무음 종료하지 않는다: 이 표면의 본업은 codex
    # 발사이고, 기록을 못 한다고 발사를 삼키면 래퍼가 아니라 함정이 된다.
    if argv and argv[0] == "dispatch":
        head = argv[1 : argv.index("--")] if "--" in argv else argv[1:]
        if not ({"-h", "--help"} & set(head)):
            run_dispatch(argv[1:])
            return
    parser = build_parser()
    args = parse_args_quietly(parser, insert_default_sub(sys.argv[1:]))
    ACTIVE_PARSER = getattr(args, "_parser", parser)
    if not hasattr(args, "fn"):
        if find_hippo() is None:
            sys.exit(0)  # .hippo 없는 곳에서는 완전 무음 no-op
        parser.print_help(sys.stderr)
        sys.exit(2)
    if args.fn is not cmd_init:
        hp = find_hippo()
        if hp is None:
            sys.exit(0)
        args.hp = hp
    args.fn(args)


if __name__ == "__main__":
    main()
