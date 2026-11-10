# 성능 전후 수치

`uv run --no-project python -B scripts/bench.py` 출력(2026-11-10, Apple Silicon Mac, Python 3.12.13).
모든 측정은 샌드박스 + 가짜 자격증명 저장소 + 합성 데이터로 했다 — 실제 키체인·네트워크는 쓰지 않는다.
"before" 는 이전 설계의 처리 순서(신선도 검사 전에 저장소 읽기, 전체 재스캔, 상태 락 안의 네트워크,
모든 로그 줄을 stderr 로)를 같은 데이터에 다시 돌린 값이다.

| measure | before (baseline design) | after |
|---|---|---|
| credential-store reads, 10 dashboard polls × 5 fresh slots | 50 | 0 |
| bytes read by the next gateway scan (20 MB transcripts, 1 line appended) | 21,778,897 | 139 (0.3 ms) |
| longest state-lock hold in a tick with 3 × 0.2 s usage requests | ≥ 0.60 s | 2.9 ms |
| stderr lines after 1440 no-action daemon ticks | 1440 | 0 |
| `python -m limitpilot --version` (best of runs) | 70 ms (single 220 KB script) | 61 ms |

## 무엇을 바꿨나

1. **대시보드 폴링에서 저장소 읽기 0회** — 스냅샷이 신선하거나 429 백오프 중이거나 직전 조회 캐시가
   살아 있으면 키체인을 읽지 않는다. 토큰 만료 표시는 데몬이 남긴 만료 시각(비밀이 아닌 숫자)으로 한다.
   macOS 에서 `security` 프로세스 1회는 약 12 ms 이고 키체인 확인창을 부를 수 있었다.
2. **숨긴 탭은 폴링하지 않고, `/api/status` 는 ETag 로 304** — 상태 파일(mtime·크기)과 라이브 캐시가 그대로면
   서버가 상태를 다시 만들지 않는다.
3. **게이트웨이 토큰 집계는 증분** — 파일마다 `(inode, size, mtime, offset)` 커서를 두고 덧붙은 바이트만 읽는다.
   대시보드에서는 백그라운드 스레드가 돈다. 중복 제거 표는 5만 개로 상한을 둔다.
4. **데몬 로그 상한** — 데몬 모드(`LIMITPILOT_DAEMON=1`)에서는 stderr 에 오류만 쓴다. `limitpilot.log` 와
   `daemon.err.log` 는 1 MB × 4개로 돈다(최대 4 MB).
5. **네트워크는 상태 락 밖** — 락 안에서는 스냅샷 확보와 결과 병합만 한다. 토큰 갱신은 슬롯별 락으로 막아
   두 프로세스가 같은 회전 토큰을 동시에 쓰지 않게 한다.
6. **시작 비용** — 220 KB 단일 스크립트 대신 패키지라 바이트코드 캐시를 쓴다(위 61 ms 는 캐시가 데워진 값).
