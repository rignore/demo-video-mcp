# Demo Video MCP

영상의 목적, 시청 대상, 제품 가이드를 입력받아 시나리오를 계획하고
웹 또는 Android 앱의 시연 영상을 녹화하는 로컬 `stdio` MCP Server다.
Codex와 Claude Code에서 tool로 호출한다.

## 지원 범위

- Desktop web 및 responsive mobile web 녹화
- Android Emulator + Appium 기반 native app 녹화
- 최종 MP4를 고정 `1920x1080` canvas로 출력
- 브라우저에서 사용자가 직접 로그인한 profile 재사용
- 실행 전 mutation step 검출 및 명시적 승인
- 전체 flow의 자막 필요성 판단 및 화면별 자막 storyboard 승인
- 녹화 timeline 기반 WebVTT와 burned-in 자막 MP4 생성
- 제품별 guide, scenario, action을 plugin으로 분리

## 시작하기

Python 3.9 이상이 필요하다.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

Codex에 등록:

```bash
codex mcp add demo-video \
  --env DEMO_VIDEO_ALLOWED_ROOTS=/absolute/path/to/allowed-guides \
  -- /absolute/path/to/demo-video-mcp/bin/demo-video-mcp
```

Claude Code에 등록:

```bash
claude mcp add --transport stdio --scope user \
  --env DEMO_VIDEO_ALLOWED_ROOTS=/absolute/path/to/allowed-guides \
  demo-video -- /absolute/path/to/demo-video-mcp/bin/demo-video-mcp
```

상세한 tool flow, scenario schema, mobile/native 환경 구성은
[MCP Server 가이드](docs/MCP_SERVER.md)를 참고한다.

## 구조

- `src/demo_video_mcp`: 제품에 종속되지 않는 MCP core
- `plugins/generic-web`: 범용 웹 plugin
- `plugins/protectgo`: 선택형 제품 plugin 예시
- `mcp-config`: MCP client 설정 예시
- `tests`: unit 및 recording integration test

로그인 정보, APK, browser profile, 녹화 결과물은
`.demo-video-data/`에 저장되며 Git에 포함되지 않는다.
