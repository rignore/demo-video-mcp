# Demo Video MCP Server

`demo-video-mcp`는 영상 목적과 시청 대상, 가이드, 실제 화면을 기반으로
desktop, mobile web, Android native app 시연 영상을 녹화하는 로컬
`stdio` MCP Server다. 별도 웹앱을 실행하지 않으며 Codex 또는
Claude Code가 tool로 호출한다.

Codex/Claude Code가 시나리오를 계획하고, MCP Server는 다음 작업을
담당한다.

- Video Brief를 시나리오 작성 context로 변환
- 사이트 plugin과 가이드 조회
- 단일 화면 read-only 조사와 locator 후보 수집
- Scenario V1 검증
- desktop/mobile 전용 브라우저 로그인 세션 저장
- 외부 데이터 변경 단계 승인
- Playwright desktop/mobile-web 녹화 worker 실행
- Appium UiAutomator2 Android Emulator 녹화 worker 실행
- APK/.app.zip app artifact 비공개 등록
- 자막 필요성 판단 기준과 화면별 storyboard 검증
- WebM/MP4, WebVTT, burned-in 자막 MP4 및 manifest 생성

## 구조

```text
Codex / Claude Code
        │ stdio MCP
        ▼
demo-video-mcp
  ├─ brief planning context
  ├─ read-only page inspector
  ├─ capture preset resolver
  ├─ generic scenario validator
  ├─ plugin registry
  ├─ auth worker
  ├─ Playwright recording worker
  ├─ Appium Android recording worker
  └─ job / artifact store
        │
        ├─ plugins/generic-web
        └─ plugins/protectgo  # optional
```

Core package인 `src/demo_video_mcp`에는 Protect GO URL, 화면명, selector,
scenario가 없다. 사이트별 정보는 `plugins/<plugin-id>`에서만 로드한다.

## Codex 연결

MCP Server 등록은 최초 한 번 필요하다. repository의 Python dependency를
`.venv`에 설치한 뒤, 포함된 launcher가 해당 runtime을 자동으로 찾아
실행한다. 설치 명령은 repository [README](../README.md)를 따른다.

```bash
codex mcp add demo-video \
  --env DEMO_VIDEO_ALLOWED_ROOTS=/absolute/path/to/allowed-guides \
  -- /absolute/path/to/demo-video-mcp/bin/demo-video-mcp
```

등록 확인:

```bash
codex mcp list
codex mcp get demo-video
```

## Claude Code 연결

```bash
claude mcp add --transport stdio --scope user \
  --env DEMO_VIDEO_ALLOWED_ROOTS=/absolute/path/to/allowed-guides \
  demo-video -- /absolute/path/to/demo-video-mcp/bin/demo-video-mcp
```

프로젝트 단위 설정은 [`.mcp.json`](../.mcp.json)에 포함돼 있다.
Claude Code는 처음 연결할 때 project MCP Server 신뢰 승인을 요청한다.

## 목적 중심 호출 순서

1. `list_video_plugins`
2. 사용자 요청을 Video Brief V1으로 정규화
3. `get_video_planning_context`
4. 필요할 때 동일 capture 설정으로 `inspect_video_site`
5. Host model이 전체 flow의 자막 필요성을 판단하고 Scenario V1 작성
6. `start_video_login` → 브라우저에서 직접 로그인 및 프로젝트 선택
7. `finish_video_login`
8. Brief와 capture를 포함해 `create_video_job`
9. `preflight_video_job`
10. 자막 storyboard와 변경 단계를 사용자에게 표시한 뒤, preflight가
    요구하면 `approve_video_job`
11. `start_video_job`
12. `get_video_job` polling
13. MP4, `manifest.json`, 생성된 자막 artifact 경로 반환

MCP Server 내부에는 LLM이 없다. Codex 또는 Claude Code가 목적과 대상에
맞는 장면을 기획하고, MCP Server는 planning context 제공, 검증, 조사,
승인, 실행을 담당한다.

사용자는 동작 단계나 selector를 작성하지 않고 다음 정도만 요청하면 된다.

```text
Protect GO를 처음 사용하는 안전관리자에게 상황 확인과 대응 과정을
소개하는 1분짜리 영상을 만들어줘. dev profile을 사용하고 실제 데이터는
변경하지 마. 모바일 세로 화면으로 촬영해줘.
```

Host model이 위 요청을 다음 Video Brief로 정규화한다.

```json
{
  "schema_version": 1,
  "purpose": "상황 확인과 대응 과정을 소개",
  "audience": "Protect GO를 처음 사용하는 안전관리자",
  "key_messages": [
    "발생 상황을 빠르게 확인할 수 있다",
    "분석 결과를 검토할 수 있다",
    "대응 진행 상태를 파악할 수 있다"
  ],
  "duration_seconds": 60,
  "constraints": [
    "실제 데이터를 변경하지 않는다"
  ]
}
```

