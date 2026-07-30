# waystone 1.0 — 설계 문서 (founding)

> 2026-07-31. 한 달간의 research-cc dogfooding 전수 감사(37세션·위임 806건·479MB)와
> Opus 5 prompting guide, `/doctor` skill 분석에서 도출된 재정립.
> 이전 세대(0.x round/review/delegate/brief 기계)는 git 이력(dev 브랜치)으로 은퇴했다.

## 0. 한 문장

**waystone은 agent를 통제하는 프로세스가 아니라, agent에게 지각(관측)과 기억(정리)을
제공하는 배경 기관(organ)이다. 판단과 창발은 모델의 몫이고, waystone의 일은 그 판단이
좋은 증거 위에서 일어나게 조용히 받쳐주는 것이다.**

## 1. 원칙 (전부 dogfooding 실측에서 도출)

1. **간결성** — 규율의 진짜 비용은 규율을 지키는 비용이 아니라 규율에 대해 *생각하는*
   비용이다. agent의 attention이 도구로 새면 프로젝트가 느려진다.
   (실측: waystone CLI 1,081회 호출 중 7.9% 오류 응답·문법 재시도 루프,
   boundary 훅 259회 실행 전량 무출력, 8–14KB 계약문 재주입.)
2. **투명성** — 내부가 아무리 복잡해도 표면에 새어나오지 않는다. 표면 노출의 유일한
   기준: *"이 정보를 알면 agent의 다음 행동이 달라지는가?"* 아니면 침묵.
3. **기록하되 강제하지 않는다** — git이 무결성을 보증하는 세계에서 provenance는 ledger의
   사실 한 줄이지 게이트가 아니다. 검사가 필요하면 게이트가 아니라 서비스 내부에 산다.
4. **derivability test** — 나중에 재유도 가능한 것은 저장하지 않는다(원시 텔레메트리는
   transcript에 이미 있다). 저장하는 것은 그 순간에만 존재하고 증발하는 *판단*뿐이다.
5. **해석은 저장하지 않고 생성한다** — 손으로 유지되는 해석 문서는 낡는다(PROGRESS.md
   실증). ledger는 사실만 쌓고, PRIORS 같은 해석 표면은 매번 재생성한다.
6. **판단은 main에, 증거는 waystone에** — 모델은 증거가 눈앞에 있으면 알아서 적응하지만
   증거 수집을 기억하지는 못한다. 라우팅·수용·정책 진화는 main이, 그 근거가 되는
   측정·축적·증류는 waystone이 맡는다.
7. **좋은 제약은 가역성 제약뿐** — 행동을 금지하는 제약 대신, 어떤 행동도 되돌릴 수
   있게 만드는 데 투자한다(worktree 격리가 최고 성과 패턴이었다).
8. **복잡성은 코드가 아니라 텍스트에 산다** — 판단이 필요한 로직은 clerk/skill 프롬프트로.
   런타임 코드가 줄면 corner case도 준다. (`/doctor` = 43KB 프롬프트 하나가 존재 증명.)
9. **순종적인 모델일수록 낡은 지시가 위험하다** — 신형 모델은 stale 지시를 무시하지 않고
   충실히 실행한다(GPU 조항 모순 fail-closed 사고 실증). 따라서 지시에는 수명(scope)이
   1급 개념으로 붙고, 지시 위생이 검증 기계보다 우선한다.

## 2. 실행 표면 4층

| 층 | 무엇 | 비용 |
|---|---|---|
| deterministic script | 훅·CLI. 빠르고 멍청한 것 | 0 |
| **clerk** | 훅/cron이 저가 모델(luna·haiku급)을 headless 호출. 판단이 필요하지만 main의 맥락은 불필요한 일 | 토큰만, main 컨텍스트 0 |
| skill | main의 맥락이 필요하거나 main이 결과에 따라 행동해야 하는 일 | main 컨텍스트 |
| main | 라우팅·수용·사용자 대화 | — |

clerk 가드레일(불변):
- 훅에서는 반드시 **detached** 발사(Stop을 1초도 막지 않는다). 발사 전 결정적 프리필터로
  사소한 턴은 모델 호출 자체를 생략.
- **재귀 금지**(clerk는 clerk를 낳지 않는다), 쓰기 없음(지정 산출물 제외), 타임아웃 필수.
- transcript는 **untrusted 입력**: clerk 출력은 ledger/생성물로만 가며, 설정·CLAUDE.md를
  직접 편집하지 않는다(제안까지만).
