# PROGRESS

round 단위 작업 이력이 이 파일에 축적된다. 활성 task와 의존성은 `tasks.yaml`(CLI: `waystone task`)과 생성 파일 `ROADMAP.md` 참조.

## 2026-07-26-013-binding-closure

- **Goal**: 3차 재리뷰 회신(CHANGES REQUESTED — Critical 0/Major 3, WS-GPT-029·030·031 전량 REAL 확정; 직전 major 3건은 실질 폐쇄 인정) 폐쇄 + open Q3 파생 minor. w0726 wave: finding당 적대 verifier 3기 선행(codex sol xhigh/high/high — 030은 tempdir 동적 재현 포함), 구현 codex 4기(sol xhigh/high/high/medium)·worktree 격리(base 9daaa56), 전 기 main 독립 재검증(전체 게이트 재실행·base-RED 재현·diff 정독).
- **Shipped**:
  - fix/promotion-verifier-execution-binding — RunnerExecutionDescriptor(env digest·canonical executable path·bytes sha256·backend/binding digest, raw 값 비영속)를 promote start에서 CAS freeze, resume·stage invocation·spawn 직전 3지점 exact-match typed refusal(재시작 안내), launch schema v3, VerifierEvidence에 descriptor/launch reference 동반 필수, invocation digest 결속, reload 전량 재검증, decision validation이 reference 부재 거부 (done; 1e9c0ce, 원 7518dce)
  - fix/git-ambient-environment-authority — adapters/git.py에 build_git_environment(ambient GIT_* 전면 strip + override allowlist{INDEX_FILE·OPTIONAL_LOCKS·AUTHOR/COMMITTER}, repository selector 재주입 API 거부) 신설, git_read_bytes/git_rc/git() 3함수·outcome._git·brief.py·completion.py·tasks_cli.py 직호출·context.py 지역 scrub 전부 통일 (done; 5714fae, 원 da0359e)
  - fix/promotion-closeout-result-authority — stage-aware _final_result_authority(scaffold/publication 공유): promote는 worker-result 존재 거부(위조 차단)+public reload로 decision tuple 전량 재검증 후 approved GitResultTriple digest를 result identity로, apply effect-plan/observation을 completion refs로 결속(schema 무확장); preflight에 _load_dispatch_authority 상태정책 분리 — terminal-safe loader({dispatch-ready,closeout-ready,completed,failed})는 public reload 2곳 전용, operational gate 보존(+보존 회귀); full-chain E2E(explore→evaluate→promote→close→completed→ledger 게시→fresh reload) 신설 (done; 73391ff, 원 ed0bde4+490218d)
  - fix/review-programmatic-context-proof — review programmatic API 4종+wrapper에 canonical ProjectContext proof(_canonical_review_root: ReviewContextRequired·CanonicalRootIsLinkedWorktree typed refusal), materialize 선례와 계약 통일 (done; 6df3fb2, 원 5b9534c)
- **Gates**: full suite 284→**303** green(중간 조합 294 @ 1e9c0ce + 최종 조합 OK @ 6df3fb2, 각 worktree 게이트 290·288·298·303 main 재실행). 전 task base-RED main 독립 재현 — 029 구조 RED 4/4·030 6건 전량 FAIL(GIT_WORK_TREE status 오염·effects B-redirect 실연)·031 구조 2/2+e2e6 정확한 거부 문구+terminal reload FAIL·minor 8/9.
- **Incidents**: fix031 2회 BLOCKED — 둘 다 정당한 정지(임의 우회 없음). ① 브리프 R3의 전제 오류(candidate producer digest≡GitResultTriple digest 등식 — 서로 다른 층위 identity)를 기체가 동적 재현으로 검출 ② 수리에 필요한 verify.py가 병행 fix029에 예약. 각각 ruling 수정·scope 한정 해제로 재발진 회수, 작업 손실 0(worktree WIP 보존 + main이 dev 사전 동기화 머지).
- **Decisions**(ruling 자율권): ① 031-R3 수정 — 등식 요구 철회, result identity=decision이 결속한 approved GitResultTriple digest(옵션 A candidate publication migration은 파급 범위로 기각) ② verify.py terminal-safe reload 허용 — public reload 2곳만, load_dispatch_ready 전면 terminal-open 기각 ③ Q1=start-time freeze+resume exact-match(raw env 비영속 — secret 회피) ④ Q2=GitResultTriple digest ⑤ Q3=ProjectContext proof 요구(privatize 기각) ⑥ Q4=HOME 내 tool config bytes는 solo-local trust boundary(보증 문구 수준 확정) ⑦ executable TOCTOU 위협모델=세션 간 drift 검출까지(재관측→exec 동시 교체 창은 known boundary).
- **Review**: 리뷰어가 남긴 세 부등식(preflighted verifier env/tool ≠ 실행 env/tool, canonical ProjectContext ≠ ambient-Git-selected repository, successful promotion ≠ durably closeable run) 전량 폐쇄 — 재리뷰 요청 이 round에서 게시(이번 회신이 release 재판정).
- **Next**: 재리뷰 회신 대기 → 실 release(owner; 잔여 판단 2건은 chore/013-release-prep notes) → feat/lifecycle-maintenance-path(major) → fix/runtime-publication-race(minor) → GC 계열(candidate-contexts + private integration ref durable 누적).

## 2026-07-24-013-authority-closure

- **Goal**: 재리뷰 회신(CHANGES REQUESTED — Critical 0/Major 3, WS-GPT-026·027·028 전량 REAL 확정) 폐쇄. w0724 wave: codex 3기(sol xhigh/high/high) 병렬·worktree 격리(base 1ded7e7), 전 기 main 독립 재검증(전체 게이트 재실행·base-RED 재현·diff 정독).
- **Shipped**:
  - fix/runner-environment-provenance — waystone/runs/environment.py 명시 allowlist builder+결정적 env digest, invocation digest·supervisor launch v2·promotion verifier evidence에 결속, 전 subprocess 명시 env= 전달, 실행 파일 탐색도 frozen PATH, detached 자식이 digest fail-loud 재검증, PYTHONPATH 하네스 주입 폐지(R5 — runner는 codex exec라 waystone import 불요; supervisor bootstrap만 package-root cwd 명시) (done; 7465ae7, 원 e3f38a1+3bb8d30)
  - fix/promotion-private-integration-ref — promotion target을 checked-out branch에서 refs/waystone/integration/<brief_id>로(최초 zero-OID CAS 생성·기존 ref 보존·expected-old CAS 의미론 유지), PC-17 _checked_out_target_ref를 staged promotion apply 직전에 강제, CLI 완료 보고에 private ref+live-tree delivery 미수행 명시, e2e6 blindspot 역단언 교정(public OID·bytes·index·status 불변+private ref 이동) (done; e179adb, 원 3646a65; ruling: promotion≠delivery — open Q1 권고안 채택, 0.13 public branch 불이동)
  - fix/review-canonical-root-authority — review 전 subcommand가 _review_context front door(resolve_project_context) 경유, linked worktree mutation·명시 --root <linked> 모두 canonical_root_is_linked_worktree typed refusal, materialize는 canonical ProjectContext proof 없이 tasks_cli.cmd_add 진입 불가 (done; 3075205, 원 c59f539)
- **Gates**: full suite 271→**284** green(wave 조합 gate + registry 커밋 전 재확인). 전 task base-RED main 독립 재현 — 026 구조 RED(environment 모듈 부재)·027 신규 3건 구조 RED+e2e6 FAIL on base·028 4/4 FAIL(base에서 stale disposition 기록·linked materialize가 실제 성공하는 장면 실연). 각 worktree 전체 게이트도 main이 재실행(274·275·277).
- **Incidents**: 없음 — 3기 전부 1회 발진 DONE(재발진 0). 범위 밖 발견 1건: supervisor runtime.json 0-byte publication race(간헐 test_run_cancel 실패, 기존 결함) → fix/runtime-publication-race(minor) 등록.
- **Decisions**: open Q2 allowlist ruling 5건(R1–R5)을 브리프에 pre-register 후 구현 — GIT_* 전면 제거·PYTHONPATH/uv-redirect 제거(UV_CACHE_DIR은 통과하되 digest 관측)·기본 allowlist(PATH/HOME/locale/TERM/TMPDIR/USER/SHELL/proxy)·HOME 격리 0.13 비대상·하네스 주입은 제거 선택(필요성 실측 결과 불요). 외부 도구 설정 파일 내용 digest는 known boundary(비-범위 유지).
- **Review**: 리뷰어의 release 보류 사유(major 3) 전량 폐쇄 — 재리뷰 요청 이 round에서 게시(이번 회신이 release 판정 질문).
- **Next**: 재리뷰 회신 대기 → 실 release(owner; 잔여 판단 2건은 chore/013-release-prep notes) → feat/lifecycle-maintenance-path(major) → M1-B minor 재평가 → GC 계열(candidate-contexts + private integration ref durable 누적).

## 2026-07-23-013-provenance-closure

- **Goal**: 직전 리뷰 회신(CHANGES REQUESTED — Critical 0/Major 3, WS-GPT-023·024·025 전량 REAL 확정) 폐쇄 + 알려진 버그 잔여(fix/run-start-refusal-diagnosability) + open Q2 + README 재작성. w0723c wave: codex 4기(sol xhigh/high/medium) 병렬·worktree 격리, finding당 적대 verifier 3기 선행, 전 기 main 독립 재검증(base-RED 재현·스위트 재실행·diff 검독).
- **Shipped**:
  - fix/promote-verifier-candidate-context — evaluate/promote 외부 검증을 exact candidate materialization cwd에서 실행, launch에 OID·fingerprint·RunSpec digest 결속+전후 fail-closed 재검증, post-hoc pass replay adapter 제거, content-aware fixture·양방향 경계 회귀 (done; 057708a, 원 fa57c41)
  - fix/promote-authority-published-reload — 승격 authority를 store/CAS reload된 발행 artifact로만(apply 직전 재reload 포함), execute_stage→_execute_stage private화(주입 표면 제거), unpublished-tuple negative (done; 057708a, 원 c54c72e)
  - fix/disposition-current-objective-binding — objective의 HEAD ancestry+current fact digest/binding 대조, objective-superseded typed refusal, 과잉 거부 경계 보존 회귀 (done; 1081d11, 원 cac27f2; Q3 ruling: evaluate selector 확장 안 함)
  - fix/run-start-refusal-diagnosability — failure envelope에 안전한 detail 채널+preflight_failed, authority/preflight/internal 3부류 기계 구별, 비밀 비노출 상한, exit code 불변 (done; 0610253, 원 2de2132)
  - chore/promote-retry-failure-taxonomy — semantic reject는 동일 candidate terminal(발행 evidence가 경계), process/malformed만 lineage retry 보존 (done; 057708a, 원 e3f7771)
  - chore/013-release-prep — README 0.13 정합 재작성(실 CLI 대조·보증 5종 절·Status 절) (touched; 2adb1b4)
- **Gates**: full suite 258→271 green @ 057708a(wave 조합 gate) + README 후 271 재확인. 전 task base-RED main 독립 재현(disposition 5건·transport KeyError+cli 5건·promote_evidence 4건+구조 ImportError). 검증 방법 상세: 각 task result + scratchpad reports(w0723c-*).
- **Incidents**: 발진 직후 네트워크 단절로 3기 동시 사망(stream disconnected) — B는 커밋·보고서 기완료로 손실 0, A는 재발진(211k 토큰 소실), C는 dirty 보존 후 이어받기 재발진으로 회수. test_run_cli.py import 충돌 1건(A∩C) union 해소.
- **Decisions**: WS-GPT-024 severity ruling — verifier의 minor 이견(canonical 경로 도달 불가) 기각하고 major 유지(round의 binding 목표·release gate 결속); retry taxonomy는 추측성 enum 대신 durable 경계(발행 semantic evidence) 채택; 후속 chore/candidate-context-materialization-gc(minor) 등록.
- **Review**: 리뷰어의 release 미승인 사유(major 3) 전량 폐쇄 — 재리뷰 요청 이 round에서 게시.
- **Next**: 재리뷰 회신 대기 → 실 release(owner; 잔여 판단 2건은 chore/013-release-prep notes) → feat/lifecycle-maintenance-path(major) → M1-B minor 재평가.

