<!-- waystone feedback: the body below is the reviewer reply VERBATIM (byte-exact copy via `waystone review ingest`) — do not edit it; a triage skeleton is appended beneath it. -->
round: 2026-07-24-013-authority-closure
reviewer: chatgpt:gpt-5.6-pro
reviewer-effort: high
review-target: b31d01b437a0ec44eae0f03aa00fbe96dd334317
reply-metadata-json: {"metadata":{"effort":"high","model":"chatgpt:gpt-5.6-pro","request-digest":"sha256:5e21870b8569720e7fec449a5d8b6308cdc1f633f0d71586cecb8c60cd17690a","review-target":"b31d01b437a0ec44eae0f03aa00fbe96dd334317"},"narrative_digest":"sha256:64ccf22edd660b6d5812e4c9499808b4c7ee8f657b58158120c4f32b6f56f3fc","rendered_request_coverage_reason":null,"rendered_request_digest":"sha256:5e21870b8569720e7fec449a5d8b6308cdc1f633f0d71586cecb8c60cd17690a","rendered_request_digest_matches":true}
ingested: 2026-07-26
source: /tmp/review.md
verbatim-bytes: 23837

---

```text
model: chatgpt:gpt-5.6-pro
effort: high
review-target: b31d01b437a0ec44eae0f03aa00fbe96dd334317
request-digest: sha256:5e21870b8569720e7fec449a5d8b6308cdc1f633f0d71586cecb8c60cd17690a
```

# Review verdict — CHANGES REQUESTED

**Critical 0 / Major 3**

이번 closure는 이전 세 finding을 실질적으로 진전시켰습니다.

* evaluate의 detached runner는 candidate materialization에서 실행되고, allowlist로 만든 child environment를 명시적으로 전달합니다.
* promotion은 public branch가 아니라 lineage-owned private integration ref만 갱신합니다.
* 정상적인 `waystone review` CLI는 canonical `ProjectContext`를 먼저 확정하고 linked-worktree mutation을 거부합니다.
* private-ref expected-old CAS와 public worktree·index 불변 회귀도 적절합니다.

특히 WS-GPT-027의 원래 실패였던 “checked-out branch ref만 이동해 worktree/index와 불일치시키는 경로”는 폐쇄됐습니다. 새 target은 `refs/waystone/integration/<lineage-id>`이고, 최초 생성은 zero-OID CAS를 사용하며, promotion은 해당 private ref에만 적용됩니다.  Public checkout의 branch OID, tracked/untracked bytes, index, status가 유지되는 회귀도 이전 blind spot을 정확히 보완합니다.

그러나 다음 세 authority gap이 남아 있습니다.

1. promotion verifier가 실제로 실행한 environment와 executable은 **start-time preflight authority 및 terminal VerifierEvidence에 결속되지 않습니다.**
2. child environment에서는 `GIT_*`를 제거했지만, harness 자체의 Git adapter는 여전히 ambient `GIT_*`를 상속하여 **canonical repository authority를 다른 repository/worktree로 재지정할 수 있습니다.**
3. successful promotion은 private ref를 이동하고 `closeout-ready`에 도달하지만, **promotion에는 worker result가 없는데 closeout은 worker result를 무조건 요구하므로 run을 완료하거나 outcome ledger에 기록할 수 없습니다.**

따라서 이전 세 finding 중 private-ref delivery 분리는 폐쇄됐지만, environment provenance와 canonical authority는 아직 완전히 닫히지 않았으며, 새 private-ref 의미론을 durable closeout까지 연결하는 경로도 미완성입니다.

---

# Claims adjudication

