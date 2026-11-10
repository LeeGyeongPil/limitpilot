# claude-account-pilot 에서 옮기기

LimitPilot 은 작성자의 이전 개인 프로젝트 claude-account-pilot 을 이어받았다. 데이터 폴더·키체인 서비스·
데몬 이름이 모두 바뀌었으므로, 예전 도구를 쓰던 PC 에서는 `limitpilot migrate` 로 한 번 옮긴다.

## 먼저 확인

```sh
limitpilot migrate --dry-run
```

아무것도 바꾸지 않고 아래 단계를 보여 준다. `~/.claude-autoswap/state.json` 이 있고 `~/.limitpilot/` 이 없을 때
`limitpilot list` 와 `doctor` 도 같은 안내를 띄운다(자동으로 옮기지는 않는다 — 키체인을 건드리기 때문).

## 옮기기

```sh
limitpilot migrate
```

순서가 중요하다. refresh token 은 한 번 쓰면 바뀌므로, 두 도구가 같은 계정을 동시에 갱신하면 한쪽이 죽은
토큰을 갖게 된다.

| 단계 | 하는 일 |
|---|---|
| 1 | 예전 데몬부터 멈춘다: 예전 상태 락을 잡고 `~/.claude-autoswap/.daemon-stopped` 표식 → `launchctl bootout gui/<uid>/com.<user>.claude-autoswap`(Windows 는 작업 `ClaudeAccountPilot-AutoSwap` 종료·삭제) → plist 와 `daemon/claude_autoswap.py` 삭제 |
| 2 | 실행 중인 예전 대시보드가 있으면 자기 nonce 로 종료를 요청한다(포트 8737, 헤더 `X-Autoswap`) |
| 3 | `state.json` 을 `~/.limitpilot/state.json` 으로 복사한다. 설정은 예전 동작 그대로(예전 기본값이던 유지 호출·리셋 전 소진은 켜진 채, 데몬이 있었다면 자동 전환도 켜진 채), `migratedFrom` 기록 추가 |
| 4 | 슬롯 자격증명을 키체인 서비스 `claude-autoswap` → `limitpilot` 으로 복사하고 하나씩 다시 읽어 비교한다(Windows 는 `slots/*.json`). **모두 일치한 뒤에만** 예전 항목을 지운다 |
| 5 | `~/.claude/settings.json` 의 `apiKeyHelper` 가 예전 스크립트(`…/.claude-autoswap/daemon/claude_autoswap.py gateway-key N`)를 부르면 limitpilot 의 데몬 사본으로 바꾼다 |
| 6 | 예전 격리 로그인 잔여물(`oauth-login-*` 폴더와 그 경로의 `Claude Code-credentials-<8자리>` 항목)을 지운다 |
| 7 | 예전 폴더에 `MIGRATED` 표식을 남긴다. 폴더 자체는 지우지 않는다 |

복사 중 하나라도 실패하면 새로 쓴 항목을 지우고 예전 항목은 그대로 둔 채 멈춘다. 문제를 고친 뒤 다시 실행하면 된다.

## 옮긴 뒤

- 예전 메뉴바 앱(`Claude Usage.app`)과 로그인 항목은 프로그램이 지우지 않는다. 직접 종료하고 지운다.
  예전 앱에서 "자동 전환 켜기" 를 누르면 데몬 두 개가 같은 자격증명을 두고 경쟁하므로 누르지 않는다.
  `limitpilot doctor` 와 `install-daemon` 은 예전 데몬이 남아 있으면 경고한다.
- 감시를 다시 켠다: `limitpilot install-daemon`(예전처럼 자동 전환까지 원하면 `--enable-auto`).
- 예전 폴더가 더 필요 없으면: `limitpilot migrate --purge-legacy`(이전이 끝난 뒤에만 동작).
