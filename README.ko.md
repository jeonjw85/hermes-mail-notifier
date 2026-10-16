# Hermes Mail Notifier

[![Tests](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 한국어

네이버, 다음, Gmail의 중요한 새 메일을 요약해 기존 Telegram으로 보내는 Hermes Agent 플러그인입니다. 여러 계정을 연결할 수 있고, Hermes에 설정된 모델과 Telegram 연결을 그대로 사용합니다

## 설치

Hermes가 실행 중인 서버에서 설치

```sh
hermes plugins install jeonjw85/hermes-mail-notifier --enable
```

현재 프로필의 기존 Telegram 봇과 **개인 대화에서 `/mail`**을 보내고 한국어 / English, 메일 서비스 선택

비밀정보 없는 관리는 자격증명 페이지 없이도 사용 가능. 이 버전에는 공개 자격증명 페이지 URL이 없으므로 새 연결이나 비밀번호 교체 전에 운영자가 신뢰하는 HTTPS `credential_page_url` 설정 필요

비밀번호는 일반 대화로 보내지 마세요

대안으로 사용할 수 있는 안전한 터미널 설정

```sh
hermes mail-notifier setup --ui-language ko
```

설치와 설정은 **Telegram을 사용 중인 동일한 Hermes 프로필**에서 진행. 터미널은 비표시 입력, Telegram은 봇의 보안 reply-keyboard 버튼으로 연 암호화 Mini App만 사용

필요한 환경
- Linux 또는 macOS, Python 3.11 이상 (실행 시 설치된 Hermes의 Python 사용)
- native plugins, plugin CLI, `ctx.llm.complete_structured`, profile-scoped settings/storage, script-only cron, `hermes send --json`을 지원하는 Hermes
- Telegram 설정에는 `post_gateway_admission`, native Telegram 핸들러 등록과 unload 콜백, 인증된 프로필 라우팅, 실제 대화 origin 문맥도 필요
- 구형 또는 미호환 호스트는 설정을 안전하게 차단 (`incompatible_hermes_onboarding`), 기존 명령은 각 호스트 계약이 지원되는 범위에서 유지
- Windows 플러그인 호스트 미지원
- 현재 프로필의 모델과 Telegram 연결, 자동 확인을 위한 해당 프로필의 Hermes gateway 실행

예약 작업은 새 프로세스에서 인증을 다시 읽으므로, 모델과 Telegram 인증은 같은 프로필의 Hermes 설정 (`.env`, 인증 파일, 재로드 가능한 비밀정보 소스)에 저장해야 합니다

셸/systemd 환경변수에만 있는 인증 값은 `setup` 사전 검사에서 감지해 예약 작업 활성화 전에 안내. 해당 프로필의 Hermes 설정에 인증을 저장한 뒤 재실행

Hermes 소스 `d5aa9d7adf308ca466a118206c201254cf3ca977`에서 실제 호스트 계약 검증 완료

- 암호화된 자격증명 입력을 위한 Python 런타임 의존성: `cryptography>=50.0.1,<51`
- Hermes와 동일한 Python 환경에 설치 필요 (소스 체크아웃에서는 `python -m pip install -e .`)
- Telegram SDK는 Hermes에서 필요한 시점에 불러오며 플러그인의 별도 의존성이 아님
- cryptography가 없으면 자격증명 요청 전에 암호화 입력을 안전하게 차단하되 기존 check/status 명령과 플러그인 등록은 계속 사용 가능
- Node는 개발과 CI 상호운용성 테스트 전용, 런타임에는 불필요

## 검증과 출시 상태

- 오프라인 검증 범위: 고정 Hermes 인증, 로더, 재로드 계약, 암호화 입력 상호운용성, 관리와 프로세스 중단 복구
- 50계정, 10,000건 시뮬레이션을 누락과 중복 없이 완료
- 실제 Telegram, 메일 서버, 모델 정확도 시험과는 별개

공개 호스팅, Pages 활성화, 릴리스 기본 자격증명 페이지 URL은 아직 미설정

Android, iOS, Desktop의 실제 reply-keyboard와 암호화 반환, 서비스별 계정과 모델 및 전송, 원격과 컨테이너 및 인바운드 차단 배포 검증은 출시 게이트로 남아 있습니다. 어떤 Telegram 클라이언트나 배포 형태도 실제 검증 완료로 주장하지 않음

Mini App에는 HTTPS와 Telegram, Web Crypto 지원이 필요하지만 설치자의 대시보드나 도메인, 인바운드 포트는 불필요. 페이지를 사용할 수 없어도 자격증명 입력만 제한되며 기존 알림과 비밀정보 없는 관리는 유지

보안 입력을 쓸 수 없다면 일반 대화 대신 CLI 사용

| 호환성 대상 | 현재 근거와 한계 |
| --- | --- |
| Linux/macOS, Python 3.11+ 플러그인 코드 | CI 대상은 Ubuntu 3.11, 3.14이며 고정 호스트 계약은 3.14에서 실행. 해당 Hermes 릴리스 자체가 모든 구형 Python에서 실행된다는 뜻은 아님 |
| 로컬, 원격, NAT, 대시보드 없음 | 기존 봇과 아웃바운드 연결 사용. 새 listener, 도메인, 대시보드, 두 번째 `getUpdates` 프로세스는 없으며 실제 배포 시험은 미완료 |
| Docker 프로필 볼륨 | 아래 오프라인 두 컨테이너 write/resume smoke로 안전하게 생성한 합성 프로필의 한영 복구와 플러그인 데이터 및 인증의 0700/0600 권한 검사. 실제 메일과 Telegram, 불안전한 프로필 권한 복구, 임의 UID나 볼륨 정책 검증과는 별개 |
| Telegram Android, iOS, Desktop | 후보 클라이언트이며 실제 지원 버전은 아직 미기록. 각 버전에서 reply KeyboardButton 실행과 암호화 `sendData` 반환 확인 필요 |
| Telegram Web, 기타 클라이언트 | 브리지가 감지되어도 실제 검증 전에는 지원을 주장하지 않음. Web Crypto나 브리지 미지원, 페이지 차단 시 보안 입력 사용 불가 |
| 일반 브라우저, inline/menu Mini App 실행 | 지원하는 자격증명 반환 경로가 아님. 비밀번호를 일반 대화로 보내 우회하지 말 것 |

클라이언트는 HTTPS 정적 페이지와 Telegram에 접속해야 합니다. Hermes 호스트에는 별도로 Telegram API, 서비스별 IMAP/TLS (993), 설정한 모델과 인증 endpoint로의 아웃바운드 연결 필요

기존 Telegram polling/webhook 방식과 네트워크 요구사항은 변경하지 않음

### 운영자 전용 자격증명 페이지 설정

현재 프로필의 `HERMES_HOME/config.yaml`에서 `plugins.entries.hermes-mail-notifier.settings.credential_page_url` 설정

`settings.configuration` 내부가 아닌 같은 단계에 추가하고, 기존 설정에 병합하되 다른 플러그인이나 프로필 설정은 덮어쓰지 않도록 주의

```yaml
plugins:
  entries:
    hermes-mail-notifier:
      settings:
        credential_page_url: ""
```

위 빈 값은 의도적이며 사용 가능한 공개 URL이 아님. 운영자만 이 저장소의 `web/credentials/v1/` 프로토콜을 제공하는 신뢰하고 검증한 HTTPS 페이지 주소로 바꾼 뒤 gateway 재로드

- userinfo나 fragment가 있는 URL, HTTPS가 아닌 URL, 미지원 wire 버전은 거부
- 생성되는 공개 challenge는 `#mn=...` fragment에 있으며 비밀번호가 아님
- 대화 입력, `mail_notifier_setup`, 정책 도구로는 호스팅 주소 변경 불가
- 페이지 운영자는 비밀번호를 볼 수 있는 JavaScript를 제공할 수 있으므로 운영자에 대한 신뢰와 검증 필요
- 암호화는 전송 보호용이며 페이지 진위나 발신자 인증의 증거가 아님

`.github/workflows/pages.yml`은 수동 `workflow_dispatch` 전용으로, `web`만 게시하고 사이트 루트 아래 `/credentials/v1/` 경로를 유지하도록 준비. `github-pages` 환경과 제한된 배포 권한 사용

Pages 활성화나 공개 페이지 도달 가능성은 보장하지 않으며, 현재 작업에는 활성화, dispatch, 배포, 검증된 기본 URL이 포함되지 않음

이후 운영자 승인 출시에서 실제 HTTPS URL과 v1 프로토콜을 확인한 뒤 기본값 지정 필요. 새 프로토콜로 v1 경로를 덮어쓰지 말 것

직접 호스팅은 선택 사항이며 모든 설치자의 필수 준비물은 아님. 신뢰하는 페이지가 없다면 `hermes mail-notifier setup --ui-language ko`의 비표시 터미널 입력을 사용하고, 기존 확인과 비밀정보 없는 관리는 그대로 유지

```sh
# 먼저 개발·검증 절의 고정 parser·SDK 의존성을 준비하세요.
python -m pip install -e '.[dev]'
python -m unittest discover -s tests
python -m ruff check .
python -m pyright
python -m tests.load_scenario --output load-report.json
# 모델이 설정된 Hermes 서버에서 실행 (최대 360회 실제 모델 호출):
hermes mail-notifier evaluate --runs 3 --output mail-evaluation.json
```

[120개 한영 평가 사례](evaluation/README.md)는 직접 작성한 가상 메일로 구성. 단위 시험은 점수 계산 검증용이며, 실제 모델 정확도와는 별개

실제 AI 평가와 메일 서비스, Telegram 수신 검증은 서버에서 수행

## 계정 준비

| 서비스 | IMAP 서버 (TLS 993) | 준비 |
| --- | --- | --- |
| 네이버 | `imap.naver.com` | IMAP 사용 설정, 2단계 인증, 애플리케이션 비밀번호 |
| 다음(한메일) | `imap.daum.net` | IMAP 사용 설정, 카카오계정 2단계 인증, 앱 비밀번호 |
| Gmail | `imap.gmail.com` | Google 2단계 인증, 앱 비밀번호 발급 가능한 계정 |

공식 안내
- 네이버: [IMAP 설정](https://help.naver.com/service/30029/contents/21344), [앱 비밀번호 정책](https://notice.naver.com/notices/mail/22462)
- 다음: [IMAP 서버](https://cs.daum.net/faq/service/43/category/9234/detail/17988), [앱 비밀번호](https://cs.daum.net/faq/service/43/category/9234/detail/31143)
- Google: [앱 비밀번호 발급](https://support.google.com/accounts/answer/185833?hl=ko)

Gmail은 앱 비밀번호를 발급할 수 있는 계정만 지원합니다. 조직 정책이나 고급 보호 등의 이유로 발급이 불가능한 계정과 OAuth 인증은 현재 미지원

## Telegram 설정과 관리

`/mail`은 모델이 제시한 ID가 아닌 인증된 개인 대화에 결합

서비스 선택, 로그인 주소 입력, 앱 비밀번호 발급 안내, 신뢰하는 페이지가 설정된 경우 앱 비밀번호 안전하게 입력, 미리보기와 확인 순서로 진행

확인 후에만 모델과 새 기준점 검사, 인증과 설정 저장, 예약 갱신, 테스트 전송 수행

새 기본값: 5분, INBOX, 인증된 현재 개인 대화와 토픽. 요약 언어는 선택하거나 감지한 UI 언어를 제안하며 기존 값은 보존

이후에는 UUID 입력 없이 번호, 별칭, 주소, 상태가 있는 계정 버튼을 5개씩 넘겨 선택

- 계정 추가, 별칭과 로그인 및 메일함과 서비스 수정, 비밀번호 교체, 중지와 재개 지원
- 5/10/15/30/60분 주기, 독립적인 UI 언어와 요약 언어 설정
- 발신자, 도메인, 키워드, 추가 지침 설정과 명시적 비우기 지원
- 기존 고급 일정과 임의 요약 언어는 명시적으로 바꾸기 전까지 보존
- 현재 대화로 새 알림 목적지를 바꾸는 경우를 포함해 모든 편집은 미리보기와 확인 필요
- 기존 그룹과 토픽 목적지는 보존하되 새 그룹 검색 UI는 미제공

상태와 복구에서는 cron과 대기 작업 정보를 표시하며, 확인 후 설정과 테스트 갱신 가능

- 기록된 cron이나 테스트 전송 실패는 미완료 단계만 재실행, 저장된 인증과 기준점 및 모델 검사는 반복하지 않음
- 자동 확인은 임의로 재개하지 않으며, 자동 확인 재개를 확인하거나 명시적으로 일시정지를 유지한 채 관리 계속
- 충돌 시 현재 설정과 이전 후보를 비교하고 이전 시도를 확인 처리 가능
- 증거는 보존하며 롤백하거나 실패 단계를 완료 처리하지 않고 현재 설정에서 새로 검토
- 준비된 알림의 문구와 목적지, 계정 ID, UID, 기존 작업은 유지
- 서비스, 로그인, 메일함 변경 시 이전 연결을 중지하고 새 연결 생성

설정 저장 후 테스트 목적지에 도달할 수 없다면 관리 계속 (테스트 미완료)로 이전 증거를 보존한 채 새 목적지 변경을 검토하고 확인 가능. 테스트 성공으로 처리하거나 일시정지를 해제하지 않으며, CLI setup도 재시도를 거절하면 같은 경로 제공

계정 중지만 적용하는 변경은 확인이 필요하지만 모델이나 인증 재로드, 전송 장애와 무관하게 진행 가능. 기존 cron 상태를 유지하고 작업을 새로 만들거나 재개하지 않으며 활성화 검사 통과를 주장하지 않음

다른 변경을 함께 저장하거나 계정을 재개할 때는 활성화 검사 수행

입력과 보관 한도
- 비밀정보 없는 초안: 마지막 변경 후 최대 24시간 보존
- 확인 대기 비밀번호: 프로세스 메모리에 최대 10분 보관, 만료되거나 재시작하면 재입력
- 새 텍스트 편집: UTF-8 16 KiB 이하, 초과 입력은 거부하고 임의로 줄이지 않음
- 기존 긴 값: 보존하되 일반 텍스트 출력만 요약
- 암호화 Mini App 전송: 별도의 4096바이트 한도 유지

페이지 운영자, Telegram 클라이언트, Hermes 호스트는 신뢰 경계. 동일한 Hermes 프로필에 허용된 사용자는 관리 권한을 공유하며 다중 테넌트 계정 서비스가 아님

## 터미널 설정 마법사

`setup`에서 계정과 알림 기준을 함께 관리

1. 새 설치에서는 작업 메뉴 없이 서비스와 로그인 주소부터 입력
2. 별칭은 주소, 메일함은 INBOX를 제안하며 필요하면 고급 계정 항목 수정 후 앱 비밀번호를 비표시 터미널 입력으로 전달
3. 연결 결과와 미리보기 확인 (목적지, 주기, 언어, 판단 기준 등의 전체 고급 설정은 선택 사항)
4. `yes`로 발행을 명시적으로 확인한 뒤 합성 메일 모델 검사, 새 IMAP 시작점 확인, 인증과 설정 저장, 소유 예약 작업 갱신, Telegram 테스트 메시지 ID 확인 수행
5. 이후 관리에서는 `add`, `edit`, `disable`, `enable`, `done` 사용. 같은 서비스의 복수 계정도 가능하며 변경 사항은 확인 후 발행

터미널 새 설치 기본값: 5분마다 확인, 선택하거나 감지한 UI 언어로 요약, INBOX, 현재 프로필의 Telegram home channel

기존 주기, 목적지, 요약 언어 (`fr` 등 포함), 판단 기준, 운영 한도는 보존

목적지는 `telegram`, `telegram:123456789`, `telegram:-1001234567890:456`처럼 지정하며, 마지막 숫자는 토픽 ID

- `edit`: 계정 ID 또는 표시 번호로 별칭, 비밀번호, 연결 정보 수정
- `disable` / `enable`: 계정 확인 중지와 재개 (대기 중인 알림은 보존)
- Enter: 현재 값 유지, 발신자와 키워드 목록 및 추가 판단 기준에서 `-` 입력 시 비우기
- 입력 중 Ctrl-C: 취소. 저장 이후 외부 단계에서 문제가 나면 완료된 단계와 오류 코드를 표시하며, 같은 `setup`을 재실행해 복구 가능
- 잘못된 항목은 그 자리에서 다시 입력. 연결 실패 시 `retry`, `password`, `back`, `cancel` 중 선택하며, 앞서 입력한 다른 계정은 저장할 때까지 유지
- 비밀정보 없는 초안은 별도 `setup.db`에 마지막 변경 후 최대 24시간 보존. 검사된 비밀번호는 메모리에만 최대 10분 보관하므로 만료되거나 재시작하면 재입력 필요
- 불명확한 인증 쓰기는 비밀번호 교체 성공으로 간주하지 않으며, cron이나 테스트 전송만 복구할 때는 이미 검증하고 저장한 비밀번호나 시작점을 다시 만들지 않음
- 다른 세션의 설정 변경은 덮어쓰지 않고 `needs_review`로 표시. `already_running`은 입력을 보존하므로 재시도 가능
- 발행 시작 후에는 외부 저장을 취소하거나 롤백했다고 주장하지 않고 완료 단계를 표시. 테스트 전송 응답이 유실되면 같은 시도 ID의 테스트 메시지가 중복될 수 있음

**처음 연결한 시점 이후 도착하는 메일부터** 확인하며, 과거 메일은 한꺼번에 전송하지 않습니다. 읽은 새 메일도 처리 대상

별칭, 비밀번호, 판단 기준을 바꿔도 시작점은 유지. 서비스, 로그인 아이디, 메일함을 바꾸면 이전 연결을 중지하고 새 연결 생성

기존 cron의 일시정지 상태는 재설정해도 유지하며 자동 확인 완료 대신 `scheduled` / `resume_cron`으로 표시

`setup`을 재실행해 명시적으로 재개를 승인하거나 일시정지를 유지한 채 계정 관리 가능

명시적 `accounts enable/disable` 명령도 자동 확인을 재개하지 않고 관리를 계속하며, 이전 시도는 완료가 아닌 `acknowledged` 상태로 보존

모든 계정을 중지하면 cron도 일시정지되며, 별도 재개는 `hermes cron resume JOB_ID` 사용

설정 충돌 시 이전 후보와 `current_settings`를 비교한 뒤 이전 시도를 명시적으로 확인 처리하고 현재 설정으로 새 초안 시작 가능

외부 저장 증거는 보존하고 자동 롤백이나 병합은 하지 않으며, 새 변경은 다시 확인 필요

### 언어

기존 `language` 설정은 모델 요약과 알림의 고정 라벨, 대체 사유, 설정 완료, 진단 테스트 알림의 언어를 결정

`setup`에서 `ko` 또는 `en`을 입력하거나 `hermes mail-notifier policy set --language en` 사용

기존의 다른 요약 언어 값도 유지해 모델에 그대로 전달하되, 해당 언어의 고정 문구는 영어로 대체. 이미 준비된 알림의 문구와 목적지는 변경하지 않음

별도 `ui_language`는 기존 요약 `language`를 바꾸지 않고 한영 설정, 상태, 진단 안내를 선택

`/mail`에서 선택하거나 `setup --ui-language ko|en`, `accounts add/edit --ui-language ko|en` 사용

UI 언어 적용 우선순위
1. 명시적 선택
2. 저장된 UI 언어
3. Telegram `language_code` 또는 CLI `LC_ALL`, `LC_MESSAGES`, `LANG` 중 첫 비어 있지 않은 값
4. 영어

`ko-KR`, `en_US.UTF-8` 같은 지역 locale은 정규화하며 미지원 언어는 영어로 안내. CLI 도움말은 한영을 함께 표시

보안 Mini App 입력에는 위의 신뢰하는 HTTPS 페이지 설정과 실제 출시 검증이 여전히 필요

## 운영과 판단 교정

```sh
hermes mail-notifier accounts list --json
hermes mail-notifier accounts add --ui-language ko
hermes mail-notifier accounts edit ACCOUNT_ID --ui-language ko
hermes mail-notifier accounts disable ACCOUNT_ID
hermes mail-notifier accounts enable ACCOUNT_ID
hermes mail-notifier doctor --json
hermes mail-notifier doctor --live --json
hermes mail-notifier history list --account ACCOUNT_ID --limit 20 --json
hermes mail-notifier history show MAIL_ID
hermes mail-notifier feedback add MAIL_ID --decision notify --note "읽어야 하는 개인적인 부탁"
hermes mail-notifier feedback list
hermes mail-notifier feedback remove FEEDBACK_ID
hermes mail-notifier queue list --json
hermes mail-notifier queue retry MAIL_ID
hermes mail-notifier queue retry --account ACCOUNT_ID
hermes mail-notifier queue retarget MAIL_ID --to telegram:123456789
hermes mail-notifier policy show
hermes mail-notifier policy set --instructions "개인 초대와 업무 요청을 우선해줘"
hermes mail-notifier policy set --always-notify-senders boss@example.com
hermes mail-notifier policy set --clear always_notify_senders
```

- 기본 `doctor`: 로컬 설정, 인증 재로드, cron, 백로그 점검
- `--live`: 활성 메일함 로그인, 합성 메일의 모델 판단, Telegram 진단 메시지 전송까지 실행하며 사용자 메일은 가져오지 않음

진단 결과에는 오류 코드와 다음 조치를 함께 표시

- `history`: 저장된 내용을 명시적으로 조회
- `status`, `queue`: 본문과 요약 미포함
- 큐 변경: 메일 ID 또는 명시적 계정 필터 필요
- 전송 대기 재시도: 저장된 요약 재사용
- 기본 Telegram 채널: 준비 시 실제 채팅 ID와 토픽 ID로 저장, 기존 대기 알림의 목적지는 `retarget`으로 변경
- 계정 중지와 재개: UID 기준점과 대기 작업 보존

교정해도 원래 판단은 유지하며, 완료 메일은 다시 보내지 않습니다

- 교정 사례: 최근 200건을 최대 180일 보존, 동일 발신자 사례 최대 5개를 이후 AI 판단에 제공
- 본문: 판단 완료 또는 7일 후 제거, 완료 판단 이력은 30일 보존
- `status --json`: 한도, 백로그, 수집 재시도 상태, 사용량 확인 (제공되지 않은 비용은 알 수 없음으로 표시)

Hermes 대화 도구: `mail_notifier_check`, `mail_notifier_status`, `mail_notifier_history`, `mail_notifier_queue`, `mail_notifier_feedback`, `mail_notifier_policy`, `mail_notifier_doctor`, 인자 없는 `mail_notifier_setup`

설정 도구는 검증된 실제 Telegram 대화에서만 패널을 열고, 다른 실행 문맥에서는 `/mail`을 안내. 기존 명시적 정책 도구와 CLI 명령도 유지

비밀번호는 보안 Mini App 또는 비표시 터미널 설정에서만 입력

## 중요도 기준

적용 순서

1. 제외 발신자: 모델 호출 없이 제외
2. 명시적 필수 알림 발신자 (`always_notify_senders`): 사용자가 지정한 필수 알림으로 처리하고 AI로 요약
3. 일반 메일: Hermes AI가 업무나 개인 연락의 의미, 수신자 관련성, 읽거나 대응할 필요성을 판단. 중요 발신자와 키워드, 추가 자연어 기준은 판단 참고용

발신자는 `boss@example.com`처럼 정확한 주소 또는 `@example.com`처럼 정확한 도메인으로 지정. 대소문자는 구분하지 않고, 도메인 규칙은 하위 도메인까지 자동 확장하지 않습니다

키워드가 있다는 이유만으로 AI의 제외 판단을 덮어쓰지는 않음

기본 판단 기준
- 우선 알림: 업무 요청과 개인 연락, 기한, 일정, 계약 변경, 결제 실패, 중요한 계정 보안 변화
- 기본 제외: 광고, 홍보, 뉴스레터, 일상적인 영수증
- 자동 발송된 예약 취소도 대응이 필요하면 알림

추가 기준 입력 예: `면접 일정 변경과 계약 마감일을 우선해서 알려줘`

알림 항목: 계정, 발신자, 제목, 판단 이유, 우선순위, 요약, 할 일, 기한, 메일 ID

AI 분류와 처리
- 분류: 업무, 개인, 거래, 홍보, 뉴스레터, 판단 불충분
- 처리: `notify`, `suppress`, `review`
- 명시된 행동이나 기한이 없으면 한국어는 `명시 없음`, 영어는 `Not specified` 표시
- 본문이 불완전하면 `본문 확인 필요` 또는 `Check the email body` 표시
- 불완전한 본문을 AI가 제외하려 하면 원문 확인 알림으로 전환
- 기계용 코드, 도구 필드, CLI 명령은 번역하지 않음

## 운영 명령

```sh
hermes mail-notifier check
hermes mail-notifier status
hermes mail-notifier status --json
hermes mail-notifier setup
```

`check`는 자동 확인과 같은 경로로 실행
- 종료 코드: 정상 0, 계정이나 모델, 전송 등의 오류 발생 시 1
- 이미 확인 중이면 추가 실행은 상태만 반환

예약 작업 ID는 `status`에서 확인

```sh
hermes cron status
hermes cron pause JOB_ID
hermes cron resume JOB_ID
```

메일 서버의 UIDVALIDITY가 바뀌면 해당 계정의 새 수집을 `uidvalidity_changed`로 보류하되, 이미 저장된 본문 판단과 준비된 알림 전송은 계속 가능

서버 상태 확인 후 아래 명령으로 새 시작점 설정

```sh
hermes mail-notifier reset ACCOUNT_ID
```

`reset`은 **선택 계정의 미완료 알림을 종료**하고 현재 메일함 이후 도착하는 메일부터 다시 시작. 영향을 받은 건수와 새 기준점 출력

Hermes 대화에서도 `mail_notifier_check`, `mail_notifier_status` 도구 사용 가능. 설치 후 기존 gateway가 새 도구를 인식하지 못하면 `hermes gateway restart`로 다시 불러오기

### 처리 방식과 재시도

- 메일함은 읽기 전용으로 열고 `BODY.PEEK` 사용, 메일 읽음 상태는 변경하지 않음
- 첨부파일 내용은 처리하지 않고, 본문 일부를 현재 Hermes 모델에 전달해 중요도와 요약 생성
- 메일당 원문 256 KiB, 모델 입력 본문 12,000자 제한
- 실행당 fetch, 모델 호출, 전송은 각각 기본 최대 20건이며 단계별, 계정별로 번갈아 처리
- 실행 후 240초부터 새 작업 시작 중단, 호출 제한은 IMAP 20초, 모델 45초, 전송 30초
- 새 메일과 재시도 작업이 없으면 모델과 Telegram 호출 없음, 모델 호출 비용은 기존 Hermes 모델 설정에 따름
- 전송 실패 시 요약을 보관하고 5분, 10분, 20분, 40분, 최대 1시간 간격으로 재시도하며 유효한 성공 응답 확인 후에만 완료 처리
- 수집, AI, 전송의 재시도 상태는 별도 저장. IMAP 장애 중에도 저장된 본문 판단과 준비된 알림 전송을 계속하며, 모델 재시도 때 메일을 다시 가져오지 않음
- 잘못된 AI 응답이 5회 누적되면 원문 확인 알림 생성. 전송이 10회 실패하면 `held`로 보존하고 `status`에 보류 건수 표시
- 기본 미완료 큐는 2,000건, 본문 캐시는 200건. 한도가 차면 수집을 늦추되 저장하지 못한 메일의 UID는 건너뛰지 않음
- 목적지 변경은 새 알림부터 적용, 이미 준비된 알림은 저장 당시 목적지 유지
- 반복 확인 시 같은 메일은 재전송하지 않음. 다만 Telegram 수신 후 성공 응답이 유실되거나 로컬 완료 기록 직전에 종료되면 재시도로 중복될 수 있음

### 자주 보는 오류

| 오류 | 확인할 내용 |
| --- | --- |
| `imap_connection_or_login_failed` | IMAP 사용 설정, 서비스별 로그인 아이디, 앱 비밀번호, 서버의 993 포트 연결 |
| `credential_unavailable` | 동일한 프로필에서 `setup`으로 비밀번호 다시 입력 |
| `credential_save_failed` | 해당 프로필의 자격증명 저장 권한과 관리 정책 |
| `credentials_not_reloadable` | 환경변수에만 있는 모델과 Telegram 인증을 해당 프로필의 Hermes 설정 또는 재로드 가능한 비밀정보 소스로 구성 |
| `credential_reload_check_failed` | 현재 Hermes의 인증 로더와 비밀정보 소스가 새 프로세스에서도 동작하는지 확인 |
| `model_unavailable` | 현재 프로필의 모델 설정, 인증, 연결 확인 |
| `invalid_model_output` | 모델 응답이 규격에 맞지 않으면 재시도, 5회 누적 시 원문 확인 알림 |
| `delivery_failed` / `delivery_unverified` | Telegram 연결과 목적지, Hermes 전송 결과 확인 |
| `cron_operation_failed` | `setup`의 확인 주기와 `hermes cron status` 확인 |
| `uidvalidity_changed` | 메일함 확인 후 해당 계정만 `reset` |
| `duplicate_owned_cron_jobs` | `hermes cron list`에서 중복된 플러그인 작업 정리 후 `setup` 재실행 |
| `incompatible_hermes` | 위의 필수 플러그인 기능을 지원하는 Hermes로 업데이트 |

## 저장 위치, 업데이트, 삭제

현재 프로필의 `HERMES_HOME` 기준

- 일반 설정: `config.yaml`의 `plugins.entries.hermes-mail-notifier.settings.configuration` (계정과 목적지를 하나의 스냅샷으로 저장)
- 신뢰하는 입력 페이지: `config.yaml`의 `plugins.entries.hermes-mail-notifier.settings.credential_page_url` (운영자 전용, 기본값은 빈 문자열)
- 앱 비밀번호: Hermes 자격증명 저장 경로의 `HERMES_MAIL_<ACCOUNT_ID>_APP_PASSWORD`
- UID 기준점, 대기 알림, 처리 이력: `plugin-data/hermes-mail-notifier/data.db`
- 비밀정보 없는 설정 초안과 복구 증거: `plugin-data/hermes-mail-notifier/setup.db` (schema v2, 확인 대기 비밀번호와 전송 개인키, 암호문은 저장하지 않음)
- cron 실행 진입점: `scripts/hermes-mail-notifier.py`

저장과 보존 정책
- 전체 원문과 첨부파일은 저장하지 않음
- 판단 대기 중인 정규화 본문은 최대 12,000자로 임시 저장하고 판단 완료 시 제거
- 7일이 지나면 본문 캐시를 비우고 필요할 때 다시 수집
- 판단 이유, 근거, 요약과 완료 이력은 30일 보존
- 미완료 알림은 재시도에 재사용
- 기본 상태 출력에는 메일 본문과 요약 미포함
- 설정, 비밀번호, DB는 설치 코드 외부에 저장

기존 DB는 최초 실행 시 프로필 잠금 아래 v3로 이전하며, 이전 스키마에 맞는 `data.db.v1.bak` 또는 `data.db.v2.bak` 백업 생성. 계정 ID, UID 기준점, 대기 알림, cron ID는 보존

**기존 중요 발신자와 키워드는 강제 중요 처리에서 AI 판단 참고로 변경**. 명시적 필수 알림은 설정의 `always_notify_senders`로 구분

`status`에서 DB와 정책 버전, 단계별 대기 및 보류 수, 마지막 실행 확인 가능

```sh
hermes plugins update hermes-mail-notifier
```

Hermes 실행 파일 경로를 바꿨다면 `setup`을 다시 실행해 cron 진입점 갱신

중지하거나 삭제할 때는 cron부터 정리

```sh
hermes cron pause JOB_ID
hermes cron remove JOB_ID
hermes plugins disable hermes-mail-notifier
hermes plugins remove hermes-mail-notifier
```

더 이상 필요 없다면 해당 프로필의 플러그인 데이터, 실행 스크립트, 계정별 비밀번호도 직접 정리 가능

## 서버에서 실제 연결 확인

1. `setup` 완료 후 Telegram의 설정 완료 메시지 확인
2. 설정 이후 회신이 필요한 업무 또는 개인 테스트 메일 전송
3. `hermes mail-notifier check`로 요약 수신 확인
4. 다시 `check`해서 중복 전송 여부 확인
5. gateway와 cron이 활성화된 상태에서 다음 새 메일의 자동 전달 확인

자동 테스트는 메일, 모델, 전송을 모두 가짜로 대체하므로 실제 계정 로그인과 Telegram 수신 여부는 위 서버 절차로 확인해야 합니다

## 개발과 검증

선언한 런타임과 개발 의존성을 먼저 설치

호환성 시험에는 고정 Hermes cron parser와 Telegram SDK도 사용하므로 위 revision의 소스를 형제 경로 `../hermes`에 준비하고 Telegram extra 설치

해당 호스트의 core 의존성은 Python 3.14+ 조건이 있으므로 3.11에서는 parser import용 두 의존성을 명시적으로 설치해야 합니다. 전체 호스트의 3.11 지원 인증과는 별개

기본 테스트에는 실제 서비스나 인증이 불필요하며, 호스트 전용 시험은 `HERMES_CONTRACT=1`이 아니면 의도적으로 skip

Node 24는 개발과 CI 전용이며 실제 Web Crypto 상호운용성에는 의존성이 설치된 같은 Python 사용

```sh
python -m pip install -e '.[dev]'
python -m pip install -e '../hermes[telegram]'
# Python 3.11 parser 전용 import 의존성:
python -m pip install 'ruamel.yaml==0.18.16' 'python-dotenv==1.2.2'
python -m unittest discover -s tests -v
python -m compileall -q __init__.py mail_notifier tests
python -m ruff check .
python -m pyright
PYTHON=python node --test tests/web/*.test.mjs
python -m tests.load_scenario --output load-report.json
```

호스트 게이트 준비: 같은 Python 3.14 환경에 Hermes 소스 `d5aa9d7adf308ca466a118206c201254cf3ca977`과 Telegram extra를 설치한 뒤 플러그인과 개발 의존성 설치

CI는 실제 호스트와 SDK import 후 전체 시험을 `HERMES_CONTRACT=1`로 실행하며 실패뿐 아니라 어떤 skip도 허용하지 않음. 임시 합성 프로필과 외부 서비스 대역을 사용하므로 실제 서비스의 인증이나 전송 시험과는 별개

```sh
python -c 'import cryptography, telegram; from hermes_cli.plugins import PluginContext, PluginManager; from gateway.run import GatewayRunner; from plugins.platforms.telegram.adapter import TelegramAdapter'
HERMES_CONTRACT=1 python -m unittest discover -s tests -v
HERMES_CONTRACT=1 python -m unittest tests.test_hermes_contract tests.test_telegram_contract -v
```

CI는 Python 3.11, 3.14에서 의존성 설치, unittest, compileall, ruff/pyright, Node 24 web/interop 순서로 실행. 별도 작업으로 고정 호스트, 중단 복구, 50계정/10,000건 load 검사 유지

workflow나 설정 검증은 HTTPS 도달 가능성이나 실제 기기의 안전성을 증명하지 않음

인바운드를 열지 않는 오프라인 Docker 검사 (daemon과 빌드 시 인터넷 필요)

```sh
python tests/container/check_context.py
docker build -f tests/container/Dockerfile -t mail-onboarding-smoke .
python tests/container/run.py
```

allowlist 빌드 context는 `.env`, DB, `.venv`, `.git`, 로컬 에이전트/계획/런타임 기록을 제외

네트워크 차단 실행 전에 의존성, 고정 호스트, Telegram extra를 이미지에 설치

runner는 UUID 이름의 임시 볼륨 하나를 만들고 서로 다른 write/resume 컨테이너에 해당 프로필 볼륨만 mount. 두 실행 모두 `--rm --network none`이며 공개 포트 없이 실행하고 자신이 만든 볼륨만 정리

외부 Telegram, IMAP, 모델 동작은 대역이며 출력에도 표시. daemon이나 빌드, 실행이 불가능하면 미수행이지 지원 완료가 아님

오프라인 smoke가 성공해도 다음 항목은 별도 운영자 승인 출시 게이트로 유지
- 공개 HTTPS와 실제 클라이언트 버전
- 새 프로필과 기존 실제 프로필, 원격 및 컨테이너 배포
- 네이버/다음/Gmail 로그인, 합성 모델 진단, Telegram 수신
- 이후 새 메일 알림의 한영 확인

승인 없이 실제 프로필에서 live 명령을 실행하지 마세요

`check_context.py`는 무해한 임시 필수/금지 파일, scratch 빌드, 로컬 출력을 사용해 Docker의 실제 필터 검사. 이미지를 pull하거나 컨테이너를 실행하지 않으며 CI에서도 이미지 빌드 전에 이 동작 검사 수행

## 라이선스

[MIT 라이선스](LICENSE)로 배포