capture는 editorial brief와 분리한다.

```json
{
  "target": "mobile",
  "device": "pixel-7",
  "orientation": "portrait",
  "locale": "ko-KR",
  "timezone_id": "Asia/Seoul",
  "color_scheme": "light"
}
```

기존처럼 명시적인 template도 사용할 수 있다.

```text
protectgo plugin의 situation-response template을 사용해줘.
profile은 dev, 대상 상황과 입력값은 아래와 같아.
먼저 preflight까지만 진행해줘.
```

## Capture와 해상도

최종 MP4는 desktop, mobile, tablet 모두 `1920x1080`으로 고정한다.

| target | device | browser viewport | 최종 MP4 |
| --- | --- | --- | --- |
| `desktop` | `desktop-chrome` | `1920x1080` | `1920x1080` |
| `mobile` | `pixel-7` | device portrait/landscape | `1920x1080` |
| `mobile` | `iphone-13` | device portrait/landscape | `1920x1080` |
| `tablet` | `ipad-mini` | device portrait/landscape | `1920x1080` |

Desktop은 browser viewport와 최종 영상이 모두 Full HD다. Mobile/tablet은
responsive layout을 유지하기 위해 논리 browser viewport는 device preset을
사용한다. 최종 MP4 변환 시 비율을 유지한 채 `1920x1080` canvas 중앙에
배치한다. 임의 출력 해상도는 허용하지 않는다.

capture preset은 viewport만 바꾸지 않는다. Playwright context에 user agent,
`is_mobile`, `has_touch`, `device_scale_factor`를 함께 적용한다. 모든
preset은 설치 의존성을 줄이기 위해 Chromium으로 실행된다. 따라서
iPhone preset은 Safari/WebKit의 완전한 재현이 아니라 responsive mobile
web 검증용이다.

Android native 앱은 Appium UiAutomator2와 Android Emulator backend로
실행한다. 기기 원본 비율은 유지하고 최종 MP4는 web과 동일하게
`1920x1080` canvas에 배치한다. iOS `.app.zip`은 artifact로 등록할 수
있지만 XCUITest 실행 backend는 아직 제공하지 않는다.

## Android Native App

### Runtime 준비

MCP package에는 Android SDK와 Appium을 자동 설치하지 않는다. Native
촬영 host에는 다음 runtime이 필요하다.

- Android SDK `adb`
- Android Emulator와 촬영용 AVD
- Node.js와 Appium
- Appium UiAutomator2 driver
- FFmpeg

Appium과 driver 설치 예시는 다음과 같다.

```bash
npm install -g appium
appium driver install uiautomator2
```

Android Studio Device Manager에서 AVD를 만든 다음
`get_native_runtime_status`로 `adb`, AVD, Appium, UiAutomator2, FFmpeg를
확인한다. Appium server가 이미 `127.0.0.1`에서 실행 중이면 재사용하고,
실행 중이 아니면 worker가 로컬 `appium` executable을 시작한다.
원격 Appium URL은 허용하지 않는다.

### APK 전달과 등록

APK bytes나 Base64를 MCP argument로 보내지 않는다. 사용자가 APK를
로컬에 저장한 다음 절대 경로만 전달한다.

```json
{
  "platform": "android",
  "path": "/absolute/path/demo-dev.apk"
}
```

`register_native_app`은 다음을 수행한다.

- symlink와 빈 파일 거부
- APK ZIP 및 `AndroidManifest.xml` 확인
- 최대 크기 확인
- SHA-256 생성
- `.demo-video-data/native-apps/<artifact-id>`에 `0600`으로 복사
- 동일 SHA-256 artifact 재사용

원본 경로와 내부 저장 경로는 public artifact metadata에 반환하지 않는다.
Scenario와 job에는 `artifact_id`와 SHA-256만 결합한다. APK가 변경되면
기존 preflight와 승인은 무효화된다.

iOS Simulator용 `.app.zip`도 동일 contract로 등록할 수 있지만 현재
상태는 `contract_only`이며 실행할 수 없다. AAB는 설치 가능한 실행
artifact가 아니므로 받지 않는다.

### Native 호출 순서

1. APK를 로컬 경로에 저장
2. `register_native_app`
3. `get_native_runtime_status`
4. 필요하면 사용자에게 앱 설치·실행 시 network effect 가능성을 알림
5. 명시적 확인 후 `inspect_native_app`
6. `get_native_video_scenario_schema`
7. Host model이 Native Scenario V1 작성
8. `create_native_video_job`
9. `preflight_video_job`
10. 자막 storyboard, 모든 mutation step과 `plan_hash` 표시
11. 명시적 승인 후 `approve_video_job`
12. `start_video_job`
13. `get_video_job` polling
14. `1920x1080` MP4, manifest와 생성된 자막 artifact 반환