## 2026-07-23-013-review-closure

- **Goal**: 0.13 외부 아키텍처 리뷰 회신 ingest → 전 finding 코드 검증·triage → 파생 작업 전량 폐쇄. 하루 두 wave(w0723 blocker 2기 + w0723b 6기 병렬), 전부 codex exec(sol high/xhigh) + main 독립 재검증.
- **Shipped**:
  - fix/promote-actor-evidence-separation — promote 3-action evidence alias 제거·typed 결속·risk-gate 배선 (done; a2c661d, 수리 2회전)
  - fix/review-disposition-authority-binding — disposition/validation exact authority 결속 (done; 16350e0)
  - fix/brief-role-model-realignment — fact 4-role 개정·ADR-0015·재adopt (done; d6a8990+fc4c20a)
  - feat/workbrief-scaffold — semantic draft 입력 경로·run_scaffold.py (done; 027ec38)
  - docs/013-v1-execution-scope — v1=external Codex 전용 명시 (done; 3ed33ae)
  - spike/013-lifecycle-dogfood — S2 PASS·S3 FAIL(관료제 실확인)·S1 차단 (done; repo 수정 0)
  - fix/promote-reject-terminal-closeout — reject terminal closeout·retry lineage (done; 1e94eb7)
  - chore/store-transient-read-ioerror — WAL/SHM 선제 생성 root-cause 수리 (done; 5a63f82)
  - chore/013-release-prep — 핵심분(SHIP_PATHS·smoke·v1 docs) 완료, 실 release는 owner 대기 (touched; 3ed33ae)
- **Gates**: full suite 234→258 green @ 027ec38 (wave 조합 gate) · 독립 release 리허설 PASS(/tmp 클론에서 release-to-main.sh 전체 실행, main@20359b1 빌드) · brief adoption record 결속 검증(fc4c20a) — 상세는 이 파일의 w0723/w0723b bullet.
- **SSOT**: PROJECT_BRIEF `commitment/roles-over-model-names` 4-role 개정 + 재adopt, ADR-0015 ratified.
- **Decisions pending**: 실 release 실행 시점(owner — decision task 미등록, ruling 자율권 정책에 따라 chore/013-release-prep notes에 기록).
- **Review**: requested (docs/reviews/2026-07-23-013-review-closure-request.md); 직전 회신은 ingested (REAL 8 — blocker 2/major 2/minor 2/관찰 1 + 기존 승계 1, 전량 폐쇄).
- **Next**: dogfood 파생 major 2건(feat/lifecycle-maintenance-path·fix/run-start-refusal-diagnosability) → M1-B 후속 minor 재평가(새 disposition 기준) → 실 release.

## 2026-07-22-013-intent-control-plane

- **Goal**: 사용자 mandate(외부 리뷰 2건 + omniphysics realign 실증)에 따른 **0.13 전면 재설계** — M1-B trust kernel 위에 intent control plane 6축을 얹어 새 정의("불확실한 가설은 탐색 가능하게, 검증·리뷰 비용은 승격 위험에 비례, worker에게 목적에 충분한 맥락")를 구현. 기존 0.12 계획·M1-C 분해 폐기. mandate verbatim: `dev_docs/0.13-redesign-mandate.md`(binding) + `dev_docs/reference/{omniphysics-realign,methodology-notes}.md`.
- **설계**: codex 설계 기체(sol xhigh)가 `dev_docs/0.13-redesign-plan.md` 작성(D1–D18·자산 처분표·task 7·e2e 7) → main 인수 + §12 ruling(Q1–Q6·R1–R4). 계획 자체가 비례 검증을 자기 적용(1099 suite green을 exit에서 제외, §11 금지선).
- **Shipped (구현 6 + fix 3 + gate)**: A1 ProjectFrame typed facts·WorkBrief provenance·CompletionContract(evidence bytes 재검증) · B1 finding claim/validation/disposition 3-immutable-chain(REAL→task 자동 연결 구조 제거) · A2 ProjectContext·production RunAssembly·RunSpec/store v2·context_request→waiting_context resume FSM · B2 stage별 exact action DAG(explore 경량/evaluate read-only/promote 전체 사슬)·candidate/evaluation freeze·promotion lineage·비초기화 review cycle budget·marker v2 · C1 OutcomeDelta·`refs/waystone/outcomes` first-parent CAS ledger·objective-first status/advisory · C2 canonical surface 단일 cutover(brief/run/review/status·ideate framing/realignment 이원화·legacy delegate/round/ssot/lanes **30파일 삭제**·docs 정합) · fix/013-gate-closure(G013-01~05) · fix/013-worker-result-schema-compat(G013-06) · fix/013-worker-result-null-decode(G013-07).
- **Gate (5회 판정 사이클, `dev_docs/0.13-gate-evidence.md`)**: 1차 FAIL(claim 5 — 최종 조립 seam 무소유) → 수리 → 2차 e2e 4·5·6 PASS·신규 G013-06 → 수리 → 3차 G013-07 → 수리 → 4차 제품 finding 0(gate 하네스 오류) → **5차 PASS**: real backend smoke 완주(run `019f885f-…` completed·marker v2·candidate publish·ledger `1d506b46`·digest 3종 CAS 일치·context-resume 야생 실증 포함). **e2e 7/7 + suite + smoke + audit 전부 충족 — 0.13 exit PASS** @ dev `60c6305`. finding 전량이 새 disposition flow(claim→validation→disposition→선택 materialize)로 처리됨 — 자동 task화 0.
- **suite**: 1099 → **234**(보존 kernel + 신설 focused만; 목표했던 수축). flaky 1건(G013-04 폴링)은 main이 직접 결정론화(25/25).
- **운영**: 새 orchestration policy 첫 적용(codex exec 전면 라우팅, sol/luna·effort 매 작업, ruling 자율권). 사고 1건 — A1 1차 기체가 worktree의 waystone 하네스로 재위임(336k 토큰 손실) → 재위임 금지 조항 표준화(메모리 §3.4) 후 재발진 성공.
- **Next**: chore/013-legacy-migration-script(일회성 — SSOT.md→PROJECT_BRIEF.md·ssot:→brief:·waystone repo 자신 포함), M1-B 후속 minor 잔여 재평가(새 disposition 기준으로), release 준비는 별도 결정(README·release-to-main.sh SHIP_PATHS가 신 surface 미반영).
- **Blocker 폐쇄 (2026-07-23 w0723 wave)**: 리뷰 blocker 2건 같은 날 수리·머지 —
  ⑴ fix/review-disposition-authority-binding(dev 16350e0): append_validation/disposition에 root 필수화·objective_ref exact commit brief fact 재도출·evidence CAS/Git authority 결속·materialize 재검증·합성 fixture 실물화; P3(binding_digest↔frozen profile 대조)는 frozen plan에 reviewer/coordinator binding 부재로 부분 NO-GO.
  ⑵ fix/promote-actor-evidence-separation(dev a2c661d): promote 3-action의 evidence_digest alias 제거 — independent-verify는 실 read-only verifier runner 결과의 typed VerifierEvidence(run-owned candidate ref는 exact-object cat-file 검증, 위임 경로 기본값 보존), integration-decision은 exact tuple 결속 coordinator artifact, adversarial-review는 lineage-결속 ReviewerEvidence 소비, accepted-risks→declared_risks 배선(P4), review attach 최소 public 연결(P5), R1-R3 거부 회귀 포함. 수리 2회전(1회전 sandbox가 e2e 검증 불가 → checkout_identity digest화·verifier worktree 전제 충돌을 main root-cause 후 bypass 재발진).
  운영: codex sol high/xhigh 3발진, 양 기 모두 환경성 suite RED에서 정직 BLOCKED 보고(무단 완화 0) → main 독립 재검증(호스트 suite + base RED 재현)으로 수용. suite 234→249. 후속 등록: fix/promote-reject-terminal-closeout·chore/store-transient-read-ioerror.
- **리뷰 잔여 전량 폐쇄 (2026-07-23 w0723b wave, 6기 병렬·sandbox 없음)**: suite 249→**258**.
  ⑴ fix/brief-role-model-realignment(d6a8990+fc4c20a) — fact 4-role 개정·ADR-0015·brief adopt 완료(adoption record fc4c20a 결속 검증).
  ⑵ chore/013-release-prep 핵심분+docs/013-v1-execution-scope(3ed33ae) — SHIP_PATHS 정리·projected smoke 4종·v1=external Codex 명시; 독립 리허설로 release 기계 end-to-end 증명(/tmp 클론 main@20359b1 빌드). 잔여: policy-schema rule 처분·버전 핀·실 release(owner).
  ⑶ chore/store-transient-read-ioerror(5a63f82) — root cause 특정(0-byte WAL/SHM 선제 생성→IOERR_SHORT_READ 522)·최소 수리·540회 무오류.
  ⑷ fix/promote-reject-terminal-closeout(1e94eb7) — reject 전용 exact-tuple 검증·apply 전 DAG 종료·failed terminal·seal 전 retry_of 재시도.
  ⑸ feat/workbrief-scaffold(027ec38) — semantic draft 입력 경로(run_scaffold.py fail-closed)·/run skill 갱신, 수제작 경로 보존.
  ⑹ spike/013-lifecycle-dogfood — S2 PASS(major+accept-risk→task 0 실증) / **S3 FAIL(관료제 우려 F5 실확인)** / S1 차단(불투명 refusal). 파생 등록: feat/lifecycle-maintenance-path(major)·fix/run-start-refusal-diagnosability(major).
  전 기 main 독립 재검증(suite 재실행+diff 검독) 후 수용, 충돌 0(파일 소유표), worktree/branch 전량 정리.
- **Review (2026-07-23 회신 ingest)**: 외부 GPT 아키텍처 리뷰 수신·verbatim 보존(`docs/reviews/2026-07-22-013-intent-control-plane-feedback.md`, 22,550 bytes byte-exact; request 미발행 수동 round라 binding 부재 — configured feedback 미집계). 정식 판정: **아키텍처·Explore/Evaluate·disposition 방향 승인 / Promote provenance CHANGES REQUESTED / release 미승인**. triage(전 건 코드 대조): REAL 8·REJECTED 0·NEEDS-RULING 0 — **blocker 2 신규**(fix/promote-actor-evidence-separation: 3개 promote action이 evaluation evidence digest 하나로 alias + risk-gated review 비활성 / fix/review-disposition-authority-binding: objective·evidence shape-only 검증) + major 2(fix/brief-role-model-realignment — 6-role fact ruling 자율확정·feat/workbrief-scaffold) + minor 2(spike/013-lifecycle-dogfood·docs/013-v1-execution-scope); release manifest 지적은 기존 chore/013-release-prep 승계. 권고 마감 순서: 신기능 동결 → blocker 2건 → brief 정합화 → dogfood 3종 → release projection.

## 2026-07-21-m1b-vertical-slice

