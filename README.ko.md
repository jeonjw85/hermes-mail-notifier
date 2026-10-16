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

최초 설정

```sh
hermes mail-notifier setup
```

설치와 설정은 **Telegram을 사용 중인 동일한 Hermes 프로필**에서 진행. 앱 비밀번호는 서버의 대화형 터미널에서 입력하며 화면에는 표시되지 않음

필요한 환경
- Linux 또는 macOS, Python 3.11 이상 (실행 시 설치된 Hermes의 Python 사용)
- native plugins, plugin CLI, `ctx.llm.complete_structured`, profile-scoped settings/storage, script-only cron, `hermes send --json`을 지원하는 Hermes
- 현재 프로필의 모델과 Telegram 연결, 자동 확인을 위한 해당 프로필의 Hermes gateway 실행

예약 작업은 새 프로세스에서 인증을 다시 읽으므로, 모델과 Telegram 인증은 같은 프로필의 Hermes 설정 (`.env`, 인증 파일, 재로드 가능한 비밀정보 소스)에 저장해야 합니다

셸/systemd 환경변수에만 있는 인증 값은 `setup` 사전 검사에서 감지해 예약 작업 활성화 전에 안내. 해당 프로필의 Hermes 설정에 인증을 저장한 뒤 재실행

Hermes 소스 `d5aa9d7adf308ca466a118206c201254cf3ca977`에서 실제 호스트 계약 검증 완료, 추가 Python 런타임 의존성 없음

## 검증과 출시 상태

- 고정 Hermes 환경에서 중단 복구 검사를 포함한 183개 테스트 통과
- 50계정, 10,000건 시뮬레이션을 누락과 중복 없이 완료
- 실제 모델 정확도와 서버의 메일 전달은 아직 미검증

```sh
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

## 설정 마법사

`setup`에서 계정과 알림 기준을 함께 관리

1. `add` 입력 후 서비스, 별칭, 로그인 아이디, 메일함, 앱 비밀번호 입력
2. 연결 검사 통과 후 필요한 계정 추가 (같은 서비스의 복수 계정도 가능)
3. `done`으로 계정 편집 종료
4. Telegram 목적지, 확인 주기, 요약 언어, 중요 발신자와 제외 발신자, 키워드, 추가 판단 기준 입력
5. 설정과 예약 작업 저장 후 Telegram으로 설정 완료 메시지 전송

기본값: 5분마다 확인, 한국어 요약, INBOX, 현재 프로필의 Telegram home channel

목적지는 `telegram`, `telegram:123456789`, `telegram:-1001234567890:456`처럼 지정하며, 마지막 숫자는 토픽 ID

- `edit`: 계정 ID 또는 표시 번호로 별칭, 비밀번호, 연결 정보 수정
- `disable` / `enable`: 계정 확인 중지와 재개 (대기 중인 알림은 보존)
- Enter: 현재 값 유지, 발신자와 키워드 목록 및 추가 판단 기준에서 `-` 입력 시 비우기
- 입력 중 Ctrl-C: 취소. 저장 이후 외부 단계에서 문제가 나면 완료된 단계와 오류 코드를 표시하며, 같은 `setup`을 재실행해 복구 가능
- 잘못된 항목은 그 자리에서 다시 입력. 연결 실패 시 `retry`, `password`, `back`, `cancel` 중 선택하며, 앞서 입력한 다른 계정은 저장할 때까지 유지

**처음 연결한 시점 이후 도착하는 메일부터** 확인하며, 과거 메일은 한꺼번에 전송하지 않습니다. 읽은 새 메일도 처리 대상

별칭, 비밀번호, 판단 기준을 바꿔도 시작점은 유지. 서비스, 로그인 아이디, 메일함을 바꾸면 이전 연결을 중지하고 새 연결 생성

기존 cron의 일시정지 상태는 재설정해도 유지. 모든 계정을 중지하면 cron도 일시정지되므로, 계정을 다시 켠 뒤 아래 `hermes cron resume JOB_ID`로 재개

## 운영과 판단 교정

```sh
hermes mail-notifier accounts list --json
hermes mail-notifier accounts add
hermes mail-notifier accounts edit ACCOUNT_ID
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

Hermes 대화 도구: `mail_notifier_check`, `mail_notifier_status`, `mail_notifier_history`, `mail_notifier_queue`, `mail_notifier_feedback`, `mail_notifier_policy`, `mail_notifier_doctor`

앱 비밀번호는 터미널 설정에서 입력

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
- 명시된 행동이나 기한이 없으면 `명시 없음`, 본문이 불완전하면 `본문 확인 필요` 표시
- 불완전한 본문을 AI가 제외하려 하면 원문 확인 알림으로 전환

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
- 앱 비밀번호: Hermes 자격증명 저장 경로의 `HERMES_MAIL_<ACCOUNT_ID>_APP_PASSWORD`
- UID 기준점, 대기 알림, 처리 이력: `plugin-data/hermes-mail-notifier/data.db`
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

기본 테스트는 외부 서비스, 자격증명, 추가 패키지 없이 실행 가능

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q __init__.py mail_notifier tests
```

Hermes가 설치된 Python 환경에서는 실제 호스트 계약도 확인 가능. 테스트는 임시 프로필을 사용하며 외부 메일, 모델, Telegram에 접속하지 않음

```sh
hermes plugins doctor . --ci
HERMES_CONTRACT=1 python -m unittest tests.test_hermes_contract -v
```

CI는 Ubuntu의 Python 3.11과 3.14에서 기본 테스트와 구문 검사 실행

## 라이선스

[MIT 라이선스](LICENSE)로 배포