| Claim                                                         | 판정                                                                      |
| ------------------------------------------------------------- | ----------------------------------------------------------------------- |
| 1. Child environment가 invocation authority의 일부다               | **부분 폐쇄** — evaluate에는 성립하지만 promote의 preflight·terminal evidence에는 미결속 |
| 2. Promotion은 public checkout 상태를 바꾸지 않는다                     | **정상 repository authority에서는 승인**                                       |
| 3. Private integration ref CAS가 concurrency를 fail-closed 처리한다 | **CAS 자체는 승인**, 단 repository 선택이 ambient `GIT_*`에 영향받음                  |
| 4. Review가 linked-worktree HEAD를 current authority로 쓰지 못한다    | **정상 CLI topology에서는 승인**, ambient Git redirect로 우회 가능                  |
| 5. PYTHONPATH 제거 후 deterministic supervisor bootstrap이 유지된다   | **승인**                                                                  |
| 6. 교정 E2E가 구 public-branch promotion을 잡는다                     | **승인**, 단 successful promote closeout은 검증하지 않음                          |

---

# Confirmed findings

## WS-GPT-029 — promotion verifier의 실행 environment와 executable이 preflight 및 terminal evidence에 결속되지 않는다

**Severity: major**

## 실패 메커니즘

새 `RunnerEnvironment`는 allowlist된 환경 값을 정렬하고 digest하며, `RunnerInvocation`도 이 environment를 보유합니다. 이 부분 자체는 적절합니다.   `StagedRunEngine._invocation_digest()`에도 environment digest가 들어가고, direct promotion verifier가 명시적인 `env=`로 실행되는 것도 확인됩니다.

문제는 **어느 시점의 environment와 executable이 검증 권위가 되는가**입니다.

Promotion `run start`에서는 `_prepare_promotion_verification()`이 현재 `PATH`의 `codex`를 찾고 그 binary bytes를 probe evidence에 넣습니다. 그러나 runner config는 `NOT_OBSERVED`이며, environment digest는 이 preflight context에 포함되지 않습니다.

실제 promotion verifier는 이때 실행되지 않습니다. `_ensure_stage_started()`는 explore와 evaluate만 시작하며 promote는 후속 `resume`에서 `_execute_promotion_verifier()`로 실행됩니다.  따라서 다음과 같은 정상적인 세션 경계가 가능합니다.

```text
run start:
  PATH/HOME/config = E1
  codex binary      = B1
  preflight PASS

세션 종료 또는 shell 변경

run resume:
  PATH/HOME/config = E2
  codex binary      = B2
  실제 verifier 실행
```

`resume` 시 `_stage_invocation()`은 다시 현재 `shutil.which("codex")`를 사용하고, `RunnerInvocation`의 default factory로 현재 environment를 새로 만듭니다.   즉 E2/B2가 실행되지만, preflight evidence는 E1/B1에 대한 것입니다.

더 중요한 점은 promotion verifier의 terminal authority가 사용하는 invocation digest입니다. `execute_verifier()`의 durable runner lineage는 별도의 `_verification_invocation_digest()`를 사용하며, 그 payload에는 다음이 없습니다.

* environment digest
* resolved executable
* executable content digest
* promotion verifier launch artifact digest

포함되는 것은 actor, candidate result, preflight digest, verifier binding·capability·sandbox 등입니다.

`VerifierEvidence`에도 environment 또는 launch descriptor field가 없습니다.  별도의 `waystone-promotion-verifier-launch-2` artifact에는 environment digest가 기록되지만, 이 artifact는 `reload_verifier_evidence()`나 `reload_integration_decision()`의 authority chain에서 요구되거나 재검증되지 않습니다.

결과적으로 다음 상태가 성립합니다.

```text
preflighted capability: B1 under E1
actual semantic judgment: B2 under E2
VerifierEvidence:
  preflight_evidence_digest = E1/B1 계보
  result/criteria           = E2/B2가 생성
  environment descriptor    = 없음
```

환경 기록은 존재하지만, **승격을 승인하는 terminal artifact의 구성요소는 아닙니다.**

## Executable TOCTOU

실행 파일도 path만 결속됩니다.

`_resolved_executable()`은 frozen `PATH`에서 executable을 찾고 regular executable file인지 검사하지만, content digest를 확인하지 않습니다.  Invocation digest 역시 argv 문자열과 environment digest만 포함합니다.

따라서 preflight 이후 또는 invocation 생성 이후 다음이 가능합니다.

```text
/path/to/codex -> codex-v1
preflight / invocation digest 생성

/path/to/codex -> codex-v2
실제 spawn
```