- **자기 계량**: clerk 실행 자체가 ledger에 기록된다(`ev:clerk`).
- **조용한 죽음 금지 원칙의 변형**: clerk가 죽어도 시스템은 안 깨지지만(설계 의도),
  공백을 지어내 메꾸지 않는다. 실패는 `failures/`에 남고 checkup이 보고한다.

## 3. 구성요소

```
런타임(얇게):  bin/waystone(shim) + cli/waystone_cli.py + hooks 2개 + scripts/{clerk_run,digest_lite,dispatch}
인지(텍스트):  clerks/{turn-scribe,distiller}.md + skills/{checkup,dispatch}
상주(≤6줄):   SessionStart 주입 한 덩어리 (아래 §6)
강제:         없음
```

### 3.1 프로젝트 데이터 (`.waystone/`, per-project)

```
.waystone/
  tasks.yaml        # 작업 레지스트리 (사람이 읽고 고칠 수 있는 YAML)
  ledger.jsonl      # append-only 이벤트 원장
  worklog.md        # 생성물: scribe가 누적하는 사람용 작업 일지 (날짜 섹션)
  PRIORS.md         # 생성물: distiller가 재생성하는 증류 표면
  cursors.json      # scribe의 세션별 transcript 커서
  failures/         # clerk 출력 검증 실패 덤프 (checkup이 보고)
  config.yaml       # 선택: clerk backend 등 오버라이드 (없어도 전부 동작)
```

`.waystone/`가 없는 디렉터리에서 모든 훅·CLI는 **완전 무음 no-op**이다(다른 프로젝트 오염 0).

### 3.2 Ledger 스키마 (계약 — 정확히 이대로)

한 줄 = JSON 객체. 공통 필드: `t`(ISO8601, 기록기가 스탬프), `ev`, 선택 `src`(`scribe|cli|wrapper`).

```jsonl
{"t":"…","ev":"dispatch","id":"d041","kind":"kernel-impl","exec":"codex/gpt-5.6-sol/high","scope":"pass2 SS-UMMA tensorize","task":"feat/x"}
{"t":"…","ev":"outcome","ref":"d041","result":"refuted","attr":"work","rework":2,"by":"verify/opus","note":"oracle 순환참조"}
{"t":"…","ev":"review","id":"r007","base":"abc123f","source":"chatgpt-web","findings":4}
{"t":"…","ev":"review-status","ref":"r007","addressed":"partial","at":"def4567"}
{"t":"…","ev":"directive","id":"gpu-01","text":"GPU 0,1만 사용","scope":"phase","state":"active"}
{"t":"…","ev":"directive","id":"gpu-01","state":"retracted"}
{"t":"…","ev":"clerk","name":"turn-scribe","ms":8100,"ok":true,"tokens":1400}
```

- `outcome.result ∈ {accepted, revised, refuted, no-go, lost}`;
  `attr ∈ {work, brief, harness}` (accepted가 아닐 때 권장 — 귀속 없는 실패 집계는
  거짓 prior를 만든다. 실측: REFUTED 다발은 work, GPU 조항 모순 NO-GO는 brief,
  StructuredOutput 증발은 harness였다).
- `directive.scope ∈ {turn, phase, durable}`, `state ∈ {active, retracted, expired}`.
  같은 `id`의 마지막 이벤트가 현재 상태다(파생 뷰는 저장하지 않는다 — 원칙 4·5).
- `review.base`는 리뷰 대상 커밋 — **이것이 SHA pinning의 전부다**(원칙 3).
- 검증: `waystone log`는 ev별 필수 필드를 fail-closed로 검사한다. 미지의 `ev`는 거부.
- 소요 시간은 필드가 아니다: dispatch/outcome 타임스탬프 차로 유도(원칙 4).
- 규모 근거: research-cc 한 달 = 위임 806건 → 약 1,600행 ≈ 300KB. grep으로 충분.

### 3.3 CLI (`bin/waystone` → `cli/waystone_cli.py`)

- 구현: Python 단일 파일(PEP 723 인라인 메타데이터, deps: PyYAML), `bin/waystone`은
  `uv run --script` shim(+ uv 부재 시 python3 폴백, 실패는 명확한 오류로).
- **모든 서브커맨드에 `-h/--help`, 오류 시 usage를 stderr에 동봉** (0.x 최다 마찰 직접 수리).
- 표면:

