# Hermes Mail Notifier

[English](README.md) | 한국어

네이버·다음·Gmail의 **중요한 새 메일을 요약해 기존 Telegram으로 보내는 Hermes Agent 플러그인**입니다. 여러 계정을 연결할 수 있고, Hermes에 설정된 모델과 Telegram 연결을 그대로 사용합니다.

## 설치

이 프로젝트를 GitHub 저장소 루트에 올린 뒤, Hermes가 실행 중인 서버에서 설치합니다. `OWNER/REPOSITORY`를 실제 저장소 주소로 바꾸세요.

```sh
hermes plugins install OWNER/REPOSITORY --enable
```

최초 설정:

```sh
hermes mail-notifier setup
```

설치와 설정은 **Telegram을 사용 중인 동일한 Hermes 프로필**에서 실행하세요. 앱 비밀번호는 서버의 대화형 터미널에서 입력하며 화면에 표시되지 않습니다.

필요한 환경:
- Linux 또는 macOS, Python 3.11 이상. 실행 시에는 설치된 Hermes의 Python을 사용합니다.
- native plugins, plugin CLI, `ctx.llm.complete_structured`, profile-scoped settings/storage, script-only cron, `hermes send --json`을 지원하는 Hermes.
- 현재 프로필의 모델과 Telegram 연결. 자동 확인에는 해당 프로필의 **Hermes gateway가 실행 중**이어야 합니다.

예약 실행은 새 프로세스에서 인증을 다시 읽습니다. 모델·Telegram 인증은 같은 프로필의 Hermes 설정 (`.env`, 인증 파일, 재로드 가능한 비밀정보 소스)에 저장되어 있어야 합니다. **셸/systemd 환경변수에만 있는 인증 값**은 `setup`의 사전 검사에서 감지해 예약 작업을 활성화하기 전에 안내합니다. 해당 프로필의 Hermes 설정으로 인증을 저장한 뒤 다시 실행하세요.

Hermes 소스 `d5aa9d7adf308ca466a118206c201254cf3ca977`에서 실제 호스트 계약을 검증했습니다. 플러그인의 추가 Python 런타임 의존성은 없습니다.

## 계정 준비

| 서비스 | IMAP 서버 (TLS 993) | 준비 |
| --- | --- | --- |
| 네이버 | `imap.naver.com` | IMAP 사용 설정, 2단계 인증, 애플리케이션 비밀번호 |
| 다음·한메일 | `imap.daum.net` | IMAP 사용 설정, 카카오계정 2단계 인증, 앱 비밀번호 |
| Gmail | `imap.gmail.com` | Google 2단계 인증, 앱 비밀번호 발급 가능한 계정 |