동일 argv와 environment digest 아래에서 다른 bytes가 실행됩니다. 별도 최소 재현에서도 symlink target 교체 후 동일 command path가 다른 executable을 실행하는 것을 확인했습니다.

이것은 local attacker를 전제하지 않아도 발생할 수 있습니다.

* package upgrade
* tool shim 갱신
* mutable uv/tool cache
* PATH의 symlink 교체
* 시작 세션과 재개 세션의 virtual environment 차이

## 필수 수정

Environment와 executable을 합친 **하나의 frozen execution descriptor**가 필요합니다.

예:

```text
RunnerExecutionDescriptor
  environment_values
  environment_digest
  resolved_executable
  executable_content_digest
  backend/model binding
  config/materialized-toolchain digests
  descriptor_digest
```

권장 규칙:

1. Promotion start/preflight 시 descriptor를 생성해 CAS와 VerificationPlan 또는 RunSpec에 결속
2. Resume은 ambient environment를 다시 샘플링하지 않고 frozen descriptor를 로드
3. Environment를 다시 선택해야 한다면 동일 run의 조용한 continuation이 아니라 새 preflight revision 또는 새 attempt로 기록
4. Spawn 직전에 executable path와 bytes를 다시 관측해 descriptor와 비교
5. Promotion verifier launch artifact를 `VerifierEvidence`가 직접 참조
6. `reload_verifier_evidence()`가 launch descriptor, environment digest, executable digest를 재검증
7. IntegrationDecision도 해당 VerifierEvidence 계보를 그대로 소비

필수 회귀:

```text
A. run start 후 PATH/HOME 변경 → resume
   expected: 기존 preflight로 실행 금지

B. preflight 후 codex binary 또는 symlink target 교체
   expected: model invocation 전 refusal

C. promotion verifier launch artifact 누락 또는 env digest 불일치
   expected: VerifierEvidence reload/apply refusal
```

---

## WS-GPT-030 — engine-owned Git operations가 ambient `GIT_*`에 의해 다른 repository authority로 재지정될 수 있다

**Severity: major**

## 실패 메커니즘

이번 수정은 **child runner environment**에서 `GIT_DIR`, `GIT_WORK_TREE`, `UV_WORKING_DIRECTORY` 등을 제거합니다. 해당 회귀는 제대로 작성됐습니다.

그러나 Waystone engine 자체의 Git adapter는 다른 계약을 사용합니다.

`git_read_bytes()`는 다음처럼 현재 process environment 전체를 상속합니다.

```python
{
    **os.environ,
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
}
```

`git_rc()`는 아예 `env`를 전달하지 않아 ambient environment를 그대로 상속합니다.

반면 `resolve_project_context()`만 자신의 Git topology probe에서 `GIT_*`를 제거합니다.

따라서 public command의 흐름은 다음처럼 분리됩니다.

```text
1. ProjectContext resolution
   GIT_* 제거
   → canonical project A를 정확히 식별

2. 이후 Git authority observation/effect
   GIT_* 상속
   → repository B 또는 stale linked checkout으로 재지정 가능
```

## 영향을 받는 경로

### Private integration ref

`_integration_target()`은 private ref의 존재, 현재 OID, 최초 생성 CAS를 모두 `git_rc()`와 `git_full_sha()`로 수행합니다.

실제 promotion apply도 `GitRefEffect`를 통해 결국 adapter의 `git update-ref`를 실행합니다.

따라서 다음 환경에서:

```bash
GIT_DIR=/other/repository/.git
GIT_WORK_TREE=/other/repository
waystone run ...
```

ProjectContext는 canonical project A를 선택하지만, private ref의 생성·관측·apply는 B에서 일어날 수 있습니다.

별도 최소 Git 재현에서 다음을 확인했습니다.

```text
git -C A rev-parse HEAD
  + GIT_DIR=B/.git
  + GIT_WORK_TREE=B
→ B의 HEAD 반환

git -C A update-ref refs/waystone/integration/probe ...
  + 동일 환경
→ A에는 ref 없음
→ B에 ref 생성
```

