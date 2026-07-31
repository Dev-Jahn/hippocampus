---
name: dispatch
description: 병렬 위임 웨이브 운영 규약 — 여러 작업을 외부 실행기(codex exec)·subagent에 동시 위임하고 main이 회수·검증·머지한다. 사용자가 "병렬로 launch", "함대", "wave", "나눠서 시켜", "가능한 task 전부 착수"를 지시하면 사용. 단일 위임이라도 호출 패턴·worktree 격리가 필요하면 참조.
---

# hippo: dispatch — 병렬 위임 웨이브

fleet-dispatch의 계승·개정판. 개정 근거는 전부 dogfooding 감사와 Opus 5 guide 실측이다.

## 0. 발사 전 30초

1. `hippo prior show` — 이 kind에 어떤 (모델·effort)가 실측으로 유리했는지 확인.
   프라이어는 조언이다: 최종 라우팅은 지금 작업의 난이도·볼륨으로 main이 판단한다.
2. `hippo directive list --active` — 살아있는 제약(GPU 범위, 보류 정책 등)을 브리프에
   반영한다. **공용 브리프(COMMON)와 개별 브리프의 제약 조항이 충돌하는지 grep으로
   사전 대조**(모순 조항이 fail-closed NO-GO를 만든 실사고).
3. 자산 프리플라이트: 브리프가 지목하는 파일·모델·데이터가 실존하는지 main이 확인.

## 1. 역할 라우팅

| 작업 성격 | 할당 |
|---|---|
| 레지스트리·머지·게이트·push·수용 판정 | main 직접 |
| 명령 하나로 끝나는 실험(soak/bench) | main의 `run_in_background` Bash |
| 파일 편집 수반 구현·조사 | 외부 실행기 lane (전용 worktree) |
| **타 실행기 산출물**의 경계 검증 | 검증 lane — 예산은 §4 |
| 고도 설계 | 독립 duo 설계 → main synthesis |

- **자기 작업 재검증을 위한 subagent를 띄우지 마라** — 신형 모델은 스스로 검증한다.
  검증 lane은 *다른* 실행기의 산출물(경계)에만 쓴다.
- 소형 잡무는 위임하지 마라: 프롬프트 작성 비용이 직접 하는 비용을 넘으면 배보다 배꼽.

## 2. 발사 규약

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/dispatch.sh" \
  --kind kernel-impl --scope "pass2 tensorize" --task feat/x \
  -m gpt-5.6-sol -c model_reasoning_effort=high \
  -C <worktree> --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
  "$(cat prompts/COMMON.md prompts/<task>.md)"