`inspect_native_app`은 control을 tap하지 않지만 APK를 설치하고 실행한다.
앱 시작 자체가 API 호출을 발생시킬 수 있으므로
`confirm_app_launch: true`가 필수다. 기본 반환값은 resource ID,
accessibility label, class와 locator 후보이며 UI text와 screenshot은
각각 명시적으로 활성화해야 한다.

### Native Scenario 예시

```json
{
  "schema_version": 1,
  "title": "Android dashboard tour",
  "platform": "android",
  "app_artifact_id": "<registered-artifact-id>",
  "package_id": "com.example.demo",
  "device": {
    "runtime": "emulator",
    "avd": "Pixel_7_API_35",
    "orientation": "portrait",
    "language": "ko",
    "locale": "KR"
  },
  "reset_policy": "preserve",
  "max_duration_seconds": 120,
  "steps": [
    {
      "id": "launch",
      "title": "앱 실행",
      "action": {
        "type": "launch"
      },
      "effects": [
        "potential_mutation"
      ],
      "approval": "required",
      "retry_policy": "never"
    },
    {
      "id": "wait-dashboard",
      "title": "대시보드 확인",
      "action": {
        "type": "wait_for",
        "target": {
          "by": "accessibility_id",
          "value": "Dashboard"
        }
      },
      "effects": [
        "local_read"
      ],
      "approval": "none",
      "retry_policy": "safe"
    }
  ]
}
```

Appium session은 승인 전 앱이 실행되지 않도록 `autoLaunch=false`로
생성한다. 첫 `launch` step은 반드시 하나 존재해야 하고 항상 mutation
승인 대상이다. `tap`, `fill`, `press_key`, `back`도 잠재적 mutation으로
취급한다.

Native 로그인 비밀번호나 token은 Scenario에 넣지 않는다. MVP에서는
사용자가 Emulator에서 직접 로그인한 뒤 `reset_policy: preserve`를
사용한다. 이 방식은 Emulator 상태에 의존하므로 재현 가능한 encrypted
Native profile은 후속 구현 대상이다.

## 화면 조사

`inspect_video_site`는 현재 URL 하나만 연다. crawl하거나 button/link를
클릭하지 않으며 다음 항목만 반환한다.

- visible heading
- navigation, link, button
- form label
- Scenario V1 locator 후보

`GET`, `HEAD`, `OPTIONS` 이외의 요청과 타 origin XHR/fetch를 차단한다.
반환된 UI 텍스트는 untrusted data로 취급하며 이후 click 승인을 대체하지
않는다. input value, cookie, storage, raw HTML, URL query는 반환하지 않는다.
단, 잘못 설계된 사이트에서 GET 자체가 상태를 변경하는 경우까지 완전한
read-only를 보장할 수는 없다.

인증 화면 screenshot은 기본적으로 저장하지 않는다.
`include_screenshot: true`를 명시한 경우에만 `.demo-video-data/inspections`
아래에 `0600` 권한으로 저장한다. Screenshot에는 고객 데이터가 포함될 수
있으므로 필요한 경우에만 사용한다.

## 로그인

ID와 비밀번호는 MCP argument나 대화에 입력하지 않는다.

`start_video_login`은 별도의 headed Chromium을 연다. Mobile 로그인이
필요하면 녹화와 동일한 capture를 전달한다. 사용자가 해당
브라우저에서 직접 로그인하고 프로젝트를 선택한 다음, 대화에서
“로그인 완료”라고 하면 Agent가 `finish_video_login`을 호출한다.

저장되는 Playwright storage state는 다음 위치에 `0600` 권한으로
보관된다.

```text
.demo-video-data/profiles/<profile-id>/state.json
```

인증 정보는 job, manifest 또는 영상 artifact 디렉터리로 복사하지 않는다.
OAuth callback의 query와 fragment는 login status 및 profile metadata에
저장하지 않는다. Browser 진입에 필요한 원본 login URL은 해당 auth
session의 `request.json`에만 `0600`으로 저장한다.

`read_video_guide`는 runtime data root와 `.auth` 같은 hidden 경로를
거부하므로 guide tool을 통해 storage state를 읽을 수 없다.

## Plugin contract

```text
plugins/<plugin-id>/
├── plugin.json
├── guides/
├── scenarios/
└── adapter.py  # 필요한 경우에만 사용하는 trusted local code
```

`plugin.json`은 다음을 선언한다.