Waystone의 postcondition도 같은 redirected adapter를 사용하므로 B에서 desired OID를 읽고 성공으로 판정할 수 있습니다.

### Candidate 및 verifier authority

Candidate ref 관측, detached worktree 생성, candidate fingerprint, GitResultTriple 도출도 같은 adapter를 사용합니다. `_candidate_stage_root()`은 `git_full_sha()`와 `git worktree add`에 의존합니다.  Verifier의 result triple 역시 `git_read_bytes()`로 base/result/tree/patch를 읽습니다.

### Review current-objective authority

Review CLI가 canonical `ProjectContext`를 고른 뒤에도 `validate_disposition_authority()`는 `git_full_sha(root)`를 사용하여 current HEAD를 정합니다.

Ambient `GIT_DIR`가 같은 Git family의 stale linked-worktree gitdir을 가리키면:

```text
ProjectContext: canonical checkout B
current_head:   stale linked branch A
```

가 되어, 이번에 폐쇄하려던 superseded objective가 다시 current authority로 인정될 수 있습니다.

### Outcome ledger

Outcome ledger의 private commit/ref 작업도 `env=None` 또는 `{**os.environ, **overrides}`를 사용하므로 ambient `GIT_*`가 제거되지 않습니다.

즉 run state DB는 canonical project A에 기록하면서, outcome ledger ref는 B에 게시하는 split authority도 가능합니다.

## 영향

* candidate와 integration ref가 다른 repository에서 관측·수정됨
* review disposition이 stale linked HEAD를 current objective로 사용
* canonical store와 Git authority가 서로 다른 project를 가리킴
* private-ref CAS가 잘못된 repository에서는 성공
* postcondition도 같은 redirect를 따라가므로 mismatch를 감지하지 못함
* outcome ledger가 canonical runtime store와 분리됨

이는 private-ref CAS 자체의 오류가 아니라 **CAS 대상 repository의 identity가 engine authority에 결속되지 않은 문제**입니다.

## 필수 수정

모든 engine-owned Git invocation이 하나의 중앙 환경 계약을 사용해야 합니다.

```text
build_git_environment()
  strip all ambient GIT_*
  set only operation-owned values
  GIT_OPTIONAL_LOCKS=0 where appropriate
  GIT_PAGER=cat
  LC_ALL=C
```

적용 대상:

* `waystone.adapters.git.git_rc`
* `git_read_bytes`
* `runs.effects._git_rc`
* `runs.outcome._git`
* project/review/brief authority resolver가 호출하는 모든 Git subprocess
* temporary index를 사용하는 경우 `GIT_INDEX_FILE`만 명시적으로 추가

Repository identity도 가능하면 다음에 결속하는 편이 좋습니다.

```text
canonical_root
git_common_dir
expected repository identity
```

필수 회귀:

```text
canonical project A
unrelated project B
ambient GIT_DIR/GIT_WORK_TREE → B

waystone run/review/close 실행

expected:
  모든 Git fact/effect가 A에서만 발생
  B bytes/refs 불변
```

추가로 같은 repository의 stale linked-worktree gitdir을 ambient `GIT_DIR`로 지정한 current-objective 회귀도 필요합니다.

---

## WS-GPT-031 — successful promotion은 worker result가 없어 durable closeout을 수행할 수 없다

**Severity: major**

## 실패 메커니즘

Explore와 evaluate는 external stage runner가 `CompletedWorkerResult`를 발행합니다. 그러나 promote는 다음 action만 실행합니다.

```text
evaluated-candidate-freeze
independent-verify
optional adversarial-review
integration-decision
target-ref-apply
completion
```

Promotion stage에는 worker-result producer가 없습니다.

Verifier evidence와 integration decision을 발행하고 private ref를 적용한 뒤 `_record_stage_completion()`은 attempt와 job을 `completed`, run을 `closeout-ready`로 전이합니다. 이 과정에서도 `worker-result:<attempt>` artifact는 생성되지 않습니다.

그러나 closeout 경로는 모든 lifecycle stage에서 worker result를 무조건 요구합니다.

### Outcome scaffold

`scaffold_outcome_delta()`는 `_final_result_reference()`를 호출하고 다음 reference만 찾습니다.