- **Goal**: M1-B one-task vertical slice 전체 — 분해 설계(main)부터 신엔진 15 task 구현·exit gate까지 하루 완주. main은 관제탑(브리핑·회수·ruling·인수), 구현은 codex 기체 12기(wave A~E ultra, bridge부터 사용자 지시로 high) + opus 보조 2기(fixture 수리·smoke).
- **분해 설계** (`docs/m1b-slice-decomposition` → `dev_docs/m1b-slice-plan.md`): 결정 D1–D10, **PC 귀속표(M1-B분) 확정**(ADR-0014 Amendment §2 위임 이행 — PC-14~22·27~29·31 부분 + 신규 의무 I-10·E-03/06/08/09 잔여·ADR-0013 3행·취소 절), fixture 8건 소유 map, 편입 예약 7건 배치(network-resilience는 M2 배치 + S1–S3 기질을 M1-B exit에 편입).
- **Shipped (구현 15 + gate)**: run-store-kernel(SQLite WAL store+CAS+append-only+UUIDv7+artifact CAS, JW-GPT-014 단일 transaction 증명) · run-domain-roles(4-role+profile v1 adapter) · review-runs-uuid-owner-directory(ADR-0009 canonical layout live 가동, JW-GPT-015 구조 소멸, legacy test ±8/48 main 승인) · run-lease-fencing(fixture 6·7·8, expiry≠권한) · run-spec-planning(frozen RunSpec·snapshot 무변조 — 가드 없는 status의 index 변경을 대조 실험으로 실증) · run-verification-preflight(WS-GPT-102 위조 toolchain 세탁 반증 폐쇄, env-prep digest 편입) · run-effect-protocol(5단계·kind 5종·unknown-effect 정직 대기·crash 결정표, D9 store additive 확장) · run-supervisor-identity(detached 생존 opus probe 실증 — S2) · run-observability(fixture 5, 100회 byte probe) · run-cancel-quiescence(fixture 4 5-case, EXITED∧reconcile∧principal 3중 게이트 — **2026-07-19 실사고 시나리오 구조 차단**, store-일관 위조 probe까지 fail-closed) · delegate-prompt-i10-surface-strip(I-10 최소 프롬프트, routing_note 투영 제거) · run-actions-transport(5중 검증·I-03 위조 probe 13/13·recoverable 분류 — S3) · run-store-permission-hardening(0700/0600/0400·no-follow — 기존 lock symlink 추종 실차단, umask fail-closed probe) · run-verify-decision(PC-16/17/20/21/22 — 판별성 probe·11-artifact tamper matrix) · run-cli-bridge(엔진 조립+CLI, e2e 실 detached 완주) · **gate/m1b-exit: exit 9항 전항 충족**(`dev_docs/m1b-exit-evidence.md`) — 실 backend smoke PASS(codex exec runner 완주·ref OID 일치·live tree 불변).
- **검증 체계**: 기체 suite → main 독립 재실행 → **opus 반증 검증 10기**(전 기 blocker/major 0; 직접 probe: raw sqlite 우회·stale principal 20경로·위조 세탁·detached 생존·100회 byte 불변·파괴 4벡터+위조 상태·판별성·I-03 13종) → 병합 조합 suite. suite 838→**1088** (신규 계약 테스트 250, legacy 구식화 8건 승인 대체).
- **Gates**: full suite 1088 rc=0 (병합 게이트 ×15 전부 green; 병합 사고 1건 — run_tests.py 충돌 마커 커밋 후 즉시 수리 7ec778a, 이후 충돌 검사 분리 절차 채택. perm·verify의 0400↔fixture 충돌 2건은 ruling으로 주입 방식 갱신, 계약 단언 불변).
- **SSOT**: unchanged. **ADR-0010 Amendment**(v1 acceptance adapter·review decision null 보존·no-retry 기본값 — 3자 충돌 해소) + `docs/run-engine-formats.md` v1 format registry 확정.
- **처분**: JW-GPT-014/015 blocked 2건 legacy-residual 종결(신규 시스템 재현 불가 fixture 보증). 후속 등록 12건(가장 중요: fix/effects-runner-absence-seam·fix/patch-effect-approval-reconcile-binding·fix/lease-principal-project-executor-binding·feat/run-production-assembly). 사용자 지시 반영: codex effort ultra→high(profile·memory 갱신).
- **Decisions pending**: decision/legacy-settlement-additional-cohort(minor, 사용자).
- **Review**: requested (docs/reviews/runs/<uuid>/… — canonical layout 첫 적용 여부는 release 하네스 기준, 실제 경로는 request 참조).
- **Next**: M1-B 리뷰 회신 처리(codex high) → M1-C compatibility cut-over 분해(front door delegate 경로 전환·legacy characterization·_git 정렬·production assembly·DB schema upgrade smoke·PC-24).

## 2026-07-20-m1a-review-closeout

- **Goal**: m1a-split packet의 codex ultra 리뷰(major 5) 처리 — 검증·처분·잔여 수리. M1-A exit 방어전.
- **판정** (opus verifier 5기 반증, 전 건 실증): **major 5 전부 minor 강등, blocker 0, M1-A exit 유지.** 401 커밋분리(핵심 기각 — fresh clone에서 diff-0 재생, 잔여 provenance 위생) · 402 orphan import(REAL latent·소비자 0·리뷰어 수리안이 회귀임을 실증) · 403 dict-patch(소비자 0·전달 기술 불가) · 404 -m/runpy(그 표면 소비자 0·계약 표면 무결) · 405 manifest 재-pin(**ID별 10건 전수 추적 = 무감사 변경 0**·시작 재정의 정당·자기참조 불성립).
- **Shipped**: 문서 2건 main 직접(`docs/m1a-provenance-hygiene`·`docs/m1a-manifest-approved-diffs` — manifest가 ID별 −1/+9 ledger+재정의 ruling 보유한 self-authorizing 기록으로, 965a9ef) + w6 기체 3건(`fix/shim-orphan-child-rebind` identity-보존 rebind·`fix/run-tests-selfdir-bootstrap` 4표면 복원·`docs/shim-supported-patch-surface`, 0247677). triage 결속 20c3a98.
- **Gates**: w6 표적 41 rc=0 + full gate 838 rc=0. **잔여 blocker 0·major 0.**
- **SSOT**: unchanged.
- **Decisions pending**: decision/legacy-settlement-additional-cohort(minor, 사용자).
- **Review**: requested (docs/reviews/2026-07-20-m1a-review-closeout-request.md).
- **Next**: **M1-B 착수** — vertical slice 분해(main 설계). 편입 예약: delegate-prompt-i10-surface-strip(값 채널 포함)·review-runs-uuid-owner-directory·#014/#015 fixture·ADR-0013 fault 3건·delegate _git↔adapters.git 정렬·(신규) shim orphan guard의 real-package parent 경로.

## 2026-07-20-m1a-split

- **Goal**: remediation packet의 codex ultra 리뷰 처리(6 finding) → 수리 wave w5 → **M1-A 기계 분할 전체 집행·exit 충족**. main은 관제탑 + ADR 정정 직접 집행.
- **리뷰 처리** (round 2026-07-20-review-remediation packet 회신): codex ultra 1b/5M → **opus verifier 6기 반증 후 blocker 0 생존, 2M/4m 확정** — 301 blocker 귀결 기각(게이트 성립, M1-A 승인 유지·전사 누락만 실재), 302 major 기각(위협 혼동 — exact-pin이 코드 유래 신규 투영을 실방어함을 역실험 입증), 303 fail-open 기각(판정 경로 fail-closed 입증)→위생 minor, 304② **수리 회귀 REAL**·① 기각, 305 REAL(신규 인접), 306 실재·PC-31 과대→minor. 문서 2건 main 직접(ADR-0014 **Amendment 2 Addendum 2**: E-08 7행 카테고리 폐쇄 + I-10 보장 범위 명문화, 214a6fc). triage 결속 99d7730.
- **w5 수리 wave** (codex ultra 3기): `fix/linked-read-selector-init-gate`(13587b4, ruling: 명시 root=자체 초기화) · `fix/review-binding-writer-identity`(1028217, typed fail-closed) · `fix/sunset-live-profile-overreach`+`fix/worktrees-cache-ancestor-symlink`(568eebc, live 제외 1행 + detector 조상 lstat·_mkdir_or_refuse owned_root containment 2겹). full gate 838 rc=0. 부수 교훈: 병렬 기체 공용 /tmp/suite.log 인용 오염 → 기체별 고유 로그 경로 강제.
- **M1-A 기계 분할** (5기 순차·병렬, 전 기 [m1a-move]/[m1a-adapter] 커밋 분리 + source-identical 기계 증명 + suite 838 green + front-door byte-identity):
  - `chore/m1a-suite-manifest-pin` — test-ID 838 manifest(f513a4e, 재고정 c6ba063) · `docs/m1a-mechanical-split-plan` — 분해 계획(91b008c)
  - `chore/m1a-package-skeleton` — waystone/ 골격 + dispatcher→cli(b0a9283, 21/21)
  - `chore/m1a-core-project-split` — common.py 84 선언→core/project/adapters.git + 102-name shim(_CommonShim monkeypatch bridge·import-shadow 보존, c597b6b)
  - `chore/m1a-runs-move` — delegate.py 3916줄→runs(aed8b3c, 160/160·234-name bridge·I-10 oracle 전후 green; git 헬퍼 정렬은 의미 차이로 정당 거절→M1-B 후보)
  - `chore/m1a-registry-move` — tasks.py→project/tasks_cli(ced172e, 32/32·49-name)
  - `chore/m1a-test-suite-split` — 21.8k줄→13 모듈+support.py, 집계 adapter(41e315e, 84 클래스/838 ID 각 1회)