- plugin ID와 version
- 허용 origin
- guide
- scenario template와 required variables
- 선택적 plugin action

새 plugin을 만들 때는 `get_video_plugin_schema` 또는
`demo-video://schemas/plugin/v1` resource를 정본으로 사용한다.

일반 사이트는 `generic-web` plugin만으로 처리한다. 사이트 고유 DOM
검증이 필요한 경우에만 별도 plugin을 추가한다.

## 실행 위험 판정

Core는 web의 `goto`, `click`, `fill`, `press`, `select_option`, `plugin`과
Native의 `launch`, `tap`, `fill`, `press_key`, `back` action을 잠재적
mutation으로 간주한다. Scenario가 read-only라고 기술해도 이 risk
floor는 낮아지지 않는다.

## 자막 판단 및 승인

Host model은 purpose, audience, key message와 전체 user flow를 기준으로
자막 필요성을 판단한다. onboarding, 교육, 외부 전달, 설명이 필요한
다단계 flow는 `required`를 사용한다. 짧은 내부 defect 증빙처럼 화면
자체로 의미가 분명하면 `not_required`를 사용한다.

```json
{
  "captions": {
    "decision": "required",
    "reason": "신규 운영자 교육용 다단계 flow입니다.",
    "language": "ko-KR",
    "output": "both"
  },
  "steps": [
    {
      "id": "show-dashboard",
      "title": "대시보드 확인",
      "hold_ms": 1800,
      "caption": {
        "screen": "홈 > 대시보드",
        "text": "현재 발생한 상황을 한눈에 확인합니다."
      }
    }
  ]
}
```

자막 scene의 `hold_ms`는 최소 1200ms다. `preflight_video_job`은
`caption_review.storyboard`에 화면명, 문구, scene, 노출 시간을 그대로
반환한다. 자막이 필요한 job은 mutation이 없어도 `AWAITING_APPROVAL`로
전환되며 승인된 `plan_hash`가 없으면 녹화를 시작할 수 없다.

`output`은 `sidecar`, `burned_in`, `both` 중 하나다. `both`는
`captions.vtt`와 자막이 화면에 합성된 `video-captioned.mp4`를 함께 만든다.
원본 `video.mp4`는 변경하지 않는다.

## 승인 규칙

Mutation 또는 required 자막이 포함된 job은 다음 값을 모두 충족해야
실행할 수 있다.

- `preflight_video_job` 완료
- 정확한 `plan_hash`
- 모든 mutation step ID
- `confirm_external_changes: true`

승인 이후 scenario, Brief, plugin, auth profile 또는 MCP Server version이
변경되면 기존 승인은 사용할 수 없다. Mutation을 시작한 뒤 실패하거나
취소된 job은 자동 재시도하지 않고 사용자 확인이 필요한 상태로 남긴다.

## Job artifact

```text
.demo-video-data/jobs/<job-id>/
├── brief.json       # Brief 기반 job일 때
├── request.json
├── scenario.json
├── preflight.json
├── approval.json
├── events.jsonl
├── manifest.json
└── artifacts/
    ├── recording.webm
    ├── native-recording.mp4
    ├── video.mp4
    ├── captions.vtt
    ├── video-captioned.mp4
    └── error.png
```

장시간 녹화는 MCP tool call을 점유하지 않는다. `start_video_job`이
worker를 시작하고 즉시 `job_id`를 반환하며, Agent가
`get_video_job`으로 상태를 조회한다.

`manifest.json`에는 실제 적용한 capture target, device, orientation,
browser viewport, raw record size와 고정 `1920x1080` output size가 포함된다.
자막이 생성되면 승인한 문구와 실제 `video_start_ms`, `video_end_ms` cue도
포함된다.
MP4 생성에 실패하거나 최종 해상도가 생성되지 않으면 job은
`SUCCEEDED`로 처리하지 않는다.

## 검증

```bash
PYTHONPYCACHEPREFIX=/private/tmp/demo-video-pycache \
PYTHONPATH="$PWD/src" \
DEMO_VIDEO_PROJECT_ROOT="$PWD" \
DEMO_VIDEO_PLUGIN_DIRS="$PWD/plugins" \
python3 \
  -m unittest discover -s tests -p 'test_*.py' -v
```

통합 테스트는 local HTTP fixture에서 desktop과 Pixel 7 mobile web을 실제
Chromium으로 녹화한다. Native는 local fake Appium W3C server가 반환한
세로 MP4를 Android worker가 처리한다. 세 결과물 모두 최종 해상도가
`1920x1080`인지 확인한다. 실제 Emulator/APK smoke test는 Android
SDK와 Appium이 설치된 host에서 별도로 실행해야 한다.
