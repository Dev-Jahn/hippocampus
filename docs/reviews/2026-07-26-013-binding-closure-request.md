# Review Request — 2026-07-26-013-binding-closure

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: codex, gpt-5.5-pro
- Reviewing: 6df3fb2d521951dd5afde982fac1cf10200a92fd   (diff against b31d01b437a0ec44eae0f03aa00fbe96dd334317)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

## What changed and why

당신의 2026-07-26 회신(대상 b31d01b, 판정 CHANGES REQUESTED — Critical 0/Major 3)에 대한 폐쇄
round다. diff base가 정확히 그 SHA이므로 이 diff 전체가 그 판정에 대한 응답이다. 세 major 전부
REAL 확정(finding당 적대 verifier — 030은 tempdir 격리 동적 재현 포함 — + main 독립 라인 추적)
후 수리했고, open question 4건도 전부 ruling으로 처분했다.

- WS-GPT-029: promote start에서 RunnerExecutionDescriptor(allowlist env digest·frozen PATH로
  해석한 canonical executable 절대경로·executable bytes sha256·verifier backend/binding
  digest)를 CAS artifact로 freeze한다. raw 환경 값은 영속화하지 않는다(proxy 등 secret 회피 —
  당신의 정책 A/B 중 A를 채택하되 resume은 current allowlist 환경을 재구축해 descriptor와
  exact-match일 때만 실행). resume·stage invocation·spawn 직전 3지점에서 불일치는 typed
  refusal(새 run start 안내 — 조용한 continuation 제거). launch artifact는 schema v3로
  descriptor를 결속하고, VerifierEvidence가 descriptor/launch reference를 동반 필수로 실으며,
  invocation digest에 descriptor digest가 들어가고, reload_verifier_evidence가 두 CAS
  artifact의 canonical bytes·digest·env/executable 결속을 전량 재검증, integration decision
  reload/apply는 그 계보를 transitive mandatory로 소비한다.
- WS-GPT-030: adapters/git.py에 build_git_environment 단일 계약 신설 — ambient GIT_* 전면
  strip 후 operation-owned override allowlist(GIT_INDEX_FILE·OPTIONAL_LOCKS·AUTHOR/COMMITTER)
  만 허용, repository selector 재주입은 API가 ValueError로 거부. 어댑터 3함수(git_read_bytes·
  git_rc·git())·outcome ledger _git·brief.py/completion.py/tasks_cli.py 직호출·context.py 기존
  지역 scrub을 전부 이 계약으로 통일했다(effects는 어댑터 위임이라 자동 커버 — 별도 회귀로
  보장). 당신이 재현한 GIT_DIR(절대·상대)·GIT_DIR+WORK_TREE·stale linked gitdir 시나리오
  전부와 GIT_WORK_TREE 단독 경계까지 회귀로 고정했다.
- WS-GPT-031: closeout result authority를 stage-aware로 분리했다. 신설
  _final_result_authority(scaffold/publication 공유 단일 resolver)에서 promote는 worker-result
  존재를 오히려 거부하고(위조 차단), public verifier/decision reload 후 decision tuple 전량
  재검증(triple digest·candidate_digest·evaluation digest·reviewer digests·actor 독립성·
  verifier result OID=candidate OID), 당신의 권고대로 approved GitResultTriple digest를
  OutcomeDelta.result_digest로, target-ref apply의 effect-plan/observation을 completion refs로
  결속했다(RunCloseout schema 무확장 — 기존 completion_evidence_refs로 충분함을 확인).
  이를 위해 public reload 2곳만 terminal-safe 상태 집합({dispatch-ready,closeout-ready,
  completed,failed})을 허용하는 loader로 전환했고 operational load_dispatch_ready의 gate는
  보존(보존 회귀 포함). full-chain E2E(explore→evaluate→promote→close→completed→ledger
  게시→fresh process reload)를 신설했다.
- open Q3: review programmatic API 4종(ingest_feedback·validate_file·disposition_file·
  attach_review)+wrapper에 materialize와 동일한 canonical ProjectContext proof를 요구
  (ReviewContextRequired·CanonicalRootIsLinkedWorktree typed refusal). privatize 대신 지원
  API 유지를 택했다.

## Read these first

- PROGRESS.md의 `2026-07-26-013-binding-closure` 절 (커밋 sha·검증 방법·ruling 7건 포함)
- docs/reviews/2026-07-24-013-authority-closure-feedback.md 말미 triage 표 — 세 finding별
  검증 증거와 open Q1–Q4 ruling 원문
- waystone/runs/environment.py — RunnerExecutionDescriptor·observe_runner_executable·
  freeze/exact-match 계약 전체
- waystone/runs/engine.py — promote start의 descriptor freeze/CAS 기록, resume·spawn 3지점
  re-match, launch v3
- waystone/adapters/git.py — build_git_environment 계약과 read-only/mutating 경계
- waystone/runs/outcome.py의 _final_result_authority·_promotion_result_authority,
  waystone/runs/preflight.py의 _load_dispatch_authority 상태정책 분리
- 신규 회귀 4모듈: scripts/tests/test_promotion_verifier_execution_binding.py·
  test_git_ambient_authority.py·test_promotion_closeout_authority.py(+test_run_verify.py
  terminal reload 2건)·test_review_canonical_root.py 확장분

## Claims to attack

1. promotion verifier의 실행 environment·executable은 이제 start-frozen descriptor와
   exact-match일 때만 실행된다 — 세션 경계(새 셸·venv·tool upgrade)의 조용한 E1/B1→E2/B2
   drift 경로가 없고, 관측 가능한 path/bytes 교체는 spawn 전에 fail-closed다.
