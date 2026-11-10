# 보안 정책 / Security policy

보안 취약점을 발견하셨다면 **공개 이슈 대신** 이 레포의 **Security → Report a vulnerability**(비공개 취약점 신고)로 알려 주세요. 확인 후 답변드리고, 고친 버전을 릴리스하면서 함께 안내합니다.

If you find a security issue, please **do not open a public issue**. Use **Security → Report a vulnerability** in this repository to report it privately. We will reply after checking and announce the fix with a release.

LimitPilot 은 자격증명을 다루는 개인용 도구입니다. 토큰은 OS 자격증명 저장소(macOS 키체인 / Windows 사용자 프로필 파일)에만 두고, 로컬 대시보드는 내 컴퓨터(127.0.0.1)에서만 열리며 실행마다 바뀌는 API 토큰을 요구합니다. / LimitPilot handles credentials. Tokens live only in the OS credential store (macOS Keychain / Windows user-profile files); the local dashboard listens on 127.0.0.1 only and requires a per-run API token.