```

- 래퍼가 `ev:dispatch`를 자동 기록하고 dispatch id를 첫 줄에 찍는다.
- `${CLAUDE_PLUGIN_ROOT}`를 쓸 수 없는 소비 프로젝트에서는 위 plugin 경로를 해소한
  프로젝트 shim(예: `tools/dispatch`)을 두고 그 shim을 호출한다.
- 래퍼가 `codex exec … < /dev/null`을 내장한다(stdin 미폐쇄 hang 방지). call-site에서
  `< /dev/null`을 중복해도 무해하다. 명령 자체는 harness `run_in_background`로 발사 —
  nohup/disown 등 harness 밖 detach 금지(orphan 사고 이력).
- 프롬프트는 scratchpad 파일로(COMMON + 개별 브리프 concat). 여러 기는 한 메시지에
  묶어 병렬 발사.
- **어휘 중립화**: GPU 메모리 겹침·오염·주입 같은 표현이 실행기 콘텐츠 필터에
  cybersecurity 오탐된 실사고 10건 — 안전성 서술은 학술 중립어로 쓴다.
- 중간 지시가 필요하면 kill 후 session-id 지정 resume(병렬 lane에서 `--last` 금지).

## 3. 브리프 계약

① 배경("착수 전 정독" 목록 — 실행기는 맥락 제로; 권위 원천은 **작업자 트리에 실재하는
파일만** 지정 — gitignored 문서 지정은 열 수 없는 죽은 참조가 된 실사고) ② 범위/비-범위
③ 검증 요구: RED 먼저, acceptance는 착수 전 pre-register하되 **구현 지시가 아니라
성질(property)로**("이렇게 고쳐라"식 조항이 2 lane × 4회전 발산을 유발, 성질로 바꾸자
수렴한 실사고) ④ 보고서 저장 경로 + stdout 요약 ⑤ 머지 주의(hot-file은 구획을 브리프에서
사전 분할) ⑥ **전제가 재현되지 않으면 조기 NO-GO 보고**(연구 가설 기각은 흔하다)
⑦ 장시간 실행은 background로 걸고 대기는 Monitor/통지로 — **포그라운드 sleep 폴링
금지**(10분 하드타임아웃 연쇄 실사고) ⑧ **재위임 일체 금지 명시**("구현은 네가 직접.
codex exec·subagent·프로젝트 내 하네스 재호출 금지" — 금지 조항이 없자 기체가 336k 토큰
재위임 나선으로 커밋 0 종료한 실사고) ⑨ 검증 lane의 동적 재현은 **tempdir에서만** —
실 repo 상태·문서 불가침("읽기 전용" 지시만으로는 오염을 못 막은 실사고).

## 4. 검증 예산 (고정 의례가 아니라 증거 비례)

- PRIORS의 executor별 반증률에 비례해서 배분: 반증률 높은 곳에 lane당 검증 1기,
  낮은 곳(수용률 안정)은 spot-check나 생략. 근거가 없으면 첫 웨이브는 검증 붙이고
  이후 데이터로 조정.
- **검증자 브리프에 severity 상한·보수성 지시를 넣지 마라** — 문자적으로 순종해 실제로
  덜 보고한다. "발견 전부 보고, 필터링은 회수 측에서"로 쓴다.
- 검증 verdict를 근거로 outcome을 기록한다:
  `hippo log outcome --ref <id> --result refuted --attr work --note "..."`.
  귀속을 정직하게: 산출물 문제=work, 브리프 결함=brief, 인프라 유실=harness —
  귀속이 틀리면 프라이어가 거짓말을 한다.

## 5. 격리·회수·머지

- 편집 lane은 전부 `{PROJECT_ROOT}/.claude/worktrees/<name>` + 전용 branch. 브리프에
  base SHA를 박제하지 말고 "네 worktree의 시작 HEAD가 base"로.
- 병렬 착수 전 disjoint 확인은 파일권만으로 부족하다 — **삭제·개명하는 심볼을 추출해
  다른 lane·dev의 소비자까지 grep**(심볼 결합이 dev 전체를 깨뜨린 실사고).
- 회수: 통지 순서대로 output tail + 보고서 + `git log --oneline <base>..HEAD` 검수 →
  repo root에서 `git merge --squash` → **머지 후 표적 게이트 재실행** → green이면 push.
- **게이트 확인과 push를 한 호출에 묶지 마라** — `tail …; git push` 체이닝이 실패
  상태를 push한 실사고 2건. 게이트 rc를 확인한 *다음* 별도 호출로 push.
- 권위 측정(성능표 등)이 도는 동안 main은 tracked 파일에 커밋하지 않는다(측정 폐기 실사고).
- 수용 시: `hippo log outcome --ref <id> --result accepted`, task done, worktree/branch 정리.
- "실행 중" 보고 전 실측: 프로세스 테이블(+GPU면 `nvidia-smi --query-compute-apps`)로
  PID를 확인한다 — worktree 커밋 존재만으로 추정 금지(오보→사용자 정정 실사고 2건).

## 6. GPU 공유 (해당 시)

flock 실행기 경유(`gpu_run.sh`류, 프로젝트 고정 경로 — 세션 scratchpad에 두지 마라:
세션별 분리로 상호배제가 깨진 실사고). CUDA_VISIBLE_DEVICES 직접 지정 금지, CPU 작업
먼저·GPU 검증은 묶어서. 유휴 GPU가 있으면 main이 선제 배분한다(몰림 지적 실사고 2건).