2. descriptor/launch는 기록용이 아니라 권위다 — VerifierEvidence→IntegrationDecision→apply
   체인이 launch artifact 부재·digest 불일치를 독립 사유로 거부한다.
3. engine 소유의 어떤 Git fact/effect도 ambient GIT_*로 다른 repository로 재지정될 수 없다 —
   ProjectContext가 고른 canonical repo와 이후 모든 관측·mutation·postcondition의 대상이
   동일하다(stale linked gitdir의 current-objective 부활 포함 차단).
4. successful promotion은 이제 durable하게 닫힌다 — run close 양 경로가 promote에서
   worker-result 없이 성공하고, outcome ledger 게시·fresh process reload까지 authority tuple
   (verifier/decision/apply)이 exact 재검증된다.
5. terminal-safe reload는 과잉 개방이 아니다 — operational dispatch 표면의 상태 gate는
   불변이고, 허용 상태 집합은 명시 4종뿐이다.
6. review 권위 표면은 CLI·programmatic 양쪽 모두 canonical ProjectContext proof 없이는
   mutation에 도달할 수 없다.

## Evidence already produced (mine — inspect, don't trust)

- full suite 284→303 green: 최종 조합 gate @ 6df3fb2 + 중간 조합 294 @ 1e9c0ce. 각 worktree
  전체 게이트(290·288·298·303)는 구현 기체 보고와 별개로 main이 재실행.
- 전 task base-RED를 main이 base 임시 worktree에서 독립 재현: 029 구조 RED 4/4(freeze API
  부재), 030 6건 전량 FAIL(GIT_WORK_TREE status 오염·effects update-ref B-redirect rc=128
  실연 출력 포함), 031 구조 2/2 + 교정 e2e6가 base에서 정확히 "completed final attempt has
  no worker result"로 FAIL + terminal reload 회귀 FAIL, minor 8/9 FAIL.
- 적대 verifier 3기 보고(반증 시도 관점·수리안 타당성 평가 포함)와 구현 4기 보고(property별
  RED 기록) 보존. main이 네 diff 전부 정독(hot-file 구획 준수·operational gate 보존 확인).
- fix031의 2회 BLOCKED 정지는 규율 증거로 남긴다: 기체가 브리프 ruling의 전제 오류(서로 다른
  층위 digest의 등식 요구)를 임의 우회하지 않고 동적 재현으로 반증했고, main이 코드 대조 후
  ruling을 수정했다(PROGRESS Decisions ① 참조).

## Known weak spots

- executable TOCTOU의 잔여 창: spawn 직전 path+bytes 재관측과 exec 사이의 동시(원자적) 교체는
  방지하지 않는다 — 위협모델을 "세션 간·정상 운영 drift의 fail-closed 검출"로 명시하고 known
  boundary로 기록(ruling ⑦). 원자 봉쇄가 필요하면 opened-fd exec 또는 immutable
  materialization이 필요하다.
- HOME 아래 tool config bytes(~/.codex·global git config)는 여전히 비결속 — 당신의 Q4 제안
  문구 수준으로 보증을 한정한다: "환경 변수 provenance는 결속됨. 사용자 HOME 내부 tool
  configuration bytes는 local trust boundary임." runner-config-content는 NOT_OBSERVED 유지.
- 030의 repository identity 결속(effect 전후 git_common_dir 대조)은 최소 수리에서 제외 —
  ambient scrub이 root cause를 닫지만, 호출자가 잘못된 canonical root를 전달하는 별도 위협은
  후속 hardening 후보로만 기록.
- terminal-safe reload의 허용 상태에 failed가 포함된다(rejected promotion의 사후 재검증용) —
  이 집합이 과다한지 이견 환영.
- supervisor launch v1 read 호환 창·private integration ref GC 미설계·runtime.json publication
  race(minor 등록)는 직전 round의 known weak spots 그대로 잔존.
- 이 round의 request도 release(0.11.1) round CLI로 생성 — frozen reviewer 기본값(codex,
  gpt-5.5-pro) 동결 한계 지속. 회신 identity 불일치 시 receipt pending은 정상이며 finding
  채택은 main 독립 검증으로 한다.

## Domain lens

trust-kernel 관점을 유지해달라. 이 round의 본질은 당신이 남긴 세 부등식 — preflighted
verifier environment/tool ≠ actually executed verifier environment/tool, canonical
ProjectContext ≠ ambient-Git-selected repository, successful promotion ≠ durably closeable
completed run — 를 등식으로 만드는 것이었다. 특히 ⑴ descriptor exact-match의 3지점(resume·
invocation·spawn 직전) 사이에 검증 없는 창이 남는지, ⑵ build_git_environment를 우회하는
production Git subprocess 잔존 여부(전역 sweep의 완전성), ⑶ terminal-safe reload 도입이
operational 표면의 상태 기계를 실질 약화시키는 경로가 있는지, ⑷ promote closeout의 decision
tuple 재검증에서 빠진 결속 축(예: apply observation과 ledger 기록 사이), ⑸ 이번에 닫은
programmatic review 표면 외에 canonical proof 없이 mutation에 도달하는 내부 호출이 더
있는지를 공격해달라. release 미승인의 세 사유가 폐쇄됐는지가 이 round의 판정 질문이다.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: codex
effort: high
review-target: 6df3fb2d521951dd5afde982fac1cf10200a92fd
request-digest: sha256:883abc2643cda46a0a6be27ec2488969fb533d77761299923ab4f21f5c09929a
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