- **M1-A exit 판정 (main): 충족** — ① 분할 5/5 ② core 상방 import 0(최종 상태 재검) ③ manifest 838 delta 0 + full gate 838 rc=0(병합 후 main 실행) ④ known-debt 대비 신규 위반 0(감시 테스트 green) ⑤ front door 단계별 byte-identity.
- **Gates**: 수확별 표적 게이트 rc=0 ×9 + full gate rc=0 ×3(w5 마감·m1a 병합·시퀀스 중간). 병렬 hot-file 충돌 0.
- **SSOT**: unchanged (views 재생성).
- **Decisions pending**: decision/legacy-settlement-additional-cohort(minor, 사용자).
- **Review**: requested (docs/reviews/2026-07-20-m1a-split-request.md).
- **Next**: M1-B 착수 준비 — vertical slice 분해(편입 예약: delegate-prompt-i10-surface-strip·review-runs-uuid-owner-directory·#014/#015 fixture·ADR-0013 fault 3건·git 헬퍼 정렬).

## 2026-07-20-review-remediation

- **Goal**: 두 round packet(fleet-fix-wave·ruling-execution)의 codex ultra 적대 리뷰 회신 전량 처리(사용자 지시: 리뷰어를 chatgpt→codex ultra로 대체) + 수리 wave w4 + M1-A 착수 판정. main은 관제탑(triage·인수·ADR·머지·게이트).
- **리뷰 처리**: 회신 2건 verbatim ingest, **13 finding 전량 finding당 opus verifier(clean-subagent) 반증 후 확정** — REAL 12(1건 기해소)·PARTIAL 1, severity 재조정 2(major→minor). triage를 feedback 파일에 결속, 수리 task 11건 등록(origin=review-*). 81bd177.
- **ADR-0014 Amendment 2** (main 직접, blocker WS-GPT-201·202 폐쇄): known-debt 목록 고정(E-08 #473/#510/#516·E-09 #486·특성화 공백), exit ②="debt 대비 신규 위반 0", M1-A=순수 기계 ruling, suite test-ID manifest pin, I-10 경계(WAYSTONE_REPORT stanza만).
- **Amendment 2 Addendum** (main 직접): w4-i10이 **현행 I-10 위반 확정**(rendered prompt가 status·milestone·round·anchor·routing_note 전달, main 독립 재현) — known-debt 편입, 수리는 M1-B(`fix/delegate-prompt-i10-surface-strip`), 특성화는 pinned-debt 형태 재규정. dev d95931b.
- **Shipped** (implementer=raw codex exec ultra, worktree 6기; 1차 발사 5기 네트워크/DNS 장애 격추 → 잔존 감사·승계 재발사, litter는 스톨 발견 후 3차):
  - `fix/improve-dual-prefix-archive-reader` — 역사 아카이브 reader dual-prefix 복원, 보존 corpus 21건 0→21, 실물 corpus 회귀 테스트. dev b6bde56
  - `fix/i10-prompt-minimality-characterization` — **blocker WS-GPT-101 폐쇄**: pinned-debt 3중 고정(양성·debt 5종 TASK_BLOCK exact equality·template SHA-256 전문 oracle), RED 증명 3종. dev a6e9ea0
  - `fix/review-binding-generation-collision` — 3종 수리(regex [1-9]\d*·generation 유일성 하중 규칙·duplicate-key reader), alias 충돌 시 pending/unknown(PC-10), settled 3건 회귀 보존. dev 6298c3d
  - `fix/sunset-preserved-profile-divergence`+`fix/sunset-marker-container-symlink` — detector fail-open 2건 typed 거부 전환(profile raw-bytes 비교·container no-follow), 부수: MarkerTests HOME 격리. dev c1cfb6a
  - `docs/plan-m1c-exit-supersession`·`docs/promoted-reverse-closure-and-pc31`·`docs/matrix-regen-adr13-obligations` — M1-C comparator supersede(byte 보존 증명)·85클래스 양방향 폐쇄(39+46)+**PC-31** 승격·matrix stale 0(AST 증명)+ADR-0013 fault 3건 M1-B fixture 등록. dev 0ae2457
  - `fix/linked-read-lock-litter` — linked read의 lock 이전 canonical 정규화(재탐침 증명) 또는 typed 거부, linked checkout 생성물 0(ADR-0011). dev ee2ea20
- **M1-A 착수 판정** (`decision/m1a-start-approval`, 사용자 위임 조건 집행): 미해소 blocker 0 확인 → **승인**. 착수 시 suite manifest pin(830 @ ee2ea20)부터.
- **Gates**: 머지별 표적 게이트 rc=0 ×6 + 최종 full gate **830 tests rc=0**(817→830: reader+1·binding+4·sunset+4·i10+1·litter+3). hot-file 충돌 0.
- **SSOT**: unchanged (views 재생성만).
- **Decisions pending**: decision/legacy-settlement-additional-cohort(minor, 사용자 — 동종 3건 marker 포함 여부).
- **Review**: requested (docs/reviews/2026-07-20-review-remediation-request.md) — 이번 라운드부터 binding이 codex:gpt-5.6-sol ultra 동결(profile 전환 반영).
- **Next**: M1-A 분해 착수 — suite test-ID manifest pin → 기계 분할 단위 확정(PC 마일스톤 귀속표 참조) + feat/review-runs-uuid-owner-directory M1-B acceptance 편입.

## 2026-07-20-ruling-execution

- **Goal**: 사용자 ruling 4건(M0 exit 노선 B·settlement 방식·readme 처분·migration sunset) 전량 집행 — fleet w0720b(codex 7기 병렬 + 경량 재심 1기). M0 exit blocker 2건 폐쇄가 핵심.
- **Ruling 기록**: `decision/m0-exit-verdict`(보류 승인 + **노선 B**: 합격 기준 = invariants+accepted ADR+승격 계약, legacy 828 기본 폐기, 승격 원칙 = git-tracked 기록 연속성) · `decision/pre-header-feedback-settlement-method`((a) marker 채택) · `decision/delegate-readme-disposition`(삭제, f2bedea). `fix/porting-ledger-grade-gate-executability`는 노선 B로 대체 소멸(dropped).
- **Shipped** (implementer=external-runner/codex:gpt-5.6-sol ultra, worktree 7기 @ 8392d5a):
  - **ADR-0013** operational threat model — 8/8축 완결(신규 3: child env closed allowlist·lease principal token CAS·permission/symlink fail-direction; 흡수 4). M0 exit 재심 조건 ①
  - **ADR-0014 + `docs/promoted-contracts.md`** — 합격 기준 전환 성문화 + 승격 계약 30건(main confirmed v1) + 신규 의무 8건 절 + 비승격 전수 절. 재심 조건 ②
  - **ADR-0006 Amendment** — manifest 4공백(§5-4 deviation 표·multi-task mapping·no-result terminal·canonical path `docs/runs/<run-id>/closeout.yaml`)
  - **run id UUIDv7 단일화** — plan §3-3·§5-2 supersession + ADR-0005 deviation note, 구 문법 산출물 실사 0건
  - **doc-sync 6항목** — ruling cell 정착·E-09 정합·anchor 수리·audit 시점 분리·§5-2 back-reference·gap 명시
  - **settlement marker** — `docs/reviews/legacy-settlements/` 3건(digest 3축 결속·fail-closed·완료 합성 불가 증명), 추가 동종 3건은 감사만 → `decision/legacy-settlement-additional-cohort`
  - **migration sunset** — pre-0.9 자동 이관·재개·repair 전량 삭제(-1661줄) → read-only typed 거부(`unsupported_pre_0_9_layout`)
- **경량 재심 (read-only codex)**: 원 판정표 10행 = **해소 9 + 전환 소멸 1 + 미해소 0**. 단 병렬 문서 교차 모순 **WS-RX-1 blocker 적발** — ADR-0014 새 exit(PC 전량 green)와 계획 M1-A 범위(기계 분할)가 동시 성립 불가(main 인수 실수: 마일스톤 귀속 미확인). → **ADR-0014 Amendment**로 해소: 단계별 gate 귀속(승격 계약은 소유 서브시스템 재구축 마일스톤에), M1-A exit = 분할 완료+invariant 0+현행 suite green(동작 무변경 자기 신호 — comparator 부활 아님). RX-2/3/4(확정·초안 이중 선언, lease_epoch 표기, stale anchor)도 정합. 상세: `docs/meta/agent-reports-2026-07-20/w2-reexam.md`
- **Gates**: 기별 표적/전체 게이트 rc=0, sunset 머지 후 및 RX 수리 후 full gate **817 tests rc=0** (828→817: settlement +9, sunset -25, 기타 wave-1 누적 반영). 병렬 hot-file(계획서 4분할) 충돌 0.
- **SSOT**: unchanged (views 재생성만).
- **Decisions pending**: decision/legacy-settlement-additional-cohort(minor — 동종 3건 marker 포함 여부). **M1-A 착수 승인 = 사용자 최종 게이트**(main 판정: M0 exit 충족, 아래 Next).
- **Review**: requested (docs/reviews/2026-07-20-ruling-execution-request.md).
- **Next**: M1-A 착수 승인 시 — 계획 M1-A 분해(기계적 구조 분할, ADR-0014 Amendment의 exit 3항 적용) + feat/review-runs-uuid-owner-directory의 M1-B acceptance 편입.

## 2026-07-20-fleet-fix-wave

- **Goal**: 대기 결함·정리 task 전량 병렬 착수 — fleet w0720(codex 9기: 구현 7·read-only 분석 1·M0 exit 적대 리뷰 1) + finding당 opus verifier 11기. main은 관제탑(브리핑·회수·머지·게이트·인수)만.
- **Shipped** (implementer=external-runner/codex:gpt-5.6-sol ultra, 커밋-고정 worktree 9기 @ 662f2e3, 순차 squash 머지):
  - `fix/hook-matrix-color-env` — uv cache dir probe의 색상 env 중화(근치, 파서 관대화 아님). dev 22ec0db
  - `fix/shallow-ancestry-honesty` — shallow에서 merge-base rc=1을 unverifiable로 정직 강등 + dead-PID verify-fetch ref sweep(+기존 vacuous glob 단언 수리). dev 2ddd8f8
  - `fix/probe-machine-axis-hostname-drift` + `fix/marker-diagnostics-polish` — hostname을 신원 축에서 진단으로 강등(E-09), proof schema v3 승격(v2는 1회 정직 재프로브 — silent 재해석 금지), marker 안내 3건. dev 340514e
  - `fix/registry-worktree-misroute-guard` — linked worktree mutation을 lock/migration 이전 fail-closed 거부(`noncanonical_intent_mutation`; git-dir↔common-dir 불일치 판정 + GIT_* 중화). 실사고 2건 경로 폐쇄. dev 0c4ac61
  - `fix/reclose-diff-base-drift` — 재close가 generation 1 결속 base_sha 재사용, generation 1 부재는 fail-closed. dev caf1b34
  - `fix/delegate-env-prep-uv-cache` — worktree-local uv cache pre-warm(.waystone.yml 선언 + run_tests.py.lock + ruff pin); UV_OFFLINE=1 실재현 검증(임시 HTTP index→source 완전 제거). 부모 env 재사용은 mutable ambient state라 기각. dev ca20c62
  - `chore/legacy-name-residue` — live 표면 구명 잔재 0(JW_REPORT→WAYSTONE_REPORT, 신규 finding ID WS-GPT, old-name migration 호환 폐기), 이력 245건 분류 보존. dev f8d8732
- **M0 exit 적대 리뷰** (codex read-only): 원판정 blocker 3/major 6/minor 2 → **finding당 opus verifier 1기 반증 후 blocker 2/major 4/minor 5 확정**. blocker 강등 1(CDX-2: porting-ledger가 곧 닫힌 특성화 manifest — 리뷰어가 디렉터리형 fixture를 기대), major 강등 3. 생존 전량 task 등록(blocker 2·major 3·doc-sync 번들 1). 판정표: `docs/meta/agent-reports-2026-07-20/m0-exit-adjudication.md`. **main 권고: M0 exit 보류, blocker 2건(threat model 완결·등급 gate 실행가능화 — 둘 다 문서/계약 작업) 폐쇄 후 재심.**
- **Gates**: 머지별 표적 게이트 rc=0 ×7 + rename 머지 트리 full gate **833 tests rc=0** (evidence: docs/meta/agent-reports-2026-07-20/ 각 보고서의 VERIFIED — 전 기 pre-registered acceptance·RED-first 준수, threshold 완화 0). 병렬 9기 hot-file 충돌 **0건**(클러스터 인접 규약 + anchor 구획 분할).
- **SSOT**: unchanged (views 재생성만).
- **Decisions pending**: decision/m0-exit-verdict · decision/pre-header-feedback-settlement-method(분석: 원인은 pre-canonical envelope, 현 HEAD 동종 6건, 권고 = digest 결속 archived-unverifiable marker) · decision/delegate-readme-disposition · (기존) chore/migration-sunset 이관 확인.
- **Review**: requested (docs/reviews/2026-07-20-fleet-fix-wave-request.md).
- **route 기록**: main·orchestrator=main-session/fable-5 (route-note). implementer/M0리뷰=raw codex exec(fleet-dispatch 레시피 — delegate 하네스의 env_prep 결함이 이번 wave 수리 대상이라 우회, 규약대로 raw 레시피+finding 처리). **finding 검증은 opus clean-subagent fan-out — profile의 verifier 바인딩(external-runner/codex, 위임 결과 검증용)과 다른 표면**(리뷰 finding 반증)이므로 route-note 미기록, 여기 명시.
- **부수 발견**: jw-* lowercase 스키마 어댑터 잔재(→ chore/legacy-schema-marker-residue) · baseline 대비 dev 테스트 drift 828→833(M1-A 게이트 운용 규칙 필요, CDX-3 task에서 판단) · zsh에서 `PIPESTATUS` 빈값(rc 캡처는 파이프 없이 직접).
- **Next**: 사용자 ruling 4건 → blocker 2건 폐쇄 → M0 exit 재심 → M1-A.

## 2026-07-20-m0-characterization

- **Goal**: M0-C 완주 — runtime-state 처분 감사 실물 확정, porting ledger 개시(828건), traceability matrix 골격. 리뷰어가 걸었던 `characterization-baseline` 게이팅이 M0-B 계약 5건 폐쇄로 해소되어 착수 가능해진 상태였다.
- **Shipped** (implementer=external-runner/codex:gpt-5.6-sol xhigh, 2 lane 병렬 @ 19938d9):
  - **`docs/runtime-state-audit.md`** — §5-1 원칙("권위는 git에 있거나 재파생 가능")을 실물에 적용해 **위반 6건** 적출. 판정 기준의 정확성이 좋았다: '재파생 가능'을 "비슷하게 다시 쓸 수 있다"가 아니라 **"named authority channel에서 결정적으로 복구"**로 적용해 projection·cache·OS lock을 finding에서 제외했고, 어제 착지한 ADR-0011의 ProjectContext로 canonical root를 확정한 뒤 감사했다.
  - **`docs/porting-ledger.md`** — 828건 전수 분류(port 818 / rewrite 10 / **drop 0**) + M1-A 출력 등급(machine JSON 406 / diagnostic 264 / canonical artifact 134 / human CLI 14 / time·path 10) + 불변조건 매핑. baseline 원본 SHA-256으로 대상 고정.
  - **`docs/traceability-matrix.md`** — 불변조건 × 테스트 4층 골격. §3-9 취소·quiescence 안전 계약은 E 번호가 없으므로 독립 행.
- **핵심 결과 — 내 우려가 틀렸다**: M0-C 내내 경계한 것은 "828 green인데 결함이 있었으니 결함을 정상으로 고정한 테스트가 있을 것"이었다. **결함 보존 테스트 0건.** JW-GPT-014·015에 대해 기존 테스트는 오히려 **반대 계약을 단언**하고 있었고(표본 검증: `test_latest_v1_supersession_requires_new_cycle_v2_refreeze`가 `assertFalse(ok)`+merge gate 차단), 결함은 **테스트가 닿지 않는 진입 경로**에 존재했다. 되돌릴 화석이 없으므로 M1-B는 **빈 fixture 2개를 채우는 작업**이며 ledger가 그 빈칸을 명시했다.
- **부수 성과 — 요청하지 않은 축의 적출 2건**: 어제 확정한 계약과 충돌하는 기존 테스트를 `needs-ruling`으로 올렸고 둘 다 main이 판정했다.
  - `test_claim_only_crash_remnant_is_discardable` → **ruling**: 'exposure.json 부재'는 effect 부재의 증거가 아니라 기록 부재일 뿐이며 effect 개시 후 기록 실패와 구분되지 않는다. fencing epoch 미진행 + action id 각인 산출물(worktree·ref·process·artifact) 부재를 **관측**했을 때만 폐기, 관측 채널 없으면 unknown-effect 보존 (ADR-0003 §3-9·ADR-0002·E-08 정합).
  - `test_fixed_stdout_shim_replacement_reprobes_via_executable_stat_identity` → **ruling**: size/mtime 단독 판정 금지, content digest 결속. round-5 JW-GPT-013(디렉터리 stat→내용 digest)과 **동일한 실수의 반복**이며 개정 E-09가 금지하는 형태다.
- **감사 finding 처분** (6건→3건 통합): F-01 profile.yml이 project 라우팅 의도와 machine 능력을 혼재 → **M3 이월**(동작 변경이라 feature freeze 대상, 계획서가 이미 profile v2를 M3에 배치) · F-06 machine-tier state 처분 부재 → **계획서 §5-1에 machine-tier 층 신설**(projects.json은 machine-local 권위, doctor typed 보고, 재등록은 명시적 owner 행위) + **원칙을 2조건에서 3조건으로 개정**(git 존재 / 재파생 가능 / **machine-local 명시+한계 문서화**) · F-02~F-05 → **수용된 잔여 ruling**(사용자 결정).
- **수용된 잔여의 조건**: delegation 101건·exposure 12건·consent 4건은 machine-local이며 유실 시 복구 불가임을 명시하되, 동반 규율 3개를 함께 확정 — ⑴ 완료 판정·인수 근거를 이 기록에만 의존해 기술하지 않는다 ⑵ 머신 교체 시 함께 사라짐을 전제로 운영 ⑶ 타 머신 위임 기록은 그 머신에만 남는다(정상 동작, ruling #6과 정합). 백업 기계를 만들지 않은 이유는 그것이 두 번째 권위 표면이 되기 때문이며 ADR-0006이 manifest에 verdict를 이식하지 않기로 한 것과 같은 논리다.
- **Gates**: 828 green + ruff clean — lane 1회 + 병합 후 1회 주관측. **코드 변경 0건**(문서만).
- **운영 정리**: delegation record 8건이 `needs-review`로 남아 worktree를 붙들고 있던 것을 인수 커밋과 함께 사후 정리했다 — 오늘 8회 전부 `delegate apply`가 아니라 수동 `git apply`+squash merge로 처리해 엔진 레코드가 병합 사실을 몰랐다. **리뷰 패킷 R-2("엔진 밖 수동 조작")의 실사례**이며, 다음부터 apply 경로 사용 또는 즉시 정리를 규율로 삼는다.
- **SSOT**: unchanged.
- **Next**: **M0 exit review → M1-A 승인**. M0-A/B/C 산출물 전량 착지 완료. M1-A는 기계적 구조 분할(동작·저장 형식 변경 0)이며 ledger의 출력 등급표가 그 exit 기준이다.

## 2026-07-19-m0-contract-gaps

- **Goal**: 설계공백 리뷰(chatgpt:gpt-5.6-pro, CHANGES REQUESTED · major 5, JW-GPT-016~020)가 지적한 **M0-B 계약의 빈칸**을 폐쇄. 리뷰어가 `gate/characterization-baseline` exit를 이 5건 반영 전에는 통과시키지 말라고 게이팅했으므로 M0-C 착수 전 필수.
- **리뷰 성격**: 코드 리뷰가 아니라 설계 공백 리뷰였다 — "이번 세션의 실패·실수 중 0.12가 설계대로 전부 구현돼도 남는 것은 무엇인가"(R-1~R-8 + 메타 질문). 내 자기 진단 8건 중 **7건이 '남음'으로 확인**됐고 R-2만 부분 해결로 갈렸다. **receipt 결속 성공**(model/target/request-digest 전부 일치 — 하네스 생성 요청서를 손대지 않고 사용, 직전 라운드의 미결속 사고와 대조).
- **Shipped** (implementer=external-runner/codex:gpt-5.6-sol xhigh, 3 lane 병렬 → 순차 병합 @ d092fe0):
  - **ADR-0010 run-spec readiness** (016 = R-1·R-4·R-5를 하나로 묶음) — 구조화 acceptance criterion(claim/source/scope/evidence kind/negative case) + 결정론 검사 + **독립 contract critic**(unachievable·unbounded·unverified-reference·scope-ambiguous·implementation-prescriptive를 typed concern으로만 반환, **자동 재작성 금지**) + **retry/수렴 hard ceiling**(counter 초기화로 회피 불가) + risk-gated reviewer requirement.
  - **ADR-0011 project context** (017) — ProjectContext(project_id·canonical_root·active_worktree_root·git_common_dir·checkout_identity). runtime DB는 canonical project 결속, linked worktree는 checkout context, intent 변경 명령은 noncanonical worktree에서 기본 거부.
  - **ADR-0012 verification capability preflight** (019) — frozen VerificationPlan, required check의 `authoritative_executor=engine`, capability preflight 불가 시 typed refusal. **독립성 논거**: 독립은 "다른 디렉터리에서 실행했다"가 아니라 **구현 actor가 자기 결과를 최종 승인하지 못하는 데서** 온다 → engine 실행이 E-07을 약화하지 않고 강화. `worker_execution_required`로 worker의 RED·self-check는 유지.
  - **ADR-0009 review artifact addressing** (018) — 신규 run은 `docs/reviews/runs/<run-uuid>/...` UUID owner directory, **filename 분해 owner 추론 금지**, 구 flat 파일은 legacy adapter 판독(bulk migration 없음, strangler).
  - **E-09 개정** (020) — durable identity(intrinsic identifier)와 scoped ambient observation(boot id·pid·monotonic time을 **scope·lifetime 선언 하에 locator/freshness로만**) 구분. hostname·cwd·파일시스템 메타데이터를 identity에서 배제. git path는 검증된 intrinsic identity의 **주소**로는 허용하되 이름 분해로 owner 추론 불가.
- **Gates**: 828 green + ruff clean — 통합 lane 1회 + 병합 후 1회, 전부 main 주관측. **코드 변경 0건**(ADR·불변조건 문서만).
- **병합 전 교차 검증 2건**: ⑴ `docs/invariants.md`는 **E-09 행 하나만 변경**(다른 불변조건 문구 손상 0, 기계 대조) ⑵ **ADR-0003 ↔ 개정 E-09 충돌 없음** — ADR-0003이 `host_boot_identity`를 "재부팅 전후 PID·monotonic 영역 구분"이라는 scope로, `process_start_token`을 "같은 boot 내 PID 재사용 구분"이라는 lifetime으로 이미 명시해 개정 E-09의 허용 조건을 충족.
- **main이 추가로 발견한 것 (리뷰어 미지적)**: 초판 E-09가 *"판정 근거는 파일 내용·**파일명**·git-tracked 사실에서만"*으로 파일명을 정당한 근거로 명시해, JW-GPT-018의 "파일명 분해 owner 추론 금지" 요구와 **정면 충돌**. 018·020을 **같은 lane에 묶어 동시 개정**해 해소했다 — 따로 고쳤으면 두 계약이 서로를 위반했을 것.
- **위임 메커니즘 결함 발견**: `dev_docs/`는 gitignore라 **위임 worktree에 존재하지 않는다.** 즉 ADR-0002~0008은 계획서를 한 번도 보지 못한 채 acceptance 조항만으로 작성됐다(결과는 main 대조로 확인했으나 메커니즘은 깨져 있었음). 내 조항의 *"계획서 §3-4를 권위 원천으로"*는 **작업자가 검증할 수 없는 참조** — JW-GPT-016이 명명한 `unverified-reference`의 실사례를 finding 등록 직후 자체 발견. 이번 lane부터 권위 원천을 git-tracked 문서로 전면 교체(리뷰 feedback 파일 + ADR + invariants).
- **각 ADR에 실측 근거 결속**: 016↔round-6 조항 결함 3건·발산·green 게이트가 결함 미탐지, 017↔registry 오배선 2회, 019↔위임 8회 전부 게이트 실행 실패, 020↔probe hostname·cwd·파일명 분해. 추상 원칙이 아니라 실제 실패에 묶여 있다.
- **SSOT**: unchanged.
- **Next**: **M0-C 착수 가능**(리뷰어 게이팅 해소). characterization + porting ledger(등급 배정) + runtime-state 처분 감사 → M0 exit review → M1-A 승인. 미착수 하네스 결함 3건은 각각 019·017·020 계약의 0.11 대응물.

## 2026-07-19-m0-contracts

- **Goal**: 0.12 M0-A 마감(baseline 동결)과 M0-B 산출물 전량 착지 — ruling 6건 확정 + ADR 7종 + 불변조건 확정본.
- **Shipped**:
  - **M0-A 완료** — `gate/trust-baseline-tag`: **`baseline/0.12-refactor` @ 7cfecd3**(annotated). exit 3조건 충족: ⑴ 미해결 trust major 2건이 하네스 사용 표면 밖임을 **실측**(review.mode=packet · freeze sidecar 0개 · `ingest_round_binding`이 packet 분기에서 freeze glob 이전 조기 반환 · 014 경로는 `--pr` 필수) ⑵ `docs/known-issues.md`에 영향 범위·게이트 무영향 근거 문서화 ⑶ feature freeze를 tag 메시지에 선언. 게이트 828 green + ruff clean 후 부착.
  - **ruling 6건 전부 확정** — ①배포브랜치(main=전체소스/dist=배포, M4 전환) ②**위협모델=우발적 손상만**(의도적 로컬 변조·adversarial filename·다중사용자 경계는 명시적 비보호; crafted filename 전제 finding은 REAL이어도 수용 잔여로 분류) ③round→run(내부 canonical, /round는 1.0까지 alias) ④SQLite 채택+DB는 project-local 기본·미지원 FS는 typed refusal ⑤**delivery 기본 commit**(사용자 결정 — 권고 manual에서 상향, automation level은 설정 조정·init이 묻지 않음) ⑥타 머신 resume 미지원 명시.
  - **ADR-0002~0008** (@ cf1073c, 2e06f71) — effect commit protocol(효과 7종 표+관측채널 불가 열+recovery 결정표) · run observability & cancellation(3분리·derived health·**취소는 의도만 기록, running/alive/unknown-effect는 삭제 금지**·supervisor identity) · executor 경계 · fact authority matrix · closeout manifest(add-only CAS·자기참조 금지·**deep audit는 로컬 artifact store 요구**=사용자 결정 b) · SQLite 운영 · terminology.
  - **`docs/invariants.md`** — I-01~12 + E-01~09 확정본(집·검증층 4열). E-불변조건 문구는 계획서에서 축자 이관, **기계 대조로 9건 전부 존재·표류 0 확인**.
- **Gates**: 828 green + ruff clean — lane별 1회 + 병합 후 1회, 전부 main 주관측(rc 직접 캡처). **코드 변경 0건**(문서·레지스트리만).
- **계획 r4→r5**: 외부 리뷰 3차(설계 승인, 필수 2+정밀화 8) 반영 — 수용 9/조정 1/기각 0. 신설: **§3-9 취소·정지·정리 안전 계약**(리뷰 최대 기여 — record.lock이 *우연히* 제공하던 "실행 중 cleanup 차단"이 lock 분해와 함께 사라진다는 지적), E-08 양방향 정밀화, `stalled`를 FSM state→derived health, liveness를 job 단위 계산 후 집계, progress 분모를 frozen closure로, status/watch 엄격 read-only, §3-10 process supervision·identity. **조정 1건**: M0-A 순서 — 리뷰는 `014·015 폐쇄→0.11.2→tag`를 권고했으나 리뷰어가 알 수 없던 두 사실(round-6 발산 결과, 두 결함이 PR-mode 한정이고 dogfooding은 packet mode)로 **0.11.1을 baseline으로 tag**하고 해소는 M1-B 수용 기준에 편입.
- **신규 finding**: `fix/probe-machine-axis-hostname-drift`(minor) — probe fingerprint의 `machine` 축이 hostname(`Mac.local`)이라 네트워크/DHCP 변경 시 증명 무효화·매번 재프로브. 같은 marker에 안정적 `host_identity`(IOPlatformUUID)가 이미 존재. E-09 계열(ambient 값을 신원 축으로 사용)이나 **E-09 문구는 파일시스템 메타데이터에 한정돼 이 사례를 못 잡는다**(리뷰 패킷 R-8).
- **조정자 실수 1건**: `git merge --squash`가 커밋 없는 lane 브랜치에 대해 **오류 없이 "no changes"로 성공** — `ls`로 확인하지 않았다면 산출물 0개인 채 "병합 완료"로 보고했을 것. 조용한 실패의 전형이며 리뷰 패킷 R-2의 실사례.
- **SSOT**: unchanged.
- **Review**: `docs/reviews/2026-07-19-residual-after-0.12-request.md` — **설계 공백 리뷰**(코드 리뷰 아님). 이번 세션의 실패·실수 중 0.12가 설계대로 전부 구현되어도 남는 것 8건(R-1~R-8)과 메타 질문(계획이 엔진은 촘촘히 설계했으나 조정자는 거의 설계하지 않았다 — 이것이 불변 지향점 ②와 양립하는가). 리뷰어 = chatgpt:gpt-5.6-pro(사용자 지정).
- **Next**: M0-C characterization + porting ledger(등급 배정) + runtime-state 처분 감사 → M0 exit review → M1-A 승인. 리뷰 회신은 M0-C 착수 전 반영이 바람직(무엇을 "보존할 동작"으로 고정하느냐가 R-5·R-6과 직결).

## 2026-07-19-supersession-attribution-attempts

- **Goal**: 5차 리뷰의 REAL major 2건(JW-GPT-014 merge 관측-기록 불일치 / JW-GPT-015 foreign malformed sidecar가 healthy round ingest 차단) 해소.
- **결과: 미착지 — 두 lane 모두 4회전 후 중단, 병합 0건. dev는 03ba429 그대로.** 라운드 산출물은 수리가 아니라 (a) ruling 1건 확정, (b) 하네스 결함 2건 발굴, (c) 0.12 구조 교체 필요성의 실측 증거다.
- **회전 궤적** (전부 implementer=external-runner/codex:gpt-5.6-sol xhigh, 매 attempt main이 게이트 재실측 + RED 독립 재현 + codex 적대 리뷰):
  - **014**: a1 major2(cycle_conflict 조기반환·비권위 로컬정책으로 영구 증거 기록) → a2 major2(approve 우회·순차 fan-out 부분상태) → a3 major3(freeze 우회·과잉 demotion·보상삭제 비원자) → **a4 major4** — a4는 구조적으로 옳은 방향(단일 chokepoint `classify_remote_review` + 원자적 단일 관측 레코드)이었으나 신규 결함 3건 유입. **결정적**: `marker_valid`는 v2의 base_sha/reviewers를 선택 필드로 허용(review.py:2007-2010)하는데 `_validated_demotion_target`은 필수 요구(1350-1352) → 정상 축약형 v2가 supersede되면 **모든 online 명령이 차단되는 위양성**. 병합 시 원 결함보다 악화. 변경 규모 149→908줄, 폐쇄율 < 유입율.
  - **015**: a1 major2 → a2 major3(phantom owner 세탁·fan-out 오염·**단조성 위반**: 새 라운드의 평범한 artifact 추가만으로 기존 corruption이 사라짐) → a3 **main 직접 기각**(단조성을 `st_mtime_ns` 순서에 결속 — git clone/checkout이 mtime을 재작성하므로 다중 머신에서 보장 소멸; round-5 JW-GPT-013 교훈 재발) → a4 major3. a4 진전: `unattributable` 1급 상태 도입, 공용 resolver로 improve 99줄 순감, E-09 준수 + mtime 불변 테스트. 미해결: unattributable 행을 최단 prefix bucket에 배정해 무관 round 오염·실제 owner의 stale 승격, 이중 glob 매칭 파일이 두 round 귀속, 외부 review-requests 상충 generation에서 ingest/improve 판정 분기.
- **구조 진단**: 014 = 관측 지점을 열거해도 다음 게 나옴(status→merge→approve→freeze→base-policy 분기) — 필요한 건 분류 경로가 하나뿐인 구조. 015 = round id가 `-freeze-`를 포함 가능한 한 파일명 귀속은 원리적으로 다의적. 둘 다 **fact당 authority가 하나여야 한다**는 0.12 M1 명제의 실증 사례.
- **dev 잔존 위험(정직한 기술)**: merge gate는 여전히 skew를 차단하므로 **게이트 우회 경로 없음**. 실제 피해는 offline 증거 투영(`improve` 리포트·offline ingest)이 online 판정과 어긋나 무효화된 리뷰 세대를 explicit으로 표시할 수 있는 것 — 오해 유발, 게이트 무영향. 두 task는 status=blocked, notes에 시도 이력·진단 기록.
- **Gates**: 각 attempt lane에서 전체 스위트 green 재실측(014 a4: 840 OK / 015 a4: 832 OK, 양쪽 ruff clean) — 게이트는 통과했으나 적대 리뷰가 잡은 결함은 테스트 없는 동작이었다. 병합하지 않았으므로 dev 스위트는 828 불변.
- **Ruling 확정**: decision/trust-threat-model-boundary — **우발적 손상만 방어**(크래시·부분 쓰기·도구 버그·정상 운영 중 이름 충돌). 의도적 로컬 파일 조작·adversarial filename·다중 사용자 권한 경계는 **명시적 비보호**. 근거: 로컬 쓰기 권한자는 scripts/*.py 직접 수정이라는 더 쉬운 경로가 있음. 적용: crafted filename 전제 finding은 REAL이어도 수용된 잔여로 분류하고 재론하지 않음. M4 SECURITY.md에 사용자용 명시.
- **신규 하네스 결함(리뷰 아닌 dogfooding 산출)**: fix/delegate-env-prep-uv-cache(major — 위임 러너 worktree 캐시에 pyyaml·ruff 부재로 러너가 자기 게이트·RED를 못 돌림; 이번 8회전 전부에서 재현) · fix/registry-worktree-misroute-guard(major — 잔류 cwd로 registry 변경이 linked worktree에 기록되고 pre-0.9 migration 유발, 2회 발생).
- **0.12 plan r3→r4**: 사용자 지시로 **실행 관측 설계 1급 편입**(§3-8 liveness/progress/current 3분리 + honest-unknown, **E-08** 침묵은 종료의 증거가 아님, `run watch`, budget 3행) — 근거는 이 라운드의 실사고(네트워크 전환 시 main session이 러너 생사를 판정 못해 살아있는 delegation을 discard 시도, record lock이 저지). 추가로 **E-09**(신뢰·귀속 판정을 파일시스템 메타데이터에 결속 금지) 신설 — 같은 실수 2회 재발(round-5 디렉터리 stat, round-6 mtime)로 개인 규율에서 불변조건으로 승격.
- **조정자(main) 자기 결함 3건**: acceptance 조항이 기각 사유를 직접 유발 — ① "improve와 동일 규칙으로 일원화"(그 규칙의 안전성 미검증) ② "explicit 되살아나는 경로 없음"(디렉터리 전체 unwritable에서 원리적 불가) ③ "**모든** 식별 가능한 v2 contract를 demote"(cycle 범위 누락 → 과잉 demotion 결함 유발). 조항을 구현 지시에서 **성질(property) 기술**로 바꾼 회전에서 양 lane 모두 구조적 해법으로 전환 — 다음 라운드부터 성질 기술을 기본으로.
- **5차 리뷰 receipt**: **UNBOUND(pending)** — raw 리뷰 프롬프트가 회신 헤더를 손으로 작성하며 full SHA 오기 + request-digest 누락. 하네스가 E-01대로 거부(설계대로 동작). finding은 리뷰어 attestation이 아닌 main의 독립 코드 검증으로 채택. 레시피 교정은 메모리 기록.
- **SSOT**: unchanged.
- **Next**: 0.12 M0로 이동 — M0-A 잔여(baseline tag·feature freeze) → M0-B(ruling 6건 중 ②확정, 나머지 5건 + ADR 세트) → M0-C. 014/015는 M1에서 구조적으로 재검토. 0.11.2는 내지 않으며 0.11.1이 현행 릴리스.

## 2026-07-19-evidence-authority-fixes

- **Goal**: 4차 리뷰(codex 대체, REAL major 3: JW-GPT-011~013) 전량 해소 — 0.11.1 hotfix 라인. 부수 목표: 0.11.0 probe 자기-churn 실사고(delegate run 전면 불가) 해소.
- **운영 특기**: 0.11.0 하네스의 probe 결함(013과 동일 근원)으로 `delegate run` 발사가 불가 → 전 lane을 **raw codex exec 우회**(사용자 규칙: 하네스 버그는 raw 우회 + finding 기록). 검증은 lane별 게이트 주관측 재실측 + RED 독립 재현 + codex 적대 리뷰(xhigh)로 delegation 파이프라인과 동등 규율 유지.
- **Shipped** (전부 raw codex gpt-5.6-sol xhigh; 순차 병합 013→011→012):
  - fix/probe-config-content-binding (013) — fingerprint를 config.toml 내용 digest에 결속(디렉터리 stat은 진단 강등), absent 상태 동등·읽기 실패 fail-toward-probe·digest-only 저장. probe 자기-churn 실사고 해소 포함 (attempt-1 인수 @ 1cbff2a; 적대 리뷰 ACCEPT-WITH-NOTES major 0)
  - fix/pr-cycle-v1-supersession-honesty (011) — same-cycle later-v1이 v2 digest 권위를 강등(3면 동일 규칙·tie/미파싱 fail-closed), completion·merge gate가 skew 사유 소비, mixed-host demotion sidecar 영속화, freeze `--round` 필수화(신형 CLI v1 발행 금지·capability 탐지 제거), post-cutoff v1은 digest-era 차단 (attempt-3 인수 @ ae2e8e3; 적대 회전 REJECT2→REJECT1→ACCEPT-WITH-NOTES0)
  - fix/freeze-sidecar-corruption-isolation (012) — corrupt freeze sidecar를 sentinel 보존(filename↔content identity 대조), 최신 cycle 손상 시 round honest-unknown(이전 cycle explicit 승격 금지), cross-round prefix 충돌 foreign-skip(improve/ingest 판정 일치) (attempt-2 인수 @ fc70ce1; REJECT1→기계 델타 main 직접 검증 — 비례성 규칙 기록)
- **Gates**: live 807→828 green(매 attempt·매 병합 주관측 재실측, rc 직접 캡처) + ruff clean. RED 전 attempt 독립 재현.
- **수용 residual(비차단)**: digest-era-v1-freeze 사유의 demotion 미영속(감사 parity, gate 무영향) · demotion 쓰기 동시 writer TOCTOU(단일 사용자 모델) · crafted demotion fail-closed DoS · unreadable rename 위조(filename=identity 계약) — 뒤 둘은 decision/trust-threat-model-boundary 범위.
- **부수 사고 기록**: ① registry 조작 1회가 잔류 cwd로 worktree에서 실행돼 worktree tasks.yaml 오염+pre-0.9 profile seed — 즉시 원복, "registry 조작은 main repo에서만" 성문화. ② 0.10.0 dedup 버그의 중복 binding sidecar 재발행 2회 수동 제거(0.11.1 후 재발 여부가 dogfooding 검증 포인트).
- **SSOT**: unchanged.
- **Next**: 5차 재검토 요청 게시 → 0.11.1 릴리스(`release-to-main.sh`) → `/plugin update` → delegate run 복구 확인 → 구식 pending 표기(historic 5건+evidence-authority binding-unavailable) 0.11.1 판독으로 재확인. 0.12 refactor plan r2는 사용자 리뷰 대기.

## 2026-07-19-evidence-authority

- **Goal**: generation-binding 3차 리뷰(CHANGES REQUESTED — major 4: JW-GPT-007~010)를 전량 해소 — "완료" 판정을 지탱하는 증거의 권위를 가변 로컬 상태에서 불변·재파생 가능한 원천으로 이전.
- **Shipped** (전부 implementer=external-runner/codex:gpt-5.6-sol xhigh; 매 attempt 실측, 설계 회전 적대 리뷰(xhigh)·재량 0 델타는 main 직접 검증 — 근거는 verdict artifacts):
  - fix/receipt-read-time-rederive (007) — receipt 권위를 reply-metadata cache → verbatim body 읽기 시점 재파생으로 이전(cache는 진단 전용, legacy 경로 포함 cache 단독 신뢰 0). 손상 격리(한 receipt가 pending/improve/overlay를 중단 불가), 사유 taxonomy 구분(envelope/불일치/sidecar 부재), bounded 리더 복원(CRLF 정확 산술), overlay 패리티 (attempt-3 인수 @ 4af545d; 적대 2회전이 major 6·minor 7 적출)
  - fix/pr-cycle-generation-binding (008) — cycle marker·freeze sidecar를 digest 필수 v2 schema로 승격, 투영은 cycle 증거가 명명한 generation만 조회(latest 채택 금지), marker 단독 복구, v1/v2 혼재는 위양성 conflict가 아닌 정직한 skew, cycle 번호는 신뢰 operator만 계상 (attempt-2 인수 @ a2ff310)
  - fix/round-mint-anchor-immutable (009) — round 기존성 앵커에서 mutable PROGRESS heading 제거(검증된 immutable exposure 단독) — 문서 한 줄 편집으로 과거-dated round를 mint해 v1 legacy 창을 여는 경로 폐쇄, 익일 확장 무회귀 (attempt-1 인수 @ b3354c8)
  - fix/probe-proof-principal-binding (010) — fingerprint에 실행 principal(euid/gid/groups)·codex config root·Linux best-effort process context 축 추가(공유 checkout의 타 사용자 적중 차단), not-observed는 상태 동등 대조(macOS probe-once 유지, 비대칭 전이만 재프로브) (attempt-2 인수 @ 69036c1)
- **리뷰 finding 처분**: 007~010 전량 폐쇄. 컨테이너/namespace 심층 축·로컬 단일-파일 변조의 강한 변형(append-only/원격 store)은 decision/trust-threat-model-boundary로 위양(사용자 ruling 대기, 비차단).
- **Gates**: live 777→804 green(lane별 병합 후 재실측; P-lane은 codex 부재 이중 게이트) + ruff clean.
- **SSOT**: unchanged.
- **Decisions pending**: decision/trust-threat-model-boundary(위협모델 경계) · chore/pre-header-feedback-settlement(역사 정착).
- **Review**: requested (docs/reviews/2026-07-19-evidence-authority-request.md) — 4차 검토.
- **4차 회신 (2026-07-19, codex:gpt-5.6-sol 대체 리뷰 — 사용자 지시)**: 007·009 resolved, 008→JW-GPT-011 재개, 010→JW-GPT-013 still-broken, 신규 JW-GPT-012 — 3건 전부 main 검증으로 REAL major 확증(근거: feedback triage 표). task 등록은 사용자 지시로 보류 후 0.11.1 수정 라인으로 확정.
- **Release 0.11.0 (2026-07-19)**: 사용자 승인으로 **known-issues 명시 릴리스** — JW-GPT-011(혼합버전 PR cycle digest 오귀속)·012(freeze sidecar 손상 시 이전 cycle explicit 승격)·013(config 내용 변경에 probe proof 재사용)은 0.10 대비 퇴행이 아닌 미완 하드닝이며(각 표면은 0.11이 엄격히 강함), 수정은 0.11.1(round-5)로. 이 머신 dogfooding 개시(하네스 0.10.0→0.11.0).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 릴리스 0.11 → /plugin update → verifier binding 복원 → round-5(011~013 수정) → carrier 라이브 검증 → migration-sunset. 0.12 refactor plan은 사용자 추가 리뷰 대기(동결). minor 큐 별도.

## 2026-07-18-generation-binding

- **Goal**: carrier-lanes-fixes 재리뷰(gpt-5.6-pro/xhigh, CHANGES REQUESTED — major 4)를 전량 해소: request generation 정체성 결속과 프로브 증명의 runtime 결속.
- **Shipped** (전부 implementer=external-runner/codex:gpt-5.6-sol xhigh; 매 attempt 호스트 스위트+ruff 실측, 설계 회전엔 raw codex 적대 리뷰(xhigh)·재량 0 기계 델타엔 main 직접 검증 — 근거는 각 delegation record verdict artifact):
  - fix/binding-schema-v2-digests — binding-2 schema: narrative+rendered request digest 필수(부재=corrupt), digest-capability를 round 날짜 cutoff(> 2026-07-18)로 sidecar 밖 앵커(다운그레이드 위장 차단, same-day 정품 v1 보호), 비실재 날짜 corrupt(packet·PR 패리티), 게이트·freeze의 stored rendered digest 대조+byte-동일 게시, round close 신규 mint 현재일 검증(기존 round 익일 확장 허용) (attempt-5 인수 @ a8f208a; attempt-4가 verdict 인수 후 병렬 병합 base drift로 hash-동일 재적용 — 같은 파일 lane 순차 병합 교훈 기록)
  - fix/probe-proof-runtime-fingerprint — 프로브 증명 마커를 versioned JSON fingerprint 계약으로: host identity(machine-id 정규형/IOPlatformUUID)·resolved codex 경로+stat·stdout version(stderr 기록 전용)·sandbox 관측 정직화·mount 정체성, exact-match만 생략+축별 안내, 전 실행 표면 resolved 경로 통일, codex 부재 CI-동등 스위트 green (attempt-4 인수 @ 84c1293)
  - fix/reprepare-generation-atomicity — reprepare를 binding 선발행 fail-closed 순서로(어느 지점 중단도 구 완료 은닉 불가), pending이 request·narrative의 최신 binding 재현성 확인 (attempt-1 인수 @ 79f241e)
  - fix/reply-narrative-echo — request가 자기 rendered digest 노출(sentinel 순환 해소), 회신 request-digest 에코가 도장 근거(echo-era 라운드는 에코 필수, 폴백은 legacy 한정), receipt는 명명된 generation 일관+읽기 시점 재대조, stale/미상/no-echo 구분 (attempt-3 인수 @ cc5ae84) — JW-GPT-004 메커니즘 A 폐쇄
- **리뷰 finding 처분**: JW-GPT-004(A+B)·005·006·Q1 전량 폐쇄. 반박·이관 판정은 각 verdict artifact에 기록 — 유한 잔여 2건(전환일 다운그레이드 창·정적 stderr-only shim 아래 child 교체)은 코드 주석 문서화 + chore/pre-header-feedback-settlement 확장 예정.
- **Gates**: live 전체 스위트 748→777 green(각 lane 병합 후 재실측, F-lane은 codex 부재 PATH 이중 게이트) + ruff F401/F841 clean. 0.10.0 ingest가 남긴 중복 v1 sidecar 1건 제거(계약 동일, canonical 보존).
- **SSOT**: unchanged.
- **Decisions pending**: chore/pre-header-feedback-settlement(역사 라운드 정착 방식 — 전환일 잔여 흡수 확장 포함).
- **Review**: requested (docs/reviews/2026-07-18-generation-binding-request.md) — 3차 검토.
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 3차 리뷰 통과 시 릴리스 0.11 → /plugin update → hooks 마커 → verifier binding 복원 → carrier 라이브 검증 → migration-sunset. minor 큐: marker-diagnostics-polish · shallow-ancestry-honesty · hook-matrix-color-env · legacy-name-residue(사용자 지시).

## 2026-07-18-carrier-lanes-fixes

- **Goal**: carrier-lanes 리뷰(gpt-5.6-pro/xhigh, CHANGES REQUESTED)의 잔여 major 3건을 병렬 lane으로 해소하고 재검토 요청 — 신뢰 표면(프로브 증명·narrative 결속·publication 증명) 경화.
- **Shipped** (lane 3건은 implementer=external-runner/codex:gpt-5.6-sol xhigh, 각 attempt마다 host 스위트+ruff 실측 + raw codex 적대 리뷰(xhigh) + main verdict — 전 회전 근거는 delegation record의 verdict artifact):
  - fix/probe-proof-machine-scope — codex 프로브 증명을 커밋 추적 `.waystone.yml`에서 미커밋 per-checkout 마커(`.waystone/codex-runner-verified`)로 이동 (attempt-3 인수 @ 874c4c3, 위임 20260717T200543Z). 3회전 경화: 복원적·원자적 self-ignore(migration 경로 통일, symlink 불수용) · 마커는 untracked+계약값일 때만 증명(추적된 마커는 무시+untrack 안내+재프로브) · legacy 키 무시+제거 안내(source-aware) · barrier 게이팅 동시성 계약(lock 하 재확인 자체를 고정)
  - fix/binding-narrative-digest — binding contract에 canonical narrative digest 결속 (attempt-3 인수 @ 874c4c3, 위임 20260717T200543Z). narrative만 바뀐 재-prepare가 새 sidecar 발행+pending 재개(RED) · freeze/packet 게이트 모두 exposure+stored narrative 재렌더 대조(request.md 변조 거부 RED) · legacy digestless는 양측-digestless일 때만 legacy-pre-digest 라벨 폴백(digest-strip 위장 차단 RED), publication 게이트는 digest 하드 요구 · improve 투영에 digest·전용 narrative_coverage_reason 보존
  - fix/remote-verify-live-ref — publication 증명을 stale remote-tracking ref 신뢰에서 라이브 원격 증명으로 재작성 (attempt-2 인수 @ 4673b58, 위임 20260717T205101Z). 정확한 upstream branch를 명시 refspec으로 pid+uuid 전용 임시 ref에 fetch해 원자 고정(공유 FETCH_HEAD 미사용, cleanup 실패도 fail-closed) · persisted refs/remotes/* 신뢰를 remote verify·head_pushed 양쪽에서 제거(RED: 삭제된 upstream·refspec 제외 — 실 bare-remote, clone 분리) · 부재 vs 네트워크 구분(ls-remote --exit-code) · remote '.' 거부 · freeze binding↔exposure 교차검증을 packet 게이트와 동등화. criterion 9 잔여(shallow 경계 rc=1 오진단, fail-closed 방향)는 override-unmet 기록 + fix/shallow-ancestry-honesty 등록
  - docs/readme-delegate-fold — README delegate 섹션을 리드+`<details>` fold(전체 lifecycle·명령 지도·run 7단계·verify/verdict/apply/discard 계약)로 재작성 @ 07bab60 (main-session; dev_docs/delegate_readme.md 스타일 참조, 최신 표면 재검증)
- **신규 등록(이번 라운드 발견, 미착수)**: fix/reply-narrative-echo (major — ingest가 현재 binding digest를 도장하는 재-prepare race, reply 헤더 계약 변경 필요·사용자 ruling 대기) · fix/marker-diagnostics-polish · fix/shallow-ancestry-honesty · fix/hook-matrix-color-env (FORCE_COLOR env에서 스위트 1건 오염 — main tree 재현으로 lane 회귀 아님 판별) · chore/legacy-name-residue (JW/jahns-workflow 구명 잔재, 사용자 지시) · chore/pre-header-feedback-settlement (0.10 헤더 이전 역사 feedback 3라운드 영구 pending — 기존 부채)
- **Gates**: 병합 전체 스위트 730→748 green (2회: 874c4c3에서 740, 4673b58에서 748; FORCE_COLOR 중화 실측) + ruff F401/F841 clean. 각 lane worktree 개별 실측 green.
- **SSOT**: unchanged.
- **Decisions pending**: fix/reply-narrative-echo (reply 계약 변경 ruling) · chore/pre-header-feedback-settlement (역사 라운드 정착 방식).
- **Review**: requested (docs/reviews/2026-07-18-carrier-lanes-fixes-request.md) — carrier-lanes 재검토.
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 재리뷰 통과 시 릴리스 0.11 → /plugin update → hooks 마커 → verifier binding 복원(TEMP UNBOUND 해제) → carrier 라이브 fan-out 검증. 이후 chore/migration-sunset(전 머신 이관 확인 후)·minor 큐(diagnostics polish·color env·name residue).

## 2026-07-18-carrier-lanes

- **Goal**: fix-wave 리뷰 큐를 3-lane 병렬 체인(R: review.py / D: delegate.py / A: release)으로 전량 해소하고, deterministic-workflow carrier(CC Workflow 통합)를 편입.
- **Shipped** (lane 항목은 전부 implementer=external-runner/codex:gpt-5.6-sol, 각 건 raw codex 적대 리뷰 + main-session agent_checks로 인수):
  - **R lane**: fix/publication-gate-direct-binding — ancestry 추론 전삭제, 단일 명제(원격 ref가 closeout SHA + byte-identical sidecar 보유) 직접 증명 · fix/reply-header-parser-simplification — 헤더 블록 한정 단일 decode 규칙 · feat/deterministic-review-packet — script 렌더링 + freeze 재렌더(3차) · feat/review-pending-ledger — pending 파생 전용(저장 없음) · feat/waystone-statusline — 파생 1줄, consent 설치
  - **D lane**: fix/drop-codex-companion — companion transport 전면 삭제, verify=codex exec 단일화 (ruling 2026-07-17) · fix/execution-surface-dep-gating — done 의존 게이트 · fix/preflight-probe-isolation + fix/probe-once-config-gate — 프로브 격리·1회 실행 · fix/verifier-transport-hardening — timeout/signal/빈 출력 정직 보고 · chore/hook-matrix-normal-mode-coverage — 양 모드 mutation-kill
  - **A lane**: fix/release-checked-out-main · chore/release-script-hardening — env-allowlist smoke, TMPDIR guard, fail-loud manifest
  - **Carrier**: decision/deterministic-workflow-carrier-semantics — ADR-0001 사용자 비준(2026-07-18) · feat/delegate-fanout-cli-contract + feat/deterministic-workflow-carrier-contract + feat/delegate-fanout-workflow-template — 14b0cff로 구현·merge, 비준으로 의존 충족 후 인수(e9e5c14)
  - **기타**: chore/overengineering-prune-batch1 (감사 batch 1) · fix/cli-uninitialized-root-gate (Fable subagent, lock chokepoint) · fix/review-feedback-triage-discipline (마커 섹션 + 읽기 시점 재계산) · decision/lanes-verify-round-scope · docs/readme-staleness-sweep — README 표면·배지(543→719)·ADR-0001 SSOT 포인터 정정 + docs gate 3표면 확장 (main-session, Workflow carrier 분석)
  - **Dropped 7건**: publication-gate-bypasses·reply-header-residuals·codex-exec-verifier-hardening(각 direct-binding·parser-simplification·transport-hardening으로 재설계 대체), companion 계열 2건(제거로 obsolete), verifier-guard-residuals-2, task-cli-arg-validation(strict options로 흡수)
- **Gates**: 전체 테스트 600→719 green (112s, 2026-07-18 2회 실측) + ruff F401/F841 clean. docs gate에 신규 CLI 표면 3종 편입.
- **SSOT**: unchanged; ADR-0001 ratified (2026-07-18).
- **Decisions pending**: none.
- **Review**: requested (docs/reviews/2026-07-18-carrier-lanes-request.md).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 릴리스 0.11 사용자 지시 대기 — 릴리스 후 /plugin update → hooks 마커 설치 → verifier binding 복원 → 라이브 검증. chore/migration-sunset은 전 머신 0.11+ 이관 확인 후 착수.

## 2026-07-16-fix-wave

- **Goal**: 첫 라운드 리뷰 지적 2건 + 현장 dogfooding 보고 3건(사용자·bw2·spark1) + 회신 프로토콜 재설계를 최대 병렬 위임으로 해소.
- **Shipped** (전부 implementer=external-runner/codex:gpt-5.6-sol, 각 건 raw codex 적대 리뷰 + main-session agent_checks로 인수):
  - fix/release-staging-isolation — 릴리스를 temp-index 투영 + positive SHIP manifest로 재작성 (ignored 로컬 파일 보존, 실패 원상; 리뷰어 지적)
  - fix/verifier-hook-hermeticity — verifier 세션에서 전 hook hermetic (ruling: 전 hook no-op)
  - fix/round-packet-remote-visibility — packet publication 게이트(push된 HEAD에 request+binding 실존), 엄격 Reviewing 파서 (2차, waiver 1건)
  - feat/effort-pro-ultra → fix/effort-drop-pro — ultra 추가 후 pro는 실측(자기선언 응답)으로 제거
  - feat/task-status-parked — 6번째 상태 parked (newton 요구)
  - fix/runner-env-failure-detection — 빈-성공 오분류 fail-loud + 프리플라이트 프로브 (spark1 AppArmor 사고)
  - feat/review-reply-structured-header — 회신 머리 key:value 블록(model/effort/review-target), robust 파싱, 회신-내장 결속 증거 (2차 + prefix 12+ 상향 84ad6a7)
  - decision/verifier-hook-isolation-contract (ruling: hermetic), decision/release-ship-manifest (ruling: positive manifest)
- **Gates**: 전체 테스트 558→600, 모든 apply 후 dev 게이트 green + ruff clean. 위임 2회 기각 후 재계약 재시도 2건(packet, header), 사용자 escalation 2건 기록.
- **SSOT**: unchanged.
- **Decisions pending**: none.
- **Review**: requested (docs/reviews/2026-07-16-fix-wave-request.md).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 대기 큐 10건(major 5: publication 우회, companion effort, 의존 게이팅, 프로브 격리, 헤더 잔여 + feat 3: 결정론 packet, pending ledger, statusline + chore 2). 릴리스(0.11)는 사용자 지시 대기 — 릴리스 스크립트는 이제 로컬 파일 안전.

## 2026-07-16-adopt-dogfooding

- **Goal**: waystone이 자기 자신의 개발 하네스가 되는 첫 사이클 — 채택 bootstrap + 첫 dogfooding finding 2건의 위임 수정.
- **Shipped**:
  - docs/adopt-waystone-harness — 자기채택 bootstrap: SSOT.md(ideate 합성), init(packet 리뷰·warn-allowed·delegation on), ADR-0000, 부산물 release EXCLUDES, 3축 profile 6-role binding (done; main-session)
  - fix/boundary-hook-cli-resolution — boundary hook을 plugin hooks.json으로 이동(마커 게이트·비차단·레거시 감지) (done; implementer=external-runner/codex:gpt-5.6-sol, 1차 discard 후 2차 apply)
  - fix/verify-worktree-self-contamination — verifier 세션에서 waystone session hook이 worktree에 상태를 시딩하던 결함을 WAYSTONE_VERIFIER_SESSION guard로 차단 (done; implementer=external-runner/codex:gpt-5.6-sol, 네트워크 실패 1회 discard 후 apply)
- **Gates**: 전체 테스트 558→562 OK + ruff F401,F841 clean (인수 verdict의 agent_checks; 커밋 bb8484c·9076ec8). main 누출 0 — release EXCLUDES tree-hash 시뮬레이션으로 증명 (4f8ddbc).
- **SSOT**: 신규 작성 (§1-§7) + ADR-0000 ratified.
- **Decisions pending**: none.
- **Review**: requested (docs/reviews/2026-07-16-adopt-dogfooding-request.md).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개 — bootstrap 단계).
- **Next**: 릴리스로 고친 hook·guard를 설치본에 반영 → verifier binding 복원 + boundary hook enable로 라이브 검증. chore/verifier-session-guard-hardening 처리. 이후 Adapt & Enforce arc.

## waystone 채택 이전 이력 (요약, 2026-06-11 ~ 2026-07-16)

waystone 채택(ADR-0000) 이전의 개발은 round 구조 없이 진행됐다. 상세는 `dev` 브랜치 git log와 `dev_docs/` 설계·구현 노트(gitignored) 참조.

- **v0.1–v0.2 (2026-06):** 플러그인 생성(당시 이름 jahns-workflow). SHA-bound 리뷰 사이클, 결정론적 merge gate 등 correctness kernel을 7차례 외부 GPT 리뷰로 경화.
- **v0.3–v0.6 (2026-06 하순~07초):** 리뷰 번들 도입 후 단일 markdown 요청으로 단순화(v0.4.1), task registry CLI + archive(v0.5), ideate 스킬 + SSOT.md 표준화(v0.6). dev/main 분리, `release-to-main.sh`, marketplace CI 자동 sync 확립.
- **v0.7 Observe & Advise (2026-07-13):** 세션 로그 trace + audit 렌즈 + evidence 기반 improve 권고.
- **v0.8 Delegate & Verify (2026-07-14):** 격리 worktree delegation primitive, artifact contract, overlay/replay/warn, 독립 verify. v0.8.1에서 waystone으로 개명, v0.8.3 Codex 호스트 지원.
- **v0.9 Unify & Automate (2026-07-15):** cross-host 저장 통합(`{root}/.waystone` + `~/.waystone`), flock locking, delegate 자율화(verdict 게이트).
- **v0.10 Bind & Compose (2026-07-16):** 설계 완전성 arc — 213요소 전수 감사 후 role 3축 소비 완성, policy 4층 합성, guard 4규칙, longitudinal metrics, verdict digest 체인. 558 tests. 최종 인수 합격 후 릴리스.