```text
worker-result:<final-attempt-id>
```

없으면 `completed final attempt has no worker result`로 거부합니다.

### Outcome publication

수동으로 완성된 OutcomeDelta를 제공해 scaffold를 우회해도 `_validate_outcome_lineage()`가 동일하게 final worker result를 요구합니다.

```text
worker-result:<attempt>
OutcomeDelta.result_digest == worker-result artifact digest
parse_worker_result_bytes(...)
CompletedWorkerResult required
```

### Verifier result와 digest 의미도 충돌

Promotion verifier evidence를 outcome evidence로 인용하면 `_validate_verifier_evidence()`는 verifier가 승인한 GitResultTriple의 `result_digest`가 OutcomeDelta의 `result_digest`와 같아야 한다고 요구합니다.

따라서 임의로 promotion worker-result를 추가하더라도 다음 두 digest를 동시에 만족해야 합니다.

```text
OutcomeDelta.result_digest
  = worker-result artifact digest
  = approved GitResultTriple digest
```

일반적으로 서로 다른 canonical artifact이므로 성립하지 않습니다.

## 실제 결과

현재 successful promote는 다음 상태까지 갈 수 있습니다.

```text
private integration ref = candidate OID
attempt                = completed
job                    = completed
run                    = closeout-ready
```

하지만 이후:

```text
waystone run close --outcome-draft ...
→ completed final attempt has no worker result

waystone run close --outcome ...
→ final attempt has no frozen worker result
```

이므로:

* run을 `completed`로 만들 수 없음
* `refs/waystone/outcomes`에 closeout/outcome pair를 게시할 수 없음
* 세션·머신 간 durable completion evidence가 없음
* status에서 영구적으로 `closeout-ready`에 머무름
* private integration ref의 의미가 outcome ledger에 기록되지 않음

현재 E2E도 successful promotion 후 attempt state와 run `closeout-ready`까지만 확인하고, `run close` 및 ledger reload까지 수행하지 않습니다.

## 필수 수정

Closeout result authority를 lifecycle stage별로 분리해야 합니다.

권장 구조:

### Explore

```text
primary result authority:
  CompletedWorkerResult
  Candidate descriptor
```

### Evaluate

```text
primary result authority:
  EvaluationEvidence
  frozen candidate/spec generation
```

### Promote

```text
primary result authority:
  approved GitResultTriple digest
  VerifierEvidence
  optional ReviewerEvidence
  IntegrationDecision
  private integration-ref apply observation
```

Promotion closeout에서는 worker result를 요구하지 않아야 합니다.

`RunCloseout` 또는 completion evidence에는 최소 다음이 포함되어야 합니다.

```text
candidate digest/OID
evaluation evidence digest
verifier evidence reference/digest
reviewer evidence digest if required
integration decision reference/digest
private integration ref
expected-old OID
applied OID
GitRefEffect observed digest
```

OutcomeDelta의 `result_digest`는 worker-result artifact가 아니라 **stage가 정의한 semantic result identity**여야 합니다. Promotion에서는 승인된 GitResultTriple digest를 사용하는 것이 가장 일관적입니다.

필수 E2E:

```text
explore
→ evaluate
→ promote
→ private ref apply
→ run close
→ outcome ledger publication
→ fresh process에서 ledger reload

assert:
  run == completed
  private integration ref == candidate OID
  public branch/worktree/index/status unchanged
  closeout verifier/decision/apply tuple exact
```

---

# 폐쇄된 이전 경계

## Public branch/worktree mutation

WS-GPT-027의 원래 failure mechanism은 폐쇄됐습니다.

* lineage별 private ref
* zero-OID creation CAS
* expected-old apply CAS
* checked-out target refusal
* checkout/reset 부재
* dirty public worktree byte 보존

이 방향은 유지해야 합니다.

## Normal review CLI의 linked-worktree mutation

CLI는 모든 finding subcommand에서 `_review_context()`를 사용하고 noncanonical checkout을 거부합니다. `materialize()`도 raw path가 아니라 `ProjectContext`를 요구하고 canonical checkout 여부를 재확인합니다.