```
waystone init                                  # .waystone/ 생성만. 그 외 아무것도 안 함
waystone status [--inject]                     # 한 덩어리 요약; --inject는 훅용(무음 no-op 규칙)
waystone task add <id> --title T [--status pending] [--notes N] [--deps a,b]
waystone task set <id> <field> <value>
waystone task done <id> [--note N]
waystone task list [--status s1,s2] [--all] [--json]   # 콤마 다중 필터 (0.x 요구 수리)
waystone task show <id> | task drop <id>
waystone log <ev> [typed flags…]               # dispatch|outcome|review|review-status|directive
waystone log raw '<json>'                      # 검증 후 append
waystone retract <directive-id>
waystone directive list [--active] [--json]
waystone ledger tail [-n N] [--ev TYPE]
waystone prior show
waystone distill [--days N]                    # distiller clerk 실행 → PRIORS.md 재생성
waystone scribe --transcript P --session S     # Stop 훅이 detached로 부르는 내부 표면
```

- task 상태: `pending|active|done|dropped`. tasks.yaml은 사람이 직접 편집해도 되고
  CLI는 항상 재파싱한다. 주석 보존 같은 곡예는 하지 않는다(0.x 교훈).

### 3.4 훅 (전부 2개 — 그 외 추가 금지)

`hooks/hooks.json`:
- **SessionStart** (startup·resume·clear·compact): `hooks/session_start.sh`
  → `.waystone/` 없으면 무음 exit 0. 있으면 `waystone status --inject` (§6 형식, ≤6줄).
  compact 후 재주입이 곧 **context keeper**다: 살아있는 directive가 compaction을 넘어
  생존한다(compaction 86회 실측 유실 문제의 해법).
- **Stop**: `hooks/stop.sh` — stdin JSON에서 `transcript_path`·`session_id`·`cwd` 파싱,
  `.waystone/` 없으면 무음 exit 0. 있으면
  `setsid waystone scribe … >/dev/null 2>&1 &` 후 **즉시 exit 0** (<100ms).

### 3.5 Scribe 파이프라인 (`waystone scribe` 내부)

1. `.waystone/scribe.lock` flock 논블로킹 — 잡혀 있으면 그냥 종료(커서가 다음 실행 때
   공백을 자동 커버).
2. `cursors.json`에서 이 세션 커서 로드 → `digest_lite.py`로 커서 이후 라인만 압축
   (479MB 감사에서 검증된 다이제스트 로직의 경량판).
3. **결정적 프리필터**: 다이제스트에 TOOL/USER 라인이 없으면 커서만 갱신하고 종료
   (모델 호출 0).
4. backend 해석: `config.yaml > $WAYSTONE_CLERK_BACKEND > 자동(codex 있으면
   codex/gpt-5.6-luna/low, 아니면 claude -p haiku) > mock`(테스트용). 120s 타임아웃.
5. 프롬프트 = `clerks/turn-scribe.md` + 다이제스트. 기대 출력 = 엄격 JSON:
   `{"worklog": "…", "events": [ …t 없는 ledger 이벤트… ]}`.
6. 검증: 이벤트를 `waystone log`와 동일 규칙으로 검사. 실패 → `failures/`에 원문 덤프
   + `ev:clerk ok:false` 기록, ledger는 오염시키지 않는다. **지어내서 메꾸지 않는다.**
7. 성공 → 이벤트 append(`src:scribe`), worklog.md의 오늘 날짜 섹션에 한 줄 append,
   커서 갱신, `ev:clerk` 자기 계량 append.

### 3.6 dispatch 래퍼 (`scripts/dispatch.sh`)

codex exec 래퍼: 자기 argv에서 model/effort를 이미 아는 지점이 곧 자동 수집 지점이다
(원칙 6). `--kind`·`--scope`·`--task` 라벨을 받아 `ev:dispatch`를 자동 기록하고 dispatch id를
stdout 첫 줄에 찍은 뒤 `codex exec … < /dev/null`을 그대로 실행한다. outcome은 기록하지
않는다 — 수용 판단은 main(직접 CLI) 또는 scribe(추론)의 몫.

### 3.7 Skills (2개)

