# Hermes Mail Notifier

[![Tests](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 한국어

네이버·다음·Gmail의 **중요한 새 메일을 요약해 기존 Telegram으로 보내는 Hermes Agent 플러그인**입니다. 여러 계정을 연결할 수 있고, Hermes에 설정된 모델과 Telegram 연결을 그대로 사용합니다.

## 설치

Hermes가 실행 중인 서버에서 아래 명령으로 설치하세요.

```sh
hermes plugins install jeonjw85/hermes-mail-notifier --enable
```

최초 설정:

```sh
hermes mail-notifier setup --ui-language ko
```

설치와 설정은 **Telegram을 사용 중인 동일한 Hermes 프로필**에서 실행하세요. 앱 비밀번호는 서버의 대화형 터미널에서 입력하며 화면에 표시되지 않습니다.

필요한 환경:
- Linux 또는 macOS, Python 3.11 이상. 실행 시에는 설치된 Hermes의 Python을 사용합니다.
- native plugins, plugin CLI, `ctx.llm.complete_structured`, profile-scoped settings/storage, script-only cron, `hermes send --json`을 지원하는 Hermes.
- 현재 프로필의 모델과 Telegram 연결. 자동 확인에는 해당 프로필의 **Hermes gateway가 실행 중**이어야 합니다.

예약 실행은 새 프로세스에서 인증을 다시 읽습니다. 모델·Telegram 인증은 같은 프로필의 Hermes 설정 (`.env`, 인증 파일, 재로드 가능한 비밀정보 소스)에 저장되어 있어야 합니다. **셸/systemd 환경변수에만 있는 인증 값**은 `setup`의 사전 검사에서 감지해 예약 작업을 활성화하기 전에 안내합니다. 해당 프로필의 Hermes 설정으로 인증을 저장한 뒤 다시 실행하세요.

Hermes 소스 `d5aa9d7adf308ca466a118206c201254cf3ca977`에서 실제 호스트 계약을 검증했습니다. 플러그인의 추가 Python 런타임 의존성은 없습니다.

## 검증과 출시 상태

고정 Hermes 환경에서 중단 복구 검사를 포함한 183개 테스트가 통과했습니다. 50계정·10,000건 시뮬레이션도 누락·중복 없이 완료했습니다. 실제 모델 정확도와 서버의 메일 전달 검증은 아직 남아 있습니다.

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -s tests
python -m ruff check .
python -m pyright
python -m tests.load_scenario --output load-report.json
# 모델이 설정된 Hermes 서버에서 실행 (최대 360회 실제 모델 호출):
hermes mail-notifier evaluate --runs 3 --output mail-evaluation.json
```

[120개 한영 평가 사례](evaluation/README.md)는 직접 작성한 가상 메일입니다. 단위 시험은 점수 계산을 검증하며 실제 모델 정확도를 뜻하지 않습니다. 실제 AI 평가와 메일 서비스·Telegram 수신 검증은 서버에서 수행해야 합니다.

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

1. 새 설치에서는 작업 메뉴 없이 서비스와 로그인 주소부터 입력합니다.
2. 별칭은 주소, 메일함은 INBOX를 제안합니다. 선택적으로 고급 계정 항목을 수정한 뒤 **앱 비밀번호**를 비표시 터미널 입력으로 전달합니다.
3. 연결 결과와 미리보기를 확인합니다. 목적지·주기·언어·판단 기준 등의 전체 고급 설정은 선택 사항입니다.
4. `yes`로 발행을 명시적으로 확인합니다. 합성 메일 모델 검사, 새 IMAP 시작점 확인, 인증·설정 저장, 소유 예약 작업 갱신, Telegram 테스트 메시지 ID 확인을 수행합니다.
5. 이후 관리에서는 `add`·`edit`·`disable`·`enable`과 `done`을 사용합니다. 같은 서비스의 복수 계정도 가능하며 변경 사항은 확인 후 발행합니다.

새 설치 기본값은 **5분마다**, **선택·감지한 UI 언어로 요약**, **INBOX**, **현재 프로필의 Telegram home channel**입니다. 기존 주기·목적지·요약 언어 (`fr` 등 포함)·판단 기준·운영 한도는 보존합니다. 목적지는 `telegram`, `telegram:123456789`, `telegram:-1001234567890:456`처럼 지정할 수 있습니다. 마지막 숫자는 토픽 ID입니다.

- `edit`: 별칭·비밀번호·연결 정보 수정. 계정 ID 또는 표시 번호를 입력합니다.
- `disable` / `enable`: 계정 확인 중지·재개. 대기 중인 알림은 보존합니다.
- Enter: 현재 값 유지. 발신자·키워드 목록과 추가 판단 기준에서 `-`: 비우기.
- 입력 중 Ctrl-C: 취소. 저장 이후 외부 단계에서 문제가 나면 완료된 단계와 오류 코드를 표시하므로 같은 `setup`을 재실행해 복구할 수 있습니다.
- 잘못된 항목은 그 자리에서 다시 입력합니다. 연결 실패 시 `retry`·`password`·`back`·`cancel`을 선택하며, 앞서 입력한 다른 계정은 저장할 때까지 유지합니다.
- 비밀정보 없는 초안은 별도 `setup.db`에 마지막 변경 후 최대 24시간 보존합니다. 검사된 비밀번호는 메모리에만 최대 10분 보관하므로 만료·재시작 후 재입력합니다. 불명확한 인증 쓰기를 비밀번호 교체 성공으로 간주하지 않습니다. cron·테스트 전송만 복구할 때는 이미 검증·저장한 비밀번호나 시작점을 다시 만들지 않습니다.
- 다른 세션의 설정 변경은 덮어쓰지 않고 `needs_review`로 표시합니다. `already_running`은 입력을 보존하므로 재시도할 수 있습니다. 발행 시작 후에는 외부 저장을 취소·롤백했다고 주장하지 않고 완료 단계를 표시합니다. 테스트 전송 응답 유실 시 같은 시도 ID의 테스트 메시지가 중복될 수 있습니다.

**처음 연결한 시점 이후 도착하는 메일부터** 확인합니다. 과거 메일은 한꺼번에 전송하지 않습니다. 읽은 새 메일도 처리하며, 별칭·비밀번호·판단 기준을 바꿔도 시작점을 초기화하지 않습니다. 서비스·로그인 아이디·메일함을 바꾸면 이전 연결을 중지하고 새 연결을 만듭니다.

기존 cron의 일시정지 상태는 재설정해도 유지하며 자동 확인 완료 대신 `scheduled` / `resume_cron`으로 표시합니다. `setup`을 재실행해 명시적으로 재개를 승인하거나 `hermes cron resume JOB_ID`를 사용하세요. 모든 계정을 중지하면 cron도 일시정지됩니다.

### 언어

기존 `language` 설정은 모델 요약과 알림의 고정 라벨·대체 사유·설정 완료·진단 테스트 알림 언어를 결정합니다. `setup`에서 `ko` 또는 `en`을 입력하거나 `hermes mail-notifier policy set --language en`을 사용하세요. 기존의 다른 요약 언어 값도 유지하며 모델에 그대로 전달합니다. 해당 언어의 고정 문구는 영어로 대체합니다. 이미 준비된 알림의 문구와 목적지는 바꾸지 않습니다.

별도 `ui_language`는 기존 요약 `language`를 변경하지 않고 한영 설정·상태·진단 안내를 선택합니다. `setup --ui-language ko|en` 또는 `accounts add/edit --ui-language ko|en`을 사용하세요. 우선순위는 명시적 선택 → 저장된 UI 언어 → CLI `LC_ALL`·`LC_MESSAGES`·`LANG` 중 첫 비어 있지 않은 값 → 영어입니다. `ko-KR`·`en_US.UTF-8` 같은 지역 locale을 정규화하며 미지원 언어는 영어로 안내합니다. CLI 도움말은 한영을 함께 표시합니다. Telegram 설정·Mini App UI는 아직 제공하지 않으므로 안전한 터미널 경로를 사용하세요.

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

기본 `doctor`는 로컬 설정·인증 재로드·cron·백로그를 점검합니다. `--live`는 활성 메일함 로그인, 합성 메일의 모델 판단, Telegram 진단 메시지 전송도 실행합니다. 사용자 메일을 가져오지는 않습니다. 오류 코드와 다음 조치를 함께 표시합니다.

`history`는 저장된 내용을 명시적으로 조회합니다. `status`·`queue`에는 본문·요약이 없습니다. 큐 변경에는 메일 ID 또는 명시적 계정 필터가 필요합니다. 전송 대기 재시도는 저장된 요약을 재사용합니다. 기본 Telegram 채널도 준비 시 실제 채팅·토픽 ID로 저장하며, 기존 대기 알림의 목적지를 바꾸려면 `retarget`을 사용합니다. 계정 중지·재개는 UID 기준점과 대기 작업을 보존합니다.

교정은 원래 판단을 유지하며 완료 메일을 다시 보내지 않습니다. 최근 200건을 최대 180일 보존하고 동일 발신자 사례 최대 5개를 이후 AI 판단에 제공합니다. 본문은 판단 완료 또는 7일 후 제거하고 완료 판단 이력은 30일 보존합니다. `status --json`에서 한도·백로그·수집 재시도 상태·사용량도 확인할 수 있으며, 제공되지 않은 비용은 알 수 없음으로 남깁니다.

Hermes 대화 도구: `mail_notifier_check`, `mail_notifier_status`, `mail_notifier_history`, `mail_notifier_queue`, `mail_notifier_feedback`, `mail_notifier_policy`, `mail_notifier_doctor`. 앱 비밀번호는 터미널 설정에서 입력하세요.

## 중요도 기준

우선순위는 다음과 같습니다.

1. **제외 발신자**: 모델 호출 없이 제외합니다.
2. **명시적 필수 알림 발신자** (`always_notify_senders`): 사용자가 지정한 필수 알림으로 처리하고 AI로 요약합니다.
3. **일반 메일**: Hermes AI가 업무·개인 연락의 의미, 수신자 관련성, 읽거나 대응할 필요성을 판단합니다. 중요 발신자·키워드와 추가 자연어 기준은 판단 참고 정보입니다.

발신자는 `boss@example.com`처럼 정확한 주소 또는 `@example.com`처럼 정확한 도메인을 사용합니다. 대소문자를 구분하지 않으며, 도메인 규칙은 하위 도메인까지 자동 확장하지 않습니다. 키워드가 있다는 이유만으로 AI의 제외 판단을 덮어쓰지 않습니다.

기본적으로 업무 요청·개인 연락, 기한·일정·계약 변경, 결제 실패, 중요한 계정 보안 변화를 우선합니다. 광고·홍보·뉴스레터·일상적인 영수증은 기본 제외합니다. 자동 발송된 예약 취소도 대응이 필요하면 알립니다. 예를 들어 추가 기준에 `면접 일정 변경과 계약 마감일을 우선해서 알려줘`를 입력할 수 있습니다.

알림에는 계정, 발신자, 제목, 판단 이유, 우선순위, 요약, 할 일, 기한, 메일 ID를 표시합니다. AI 결과는 업무·개인·거래·홍보·뉴스레터·판단 불충분으로 분류하고 `notify`·`suppress`·`review`로 처리합니다. 명시된 행동·기한이 없으면 한국어에서는 `명시 없음`, 영어에서는 `Not specified`를 표시합니다. 본문이 불완전하면 `본문 확인 필요` 또는 `Check the email body`를 표시하며, 불완전한 본문을 AI가 제외하려 하면 원문 확인 알림으로 전환합니다. 기계용 코드·도구 필드·CLI 명령은 번역하지 않습니다.

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

메일 서버의 UIDVALIDITY가 바뀌면 해당 계정의 새 수집을 `uidvalidity_changed`로 보류합니다. 이미 저장된 본문의 판단과 준비된 알림 전송은 계속할 수 있습니다. 서버 상태를 확인한 뒤 새 시작점으로 전환합니다.

```sh
hermes mail-notifier reset ACCOUNT_ID
```

`reset`은 **선택 계정의 미완료 알림을 종료**하고 현재 메일함 이후 도착하는 메일부터 다시 시작합니다. 영향을 받은 건수와 새 기준점을 출력합니다.

Hermes 대화에서도 `mail_notifier_check`, `mail_notifier_status` 도구를 사용할 수 있습니다. 설치 후 기존 gateway가 새 도구를 아직 인식하지 않으면 `hermes gateway restart`로 다시 불러옵니다.

### 처리 방식과 재시도

- 메일함은 읽기 전용으로 열고 `BODY.PEEK`를 사용합니다. 메일 읽음 상태를 변경하지 않습니다.
- 첨부파일 내용은 처리하지 않습니다. 본문 일부가 현재 Hermes 모델로 전달되어 중요도·요약을 생성합니다.
- 메일당 원문 256 KiB, 모델 입력 본문 12,000자입니다. 실행당 fetch·모델 호출·전송이 각각 기본 최대 20건이며, 단계·계정별로 번갈아 처리합니다.
- 실행 후 240초부터 새 작업을 시작하지 않습니다. IMAP 20초, 모델 45초, 전송 30초의 호출 제한을 둡니다.
- 새 메일과 재시도 작업이 없으면 모델·Telegram을 호출하지 않습니다. 모델 호출 비용은 기존 Hermes 모델 설정을 따릅니다.
- 전송 실패 시 요약을 보관하고 5분 → 10분 → 20분 → 40분 → 최대 1시간 간격으로 재시도합니다. 유효한 성공 응답을 확인한 뒤에만 완료 처리합니다.
- 수집·AI·전송의 재시도 상태를 별도로 저장합니다. IMAP 장애 중에도 저장된 본문을 판단하고 준비된 알림을 전송합니다. 모델 재시도 때 메일을 다시 가져오지 않습니다.
- 잘못된 AI 응답이 5회 누적되면 원문 확인 알림을 만듭니다. 전송이 10회 실패하면 `held`로 보존하며 `status`에 보류 건수를 표시합니다.
- 기본 미완료 큐는 2,000건, 본문 캐시는 200건입니다. 한도가 차면 수집을 늦추고, 저장하지 못한 메일의 UID를 건너뛰지 않습니다.
- 목적지 변경은 새 알림부터 적용합니다. 이미 준비된 알림은 저장 당시 목적지를 유지합니다.
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
| `invalid_model_output` | 모델 응답이 규격에 맞지 않아 재시도. 5회 누적되면 원문 확인 알림 |
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

전체 원문·첨부파일은 저장하지 않습니다. 판단 대기 중인 정규화 본문은 최대 12,000자로 임시 저장하고 판단 완료 시 제거합니다. 7일이 지나면 캐시를 비우고 필요할 때 다시 수집합니다. 판단 이유·근거·요약과 완료 이력은 30일 보존합니다. 미완료 알림은 재시도에 재사용합니다. 기본 상태 출력에는 메일 본문·요약이 포함되지 않습니다. 설정·비밀번호·DB는 설치 코드 밖에 있습니다.

기존 DB는 최초 실행 시 프로필 잠금 아래 v3로 이전하며 이전 스키마에 맞는 `data.db.v1.bak` 또는 `data.db.v2.bak` 백업을 만듭니다. 계정 ID·UID 기준점·대기 알림·cron ID를 보존합니다. **기존 중요 발신자·키워드는 강제 중요 처리에서 AI 판단 참고로 바뀝니다.** 명시적 필수 알림은 설정의 `always_notify_senders`로 구분합니다. `status`에서 DB·정책 버전, 단계별 대기·보류 수, 마지막 실행을 확인할 수 있습니다.

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
2. **설정 이후** 회신이 필요한 업무·개인 테스트 메일을 보냅니다.
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