공식 안내:
- 네이버: [IMAP 설정](https://help.naver.com/service/30029/contents/21344), [앱 비밀번호 정책](https://notice.naver.com/notices/mail/22462)
- 다음: [IMAP 서버](https://cs.daum.net/faq/service/43/category/9234/detail/17988), [앱 비밀번호](https://cs.daum.net/faq/service/43/category/9234/detail/31143)
- Google: [앱 비밀번호 발급](https://support.google.com/accounts/answer/185833?hl=ko)

Gmail은 앱 비밀번호를 발급할 수 있는 계정만 지원합니다. 조직 정책·고급 보호 등의 이유로 발급이 불가능한 계정과 OAuth 인증은 현재 지원하지 않습니다.

## 설정 마법사

`setup`에서 계정과 알림 기준을 함께 관리합니다.

1. `add`를 입력하고 서비스, 별칭, 로그인 아이디, 메일함, 앱 비밀번호를 입력합니다.
2. 연결 검사를 통과하면 필요한 계정을 추가합니다. 같은 서비스의 복수 계정도 가능합니다.
3. `done`으로 계정 편집을 마칩니다.
4. Telegram 목적지, 확인 주기, 요약 언어, 중요·제외 발신자, 키워드, 추가 판단 기준을 입력합니다.
5. 설정·예약 작업을 저장하고 Telegram에 설정 완료 메시지를 보냅니다.

기본값은 **5분마다**, **한국어**, **INBOX**, **현재 프로필의 Telegram home channel**입니다. 목적지는 `telegram`, `telegram:123456789`, `telegram:-1001234567890:456`처럼 지정할 수 있습니다. 마지막 숫자는 토픽 ID입니다.

- `edit`: 별칭·비밀번호·연결 정보 수정. 계정 ID 또는 표시 번호를 입력합니다.
- `disable` / `enable`: 계정 확인 중지·재개. 대기 중인 알림은 보존합니다.
- Enter: 현재 값 유지. 발신자·키워드 목록과 추가 판단 기준에서 `-`: 비우기.
- 입력 중 Ctrl-C: 취소. 저장 이후 외부 단계에서 문제가 나면 완료된 단계와 오류 코드를 표시하므로 같은 `setup`을 재실행해 복구할 수 있습니다.

**처음 연결한 시점 이후 도착하는 메일부터** 확인합니다. 과거 메일은 한꺼번에 전송하지 않습니다. 읽은 새 메일도 처리하며, 별칭·비밀번호·판단 기준을 바꿔도 시작점을 초기화하지 않습니다. 서비스·로그인 아이디·메일함을 바꾸면 이전 연결을 중지하고 새 연결을 만듭니다.

기존 cron의 일시정지 상태는 재설정해도 유지합니다. 모든 계정을 중지하면 cron도 일시정지되므로, 계정을 다시 켠 뒤에는 아래 `hermes cron resume JOB_ID`로 재개하세요.

## 중요도 기준

우선순위는 다음과 같습니다.

1. **제외 발신자**: 모델 호출 없이 제외합니다.
2. **중요 발신자 또는 키워드**: 중요 메일로 지정하고 모델로 요약합니다.
3. **나머지**: 기본 기준과 사용자가 입력한 자연어 기준으로 판단합니다.

발신자는 `boss@example.com`처럼 정확한 주소 또는 `@example.com`처럼 정확한 도메인을 사용합니다. 대소문자를 구분하지 않으며, 도메인 규칙은 하위 도메인까지 자동 확장하지 않습니다. 키워드는 제목과 본문의 부분 문자열로 비교합니다.

기본적으로 회신·확인이 필요한 직접 연락, 기한·일정·계약, 결제 문제, 중요한 계정 보안 변화를 우선합니다. 광고·뉴스레터·일상적인 영수증은 낮은 중요도로 판단합니다. 예를 들어 추가 기준에 `면접 일정 변경과 계약 마감일을 우선해서 알려줘`를 입력할 수 있습니다.

알림에는 계정, 발신자, 제목, 요약, 할 일, 기한을 표시합니다. 명시된 행동·기한이 없으면 `명시 없음`, 본문이 잘리거나 추출되지 않으면 `본문 확인 필요`를 표시합니다.

## 운영 명령

```sh
hermes mail-notifier check
hermes mail-notifier status
hermes mail-notifier status --json
hermes mail-notifier setup
```

`check`는 자동 확인과 같은 경로를 사용합니다. 정상 종료는 0, 계정·모델·전송 등의 오류가 있으면 1입니다. 이미 확인 중이면 추가 실행은 상태만 반환합니다.

예약 작업 ID는 `status`에 표시됩니다.

```sh
hermes cron status
hermes cron pause JOB_ID
hermes cron resume JOB_ID
```

메일 서버의 UIDVALIDITY가 바뀌면 해당 계정을 `uidvalidity_changed`로 보류합니다. 서버 상태를 확인한 뒤 새 시작점으로 전환합니다.

```sh
hermes mail-notifier reset ACCOUNT_ID
```

`reset`은 **선택 계정의 미완료 알림을 종료**하고 현재 메일함 이후 도착하는 메일부터 다시 시작합니다. 영향을 받은 건수와 새 기준점을 출력합니다.

Hermes 대화에서도 `mail_notifier_check`, `mail_notifier_status` 도구를 사용할 수 있습니다. 설치 후 기존 gateway가 새 도구를 아직 인식하지 않으면 `hermes gateway restart`로 다시 불러옵니다.

### 처리 방식과 재시도

- 메일함은 읽기 전용으로 열고 `BODY.PEEK`를 사용합니다. 메일 읽음 상태를 변경하지 않습니다.
- 첨부파일 내용은 처리하지 않습니다. 본문 일부가 현재 Hermes 모델로 전달되어 중요도·요약을 생성합니다.
- 메일당 원문 256 KiB, 모델 입력 본문 12,000자, 한 번에 최대 20건입니다. 계정별로 번갈아 처리합니다.
- 실행 후 240초부터 새 작업을 시작하지 않습니다. IMAP 20초, 모델 45초, 전송 30초의 호출 제한을 둡니다.
- 새 메일과 재시도 작업이 없으면 모델·Telegram을 호출하지 않습니다. 모델 호출 비용은 기존 Hermes 모델 설정을 따릅니다.
- 전송 실패 시 요약을 보관하고 5분 → 10분 → 20분 → 40분 → 최대 1시간 간격으로 재시도합니다. 유효한 성공 응답을 확인한 뒤에만 완료 처리합니다.
- 반복 확인 시 같은 메일을 다시 보내지 않습니다. 다만 Telegram은 수신했으나 성공 응답이 유실되거나 로컬 완료 기록 직전에 종료된 경우에는 재시도로 중복될 수 있습니다.

### 자주 보는 오류

| 오류 | 확인할 내용 |
| --- | --- |
| `imap_connection_or_login_failed` | IMAP 사용 설정, 서비스별 로그인 아이디, 앱 비밀번호, 서버의 993 포트 연결 |
| `credential_unavailable` | 동일한 프로필에서 `setup`으로 비밀번호 다시 입력 |
| `credential_save_failed` | 해당 프로필의 자격증명 저장 권한·관리 정책 |
| `credentials_not_reloadable` | 환경변수에만 있는 모델·Telegram 인증을 해당 프로필의 Hermes 설정 또는 재로드 가능한 비밀정보 소스로 구성 |
| `credential_reload_check_failed` | 현재 Hermes의 인증 로더와 비밀정보 소스가 새 프로세스에서도 동작하는지 확인 |
| `model_unavailable` | 현재 프로필의 모델 설정·인증·연결 |
| `invalid_model_output` | 모델 응답이 규격에 맞지 않아 대기 유지. 다음 주기에 재시도 |
| `delivery_failed` / `delivery_unverified` | Telegram 연결·목적지·Hermes 전송 결과 확인 |
| `cron_operation_failed` | `setup`의 확인 주기와 `hermes cron status` 확인 |
| `uidvalidity_changed` | 메일함 확인 후 해당 계정만 `reset` |
| `duplicate_owned_cron_jobs` | `hermes cron list`에서 중복된 플러그인 작업 정리 후 `setup` 재실행 |
| `incompatible_hermes` | 위의 필수 플러그인 기능을 지원하는 Hermes로 업데이트 |

## 저장 위치·업데이트·삭제

현재 프로필의 `HERMES_HOME` 기준:

- 일반 설정: `config.yaml`의 `plugins.entries.hermes-mail-notifier.settings.configuration` (계정·목적지를 하나의 스냅샷으로 저장)
- 앱 비밀번호: Hermes 자격증명 저장 경로의 `HERMES_MAIL_<ACCOUNT_ID>_APP_PASSWORD`
- UID 기준점·대기 알림·처리 이력: `plugin-data/hermes-mail-notifier/data.db`
- cron 실행 진입점: `scripts/hermes-mail-notifier.py`

전체 메일 원문은 저장하지 않습니다. 전송 대기 요약은 재시도에 재사용하며, 완료 이력은 30일 후 정리합니다. 설정·비밀번호·DB는 설치 코드 밖에 있어 업데이트로 덮어쓰지 않습니다.

```sh
hermes plugins update hermes-mail-notifier
```

Hermes 실행 파일의 경로를 바꾼 경우 `setup`을 다시 실행해 cron 진입점을 갱신하세요.

중지·삭제는 cron을 먼저 정리합니다.

```sh
hermes cron pause JOB_ID
hermes cron remove JOB_ID
hermes plugins disable hermes-mail-notifier
hermes plugins remove hermes-mail-notifier
```

필요 없다면 해당 프로필의 플러그인 데이터·실행 스크립트·계정별 비밀번호도 직접 정리할 수 있습니다.

## 서버에서 실제 연결 확인

1. `setup`을 마치고 Telegram의 설정 완료 메시지를 확인합니다.
2. 테스트 발신자를 중요 발신자에 추가하고 **설정 이후** 새 메일을 보냅니다.
3. `hermes mail-notifier check`로 요약 수신을 확인합니다.
4. 다시 `check`했을 때 중복 전송되지 않는지 확인합니다.
5. gateway와 cron이 활성화된 상태에서 다음 새 메일이 자동으로 전달되는지 확인합니다.

자동 테스트는 가짜 메일·모델·전송을 사용합니다. 실제 계정 로그인과 Telegram 수신 여부는 이 서버 절차로 확인해야 합니다.

## 개발·검증

기본 테스트는 외부 서비스·자격증명·추가 패키지가 필요 없습니다.

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q __init__.py mail_notifier tests
```

Hermes가 설치된 Python 환경에서는 실제 호스트 계약도 확인할 수 있습니다. 테스트는 임시 프로필을 사용하고 외부 메일·모델·Telegram에 접속하지 않습니다.

```sh
hermes plugins doctor . --ci
HERMES_CONTRACT=1 python -m unittest tests.test_hermes_contract -v
```

CI는 Ubuntu의 Python 3.11과 3.14에서 기본 테스트·구문 검사를 실행합니다.

## 라이선스

이 프로젝트는 [MIT 라이선스](LICENSE)로 배포됩니다.