즉 ambient Git redirect가 없는 정상 shell에서는 WS-GPT-028의 public entrypoint가 폐쇄됐습니다.

## Child environment allowlist

`GIT_*`, `PYTHONPATH`, `PYTHONHOME`, `UV_WORKING_DIRECTORY` 등이 child process로 전파되지 않는 구현과 회귀는 적절합니다. Detached supervisor도 frozen environment digest를 재구축해 비교하고, 실제 child에 명시적으로 전달합니다.

문제는 이 environment가 **preflight 및 terminal promotion evidence에 결속되지 않은 것**이지, allowlist builder 자체가 잘못된 것은 아닙니다.

---

# Open domain questions

## 1. Promotion environment를 언제 freeze할 것인가

두 정책 중 하나를 명시적으로 선택해야 합니다.

### 정책 A — start-time freeze

`run start`에서 environment·executable descriptor를 freeze하고 모든 resume이 이를 재사용합니다.

### 정책 B — execution-time re-preflight

`resume`에서 environment가 달라지면 그 환경으로 새 preflight evidence와 새 attempt/revision을 발행한 뒤 실행합니다.

현재처럼:

```text
start에서 E1 preflight
resume에서 E2 실행
```

을 조용히 허용하는 안만 피하면 됩니다.

## 2. Promotion의 canonical `result_digest` 정의

다음 중 하나를 권위로 정해야 합니다.

* candidate descriptor digest
* candidate Git OID
* GitResultTriple digest
* private-ref apply observation digest
* 이들을 결속한 promotion-result artifact digest

권고는 **GitResultTriple + apply observation을 결속한 promotion-result artifact**입니다. Worker result를 promotion의 primary result로 만드는 것은 actor model과 맞지 않습니다.

## 3. Non-CLI review functions의 지원 범위

Public CLI는 닫혔지만 다음 함수는 여전히 raw `Path`를 받습니다.

* `ingest_feedback`
* `validate_file`
* `disposition_file`
* `attach_review`

`materialize`만 `ProjectContext`를 강제합니다.

이 함수들이 지원되는 programmatic API라면 동일하게 `ProjectContext` proof를 요구해야 합니다. Package-private helper라면 이름과 export surface를 private로 만들고 canonical front door 외 호출을 비지원으로 명시하는 편이 낫습니다.

## 4. HOME 아래 tool config의 trust boundary

요청서가 인정했듯 HOME 값은 digest되지만 `~/.codex`, global Git config 등 HOME 아래의 실제 bytes는 결속되지 않습니다.

Solo-local trust domain에서 이를 수용할 수는 있습니다. 다만 그 경우 보장 문구는 다음 수준이어야 합니다.

```text
환경 변수 provenance는 결속됨.
사용자 HOME 내부 tool configuration bytes는 local trust boundary임.
```

독립적인 replay까지 주장하려면 isolated HOME 또는 config content digest가 필요합니다.

---

# Residual risks and unavailable environment

* 지정 target SHA에 연결된 GitHub Actions workflow run은 확인되지 않았습니다. 
* Request에 기록된 284-test green, 각 worktree gate 및 real backend 실행은 제가 재실행하지 않았습니다.
* 이번 검토는 target diff와 production authority path를 정적으로 추적했습니다.
* 별도 최소 Git 재현으로 다음 두 동작은 직접 확인했습니다.

  * `GIT_DIR/GIT_WORK_TREE`가 `git -C <other-root>`의 repository authority를 재지정함
  * 동일 executable path의 symlink target 교체로 pre-observation 후 다른 binary가 실행됨
* 실제 Codex installation을 start와 resume 사이에 교체하거나, live promotion run에서 environment drift를 동적으로 재현하지는 않았습니다.
* GPU와 별도 dataset은 이번 trust-kernel 검토에 필요하지 않았습니다.
* Private integration ref의 GC 정책과 candidate materialization GC는 known operational residual로 보며 이번 major 판정에는 포함하지 않았습니다.
* Supervisor `runtime.json` publication race도 요청서가 별도 minor로 등록한 범위이므로 중복 계상하지 않았습니다.

