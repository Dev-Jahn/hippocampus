---
name: hippo
description: This is your hippocampus. Always use it.
---

# hippo — 프로젝트의 해마

hippo는 너를 통제하지 않는다. 기억을 대신 맡아줄 뿐이다: 무엇을 위임했고, 무엇이
수용·반증됐고, 사용자가 어떤 지시를 살려뒀는지. 판단은 언제나 너의 몫이다.

## 문법 한 줄

기록은 `hippo log <이벤트>` 한 문으로 들어가고, 명사는 그걸 읽는 창이다.
명사를 맨몸으로 부르면 기본 조회가 실행된다 (`hippo task`=목록, `hippo log`=최근
기록, `hippo directive`=살아있는 지시, `hippo prior`=라우팅 프라이어).

## 언제 무엇을

- 작업을 위임하며: `hippo log dispatch --id <새 id> --kind <태그> --exec <vehicle/model/effort> --scope "<한 줄>"`
  (codex exec은 `hippo dispatch --kind … --scope … -- <codex 인자>` 로 발사하면 자동 기록)
- 위임이 판정되면: `hippo log outcome --ref <id> --result accepted|revised|refuted|no-go|lost --attr work|brief|harness`
- 사용자가 지속 지시를 내리면: `hippo directive add --text "…" --scope turn|phase|durable`,
  끝나면 `hippo directive retract <id>`
- 외부 리뷰 회신을 받으면: `hippo log review --id <새 id> --base <sha> --source <출처> --findings <n>`
- 위임 라우팅 결정 전: `hippo prior` — 어떤 모델·effort가 실측으로 유리했는지
- 상태 확인: `hippo status`

## 안 해도 되는 것

턴마다 배경 서기(clerk)가 transcript에서 위 이벤트 대부분을 추론해 기록한다.
네가 CLI로 기록하는 것은 확실성을 높일 뿐이다. 기록을 빼먹어도 아무것도 망가지지
않는다 — 이 기관은 강제하지 않는다.
