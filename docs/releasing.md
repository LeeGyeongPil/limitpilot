# 릴리스 절차

## 버전

`src/limitpilot/__init__.py` 의 `__version__` 이 유일한 출처다. `pyproject.toml` 의 `version` 도 같은 값으로
고친다(빌드 백엔드 `uv_build` 는 동적 버전을 지원하지 않는다 — `tests/test_packaging.py` 가 두 값이 같은지 확인한다).
메뉴바 앱의 `Info.plist`, User-Agent, 대시보드 health 응답은 실행 중에 `__version__` 을 읽는다.

## 확인

```sh
uv run --no-project python -B -m unittest discover -s tests -t .
uv run --no-project python -B scripts/build_icons.py --out /tmp/limitpilot-icons --check assets/icon
```

## 패키지

```sh
uv build --out-dir /tmp/limitpilot-dist
```

## macOS 앱(선택)

사용자는 `limitpilot widget install` 로 자기 Mac 에서 직접 컴파일하므로 배포용 앱은 필수가 아니다.
유니버설 바이너리와 서명·공증이 필요하면:

```sh
scripts/build_app.sh                                   # ad-hoc 서명, dist/LimitPilot.app
CODESIGN_IDENTITY="Developer ID Application: …" scripts/build_app.sh
CODESIGN_IDENTITY=… NOTARY_PROFILE=<notarytool 키체인 프로필> scripts/build_app.sh
```

서명 인증서·키·로컬 설정 파일은 레포에 두지 않는다(`.gitignore` 가 `*.p12`·`*.cer` 등을 막는다).

## 아이콘

`scripts/build_icons.py` 의 기하 상수가 원본이다. 바꾼 뒤 `--out assets/icon` 과
`--out src/limitpilot/native/icons` 로 다시 만들고(SVG 는 assets 에만) 커밋한다.