# 최종 판정

이번 target으로 다음 부등식은 실질적으로 해소됐습니다.

```text
promotion ref
≠
checked-out public branch

normal review CLI authority
≠
linked-worktree-local authority

child process environment
≠
wholesale ambient environment
```

그러나 다음 부등식이 남아 있습니다.

```text
preflighted verifier environment/tool
≠
actually executed verifier environment/tool

canonical ProjectContext
≠
ambient-Git-selected repository

successful promotion
≠
durably closeable completed run
```

따라서 `b31d01b437a0ec44eae0f03aa00fbe96dd334317`은 **CHANGES REQUESTED**입니다.

**Critical 0 / Major 3**이며, release readiness는 위 세 finding이 폐쇄될 때까지 승인하지 않습니다.


---

<!-- waystone triage: BEGIN -->
## Findings (triage — 검증 완료 2026-07-26)

회신은 free-form(WS-GPT- prefix라 skeleton 미파싱) — verbatim 본문에서 직접 triage. 검증 방식:
finding당 적대 verifier 1기(codex gpt-5.6-sol, 029=xhigh·030/031=high, 반증 시도 지침) + main
독립 라인 추적 이중 확인. verifier 보고 원본: scratchpad reports v0726-ws029/030/031.md.

리뷰 정식 판정: "**CHANGES REQUESTED** — Critical 0 / Major 3". **직전 round의 major 3건
(WS-GPT-026·027·028)은 실질 폐쇄 판정** — env allowlist·private integration ref CAS·public
checkout 불변·review CLI front door·supervisor bootstrap·교정 E2E 전부 유지 확인으로 수용.
**Release readiness는 신규 major 3건 폐쇄까지 계속 보류.**

| finding | verdict | type | severity | evidence (검증 근거) | task |
|---|---|---|---|---|---|
| WS-GPT-029 — "promotion verifier가 실제로 실행한 environment와 executable은 start-time preflight authority 및 terminal VerifierEvidence에 결속되지 않습니다" | REAL | verification | major | verifier v0726-ws029 CONFIRMED + main 독립 확인. 반증 4경로 전부 부재: preflight `RunnerContext`에 env digest 없음(engine.py:545-554, preflight.py:789-821, runner-config `NOT_OBSERVED` engine.py:536-537); resume이 `shutil.which("codex")`+default factory로 E2/B2 재샘플링(engine.py:752-768·1543-1565, environment.py:59-69); `_verification_invocation_digest` payload·`VerifierEvidence` 필드·reload exact-key set에 env/executable/launch 결속 전무(verify.py:919-939·320-342·2062-2179); launch-2 artifact는 기록만 되고 production 소비처 없음(engine.py:1416-1455). 필수 회귀 A/B/C 전부 현행에서 refusal 없이 통과 가능. 트리거는 정상 start/resume 세션 경계(새 셸·venv·tool upgrade) — 적대 전제 불요 | fix/promotion-verifier-execution-binding |
| WS-GPT-030 — "harness 자체의 Git adapter는 여전히 ambient GIT_*를 상속하여 canonical repository authority를 다른 repository/worktree로 재지정할 수 있습니다" | REAL | correctness | major | verifier v0726-ws030 CONFIRMED(동적 재현 포함) + main 독립 확인. GIT_* strip은 context.py:82-95·155-166 지역 probe에만 존재, 반환 context에 미전파; `git_read_bytes`=`{**os.environ,...}`(adapters/git.py:31-50), `git_rc`·`git()` env 미전달(63-77·259-265), outcome `_git`=`{**os.environ,**overrides}`(outcome.py:393-403). tempdir 재현: `GIT_DIR`만으로 `git -C A`의 HEAD·update-ref가 B 대상(상대경로 포함), stale linked gitdir로 canonical HEAD가 stale commit으로 치환 — review current-objective 부활 전제 실증. postcondition도 동일 redirect라 미검출(effects.py:1689-1740). 경계: `GIT_WORK_TREE` 단독은 ref redirect 불충분. 트리거는 GIT_DIR export 셸/wrapper/direnv — 빈도 낮아도 cross-repo write라 major 유지 | fix/git-ambient-environment-authority |
| WS-GPT-031 — "promotion에는 worker result가 없는데 closeout은 worker result를 무조건 요구하므로 run을 완료하거나 outcome ledger에 기록할 수 없습니다" | REAL | correctness | major | verifier v0726-ws031 CONFIRMED + main 독립 확인. promote assurance DAG에 worker-result producer 없음(assurance.py:27-35; runner 시작·result 관찰 모두 explore/evaluate 한정 engine.py:864-866·1690-1699); `_record_stage_completion`이 closeout-ready 전이만(engine.py:2340-2363). scaffold는 `_final_result_reference` 무조건 거부(run_scaffold.py:397-409), 수동 OutcomeDelta도 `_validate_outcome_lineage` stage 분기 없이 동일 요구(outcome.py:615-643); verifier evidence 우회는 GitResultTriple digest≡worker-result artifact digest 이중 등식으로 불성립(outcome.py:602-612·625-643). e2e6는 closeout-ready까지만 검증(test_run_cli.py:927-972). 정상 성공 promote 전수 발생 — durable completion 기능 자체가 부재 | fix/promotion-closeout-result-authority |