- **`waystone:checkup`** — `/doctor`형 프로젝트 진단. ledger·PRIORS·failures·커서 공백·
  최근 transcript·CLAUDE.md/메모리를 읽고: 낭비 패턴(재시도 루프·limit 정지·고아 dispatch),
  지시 위생(stale/모순 directive vs 문서), clerk 건강(공백·실패·오버헤드)을 보고.
  제안은 recommend-first, AskUserQuestion 최대 2회, 가역성 명시. 자동 적용 없음.
- **`waystone:dispatch`** — fleet-dispatch 개정판. 핵심 개정(전부 감사·가이드 근거):
  검증자는 **전부 보고, 필터는 main**(문자적 순종 모델에서 severity 상한은 발견을
  실제로 감춘다); 검증 예산은 고정 의례가 아니라 **PRIORS의 반증률에 비례**; 자기 작업
  재검증 금지(경계 검증만); GPU·메모리 안전성 서술은 중립 어휘(콘텐츠 필터 오탐 10건
  실측); 장시간 실행은 background+Monitor(포그라운드 sleep 폴링 금지); 브리프 합성 시
  공용/개별 조항 모순 사전 grep; 게이트 확인과 push는 반드시 별도 호출.

## 4. 존재하지 않는 것 (NOT-list — 재도입하려면 이 문서를 개정하라)

| 없음 | 이유 (실측) |
|---|---|
| round / round close | 범위 기준 불명 + 리뷰 대기 = 개발 정지. 사용자가 이미 continuous dispatch로 해체 |
| review packet·ingest·receipt·attestation | 회신은 채팅에 raw로(파일 저장 리뷰는 attention에서 죽는다 — whack-a-mole 6라운드 실증). pinning은 `ev:review.base` 한 필드로 충분 |
| delegate 표면·역할 바인딩 config | 라우팅은 main의 판단 + PRIORS의 증거로. config 고정은 stale 지시 사고의 원천 |
| brief 타입 사실·assurance DAG | 의도는 짧은 문서와 대화로. drift는 통제가 아니라 가시화로 |
| PreToolUse/PostToolUse/UserPromptSubmit 훅 | 매 호출 지연 + 무출력 훅 실증. 훅은 2개가 상한 |
| OPERATING CONTRACT류 상주 주입 | 8–14KB 재주입 실측. 상주는 §6의 ≤6줄뿐 |
| 손으로 쓰는 PROGRESS.md | 낡는다. worklog(생성) + ledger(사실) + PRIORS(증류)로 대체 |
| typed refusal 게이트·frozen sidecar·remote verify | 기록하되 강제하지 않는다(원칙 3) |
| codex 호스트 플러그인(waystone-codex) | MVP 범위 밖. CLI는 어차피 호스트 무관 |
| 자동 cron 설치 | 사용자가 원하면 직접 건다. 플러그인은 스케줄을 소유하지 않는다 |

## 5. MVP 이후 (기록만, 지금 만들지 않는다)

- staleness resolver: 낡은 리뷰 회신 → 현재 HEAD 기준 delta digest(clerk). base SHA
  실존 검사는 이 서비스 *내부*에 산다.
- watchman: outcome 없는 dispatch·limit 정지 감지 → telegram 통지 (대부분 결정적).
- distill 결과의 조회형 노출(위임 직전 `prior show` 습관)을 dispatch skill에 편입.

## 6. 상주 표면 (전문 — 이것보다 커지면 회귀다)

```
[waystone] tasks 3 open · directives 2 live · priors 07-31 · worklog 07-31
· live(phase): GPU 0,1만 사용
· live(durable): 리뷰 회신은 컨텍스트 유지, 파일 저장 금지
· last: v2 Pareto duo 머지, full gate green(1421)
```

## 7. 테스트 방침

테스트는 수단이다: CLI 왕복(add/set/list 다중 필터/done), log 검증(정상·기형 fail-closed),
directive 수명주기, status --inject(존재/부재 무음), scribe 전체 파이프라인
(mock backend: 커서 전진·ledger append·worklog append·lock 경합·기형 JSON→failures 격리),
digest_lite 기본. 20개 내외. `uv run pytest`.

## 8. 인수(salvage) 기록

- 감사 다이제스트 로직(digest.py, 479MB 실증) → `scripts/digest_lite.py`
- task registry 개념(플러그인 off 후에도 1,081회 자발 사용 = revealed preference) → 얇은 재작성
- fleet-dispatch skill 본문 → `skills/dispatch` 개정판
- 나머지 0.x 기계 전부 → git 이력(dev)으로 은퇴. 감사 보고서: `~/workspace/b200-2-research-cc-audit/`