## Open domain questions — ruling (자율권 정책 2026-07-22, 사용자 사후 override 가능)

- **Q1 (promotion environment freeze 시점)**: **정책 A(start-time freeze) 채택, 단 raw 값
  영속화 없이** — start에서 execution descriptor(env digest·resolved executable·content
  digest 등)를 freeze하고, resume은 current allowlist 환경을 재구축해 descriptor와
  **exact-match일 때만 실행**, 불일치는 typed refusal + 명시적 새 preflight revision 경로.
  근거: frozen authority 철학 정합 + proxy 등 secret 값의 durable 저장 회피(verifier 029
  권고). 현행의 조용한 drift 허용은 배제.
- **Q2 (promotion canonical result_digest)**: **approved GitResultTriple.result_digest**를
  OutcomeDelta.result_digest로 사용, target-ref apply 관찰은 기존
  `completion_evidence_refs`(outcome.py:707-728)로 결속 — 표현 불가 판명 시에만 schema 확장
  (verifier 031: RunCloseout top-level 필드 신설은 과잉 가능). promotion worker-result
  신설안은 기각(actor model 불일치 + digest 이중 등식 충돌).
- **Q3 (non-CLI review 함수 지원 범위)**: 4개 함수(ingest_feedback·validate_file·
  disposition_file·attach_review)에 **materialize와 동일한 ProjectContext proof 요구** —
  지원 API로 유지하되 front door 계약 통일. → fix/review-programmatic-context-proof (minor).
- **Q4 (HOME 아래 tool config trust boundary)**: **solo-local trust boundary로 수용**, 보증
  문구를 리뷰어 제안 수준으로 명시("환경 변수 provenance는 결속됨. 사용자 HOME 내부 tool
  configuration bytes는 local trust boundary임"). 독립 replay 주장 안 함. 029 수리에서
  runner-config-content NOT_OBSERVED 경계(engine.py:536-537)의 관측 편입 여부는 수리 브리프
  설계 시 판단.
- **부수 ruling — executable TOCTOU 위협 모델**: 목표는 "세션 간·정상 운영 drift의
  fail-closed 검출"까지. 동시(원자적) symlink 교체 방지는 solo-local trust boundary 밖으로
  명시(verifier 029: rehash-then-spawn의 잔여 창 인정 — known boundary로 기록).

## 처분 요약

REAL 3 (major 3) / REJECTED 0 / NEEDS-RULING 0. open question 4건 전부 자율 ruling 확정(위
기록), 파생 minor 1건 등록(fix/review-programmatic-context-proof). blocker 없음 — major 3건은
다음 round가 downstream 작업을 소비하기 전 해소 대상. receipt는 frozen reviewer identity
불일치(frozen=codex/gpt-5.5-pro vs 회신=chatgpt:gpt-5.6-pro)로 pending — 정상. 채택 근거는 위
main 독립 검증이며 attestation 재작성 없음.
<!-- waystone triage: END -->
