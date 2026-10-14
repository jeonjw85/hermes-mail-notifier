# Hermes Mail Notifier

[![Tests](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [한국어](README.ko.md)

A Hermes Agent plugin that **summarizes important new emails from Naver Mail, Daum Mail, and Gmail and sends them through your existing Telegram connection**. It supports multiple accounts and reuses the model and Telegram connection configured in Hermes.

## Installation

Publish this project at the root of a GitHub repository, then install it on the server running Hermes. Replace `OWNER/REPOSITORY` with your repository.

```sh
hermes plugins install OWNER/REPOSITORY --enable
```

Run the initial setup:

```sh
hermes mail-notifier setup
```

Run installation and setup in **the same Hermes profile that already uses Telegram**. Enter app passwords in an interactive terminal on the server; password input is hidden.

Requirements:
- Linux or macOS with Python 3.11 or later. The plugin runs with the Python interpreter used by your Hermes installation.
- Hermes with native plugins, plugin CLI commands, `ctx.llm.complete_structured`, profile-scoped settings/storage, script-only cron, and `hermes send --json`.
- A configured model and Telegram connection in the current profile. Automatic checks require that profile's **Hermes gateway to be running**.

Scheduled runs reload credentials in a new process. Store model and Telegram credentials in the same profile's Hermes configuration (`.env`, authentication files, or a reloadable secret source). The `setup` preflight check detects **credentials available only through shell/systemd environment variables** and reports them before enabling the scheduled job. Save those credentials in the profile's Hermes configuration, then run setup again.

Host integration contracts have been verified against Hermes source revision `d5aa9d7adf308ca466a118206c201254cf3ca977`. The plugin has no additional Python runtime dependencies.

## Verification and Release Status

Local verification: 183 tests passed with pinned Hermes, including process recovery checks. A 50-account, 10,000-message simulation completed without lost or duplicate deliveries. Real-model accuracy and live-server delivery checks remain pending.

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -s tests
python -m ruff check .
python -m pyright
python -m tests.load_scenario --output load-report.json
# On the server with a configured Hermes model (up to 360 real model calls):
hermes mail-notifier evaluate --runs 3 --output mail-evaluation.json
```

The [120-case bilingual evaluation set](evaluation/README.md) contains authored fictional mail. Unit tests verify scoring, not real model accuracy. Actual AI evaluation and provider/Telegram receipt verification remain pending on your server.

## Prepare Your Email Accounts

| Service | IMAP server (TLS, port 993) | Requirements |
| --- | --- | --- |
| Naver Mail | `imap.naver.com` | Enable IMAP, enable two-step verification, and create an app password |
| Daum / Hanmail | `imap.daum.net` | Enable IMAP, enable two-step verification for your Kakao Account, and create an app password |
| Gmail | `imap.gmail.com` | Enable Google two-step verification and use an account eligible for app passwords |

Official guides:
- Naver: [IMAP settings](https://help.naver.com/service/30029/contents/21344), [app password policy](https://notice.naver.com/notices/mail/22462)
- Daum: [IMAP server](https://cs.daum.net/faq/service/43/category/9234/detail/17988), [app passwords](https://cs.daum.net/faq/service/43/category/9234/detail/31143)
- Google: [Create an app password](https://support.google.com/accounts/answer/185833?hl=en)

Gmail support requires an account that can create app passwords. Accounts blocked from doing so by organization policies, Advanced Protection, or similar restrictions are currently unsupported. OAuth authentication is not supported.

## Setup Wizard

Use `setup` to manage accounts and notification rules together.

1. Enter `add`, then provide the service, account label, login ID, mailbox, and app password.
2. Once the connection check passes, add any other accounts. Multiple accounts from the same service are supported.
3. Enter `done` to finish editing accounts.
4. Set the Telegram destination, polling interval, summary language, important and excluded senders, keywords, and additional importance criteria.
5. Save the settings and scheduled job. A setup confirmation message is sent to Telegram.

Defaults are **every 5 minutes**, **Korean summaries**, **INBOX**, and **the current profile's Telegram home channel**. Destination examples include `telegram`, `telegram:123456789`, and `telegram:-1001234567890:456`. In the last example, the final number is the topic ID.

- `edit`: Change an account's label, password, or connection details. Enter its account ID or displayed number.
- `disable` / `enable`: Stop or resume checks for an account. Pending notifications are preserved.
- Enter: Keep the current value. Use `-` to clear sender lists, keyword lists, or additional criteria.
- Ctrl-C during input: Cancel. If an external step fails after settings have been saved, setup reports the completed steps and an error code. Run `setup` again to recover.
- Invalid fields can be corrected in place. Connection failures offer `retry`, `password`, `back`, or `cancel`; other account drafts are preserved until you save.

Only **mail arriving after an account is first connected** is checked; existing mail is not sent in bulk. New mail is processed even if it has already been read. Changing a label, password, or importance criteria does not reset the starting point. Changing the service, login ID, or mailbox disables the old connection and creates a new one.

Re-running setup preserves an existing cron job's paused state. Disabling all accounts also pauses the cron job. After enabling an account again, resume the job with `hermes cron resume JOB_ID`, as shown below.

## Operations and Corrections

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
hermes mail-notifier feedback add MAIL_ID --decision notify --note "Personal request worth reading"
hermes mail-notifier feedback list
hermes mail-notifier feedback remove FEEDBACK_ID
hermes mail-notifier queue list --json
hermes mail-notifier queue retry MAIL_ID
hermes mail-notifier queue retry --account ACCOUNT_ID
hermes mail-notifier queue retarget MAIL_ID --to telegram:123456789
hermes mail-notifier policy show
hermes mail-notifier policy set --instructions "Prioritize personal invitations and work requests"
hermes mail-notifier policy set --always-notify-senders boss@example.com
hermes mail-notifier policy set --clear always_notify_senders
```

Default `doctor` checks local configuration, credential reload, cron and backlog. `--live` additionally logs into enabled mailboxes, classifies a synthetic message and sends a Telegram diagnostic message. It does not fetch user mail. Diagnostic failures include an error code and a suggested next action.

`history` explicitly displays saved content; `status` and `queue` omit bodies and summaries. Queue mutations require a mail ID or an explicit account filter. Retrying a prepared notification reuses its summary. Home-channel destinations are resolved to concrete chat/topic IDs when prepared; use `retarget` to change pending notifications. Account enable/disable preserves UID checkpoints and pending work.

Corrections keep the original judgment intact and never resend completed mail. The latest 200 corrections are retained for at most 180 days; up to five matching-sender examples guide future AI judgments. Bodies expire after seven days or classification; completed decisions expire after 30 days. `status --json` includes limits, backpressure, source retry state and available usage. Missing cost data stays unknown.

Hermes conversation tools: `mail_notifier_check`, `mail_notifier_status`, `mail_notifier_history`, `mail_notifier_queue`, `mail_notifier_feedback`, `mail_notifier_policy`, `mail_notifier_doctor`. Enter app passwords only through terminal setup.

## Importance Rules

Rules are applied in this order:

1. **Excluded senders**: Skip the message without calling the model.
2. **Explicit always-notify senders** (`always_notify_senders`): Honor the user's explicit notification instruction and summarize with AI.
3. **Other mail**: Hermes AI judges the purpose, relevance, and need to read or act. Important senders, keywords, and natural-language criteria provide context rather than overriding its judgment.

Sender rules accept exact addresses such as `boss@example.com` or exact domains such as `@example.com`. Matching is case-insensitive, and domain rules do not automatically include subdomains. A keyword alone never forces an advertisement to be important.

Default criteria prioritize meaningful work and personal correspondence, deadlines, schedule/contract changes, payment failures, and significant account security changes. Promotions, newsletters, and routine receipts are suppressed by default. Automated reservation cancellations can still need attention. For example, add `Prioritize interview schedule changes and contract deadlines` as an additional criterion.

Notifications include the account, sender, subject, reason, priority, summary, action, deadline, and stable mail ID. Categories are work, personal, transactional, promotion, newsletter, and unknown; decisions are notify, suppress, and review. Missing actions/deadlines show `명시 없음` ("Not specified"). Incomplete bodies show `본문 확인 필요` ("Check the email body"); an attempted suppression of an incomplete message becomes a review notification.

## Commands

```sh
hermes mail-notifier check
hermes mail-notifier status
hermes mail-notifier status --json
hermes mail-notifier setup
```

`check` uses the same execution path as automatic checks. It exits with code 0 on success or 1 for account, model, delivery, or other errors. If a check is already running, an additional invocation only returns status.

The scheduled job ID appears in `status`.

```sh
hermes cron status
hermes cron pause JOB_ID
hermes cron resume JOB_ID
```

If UIDVALIDITY changes, new collection pauses with `uidvalidity_changed`. Already cached bodies and prepared notifications can still be processed independently. Check the server's state, then establish a new starting point:

```sh
hermes mail-notifier reset ACCOUNT_ID
```

`reset` **closes unfinished notifications for the selected account** and restarts from mail arriving after the current mailbox checkpoint. It reports the number of affected notifications and the new checkpoint.

The `mail_notifier_check` and `mail_notifier_status` tools are also available in Hermes conversations. If the running gateway has not picked up the tools after installation, reload it with `hermes gateway restart`.

### Processing and Retries

- Mailboxes are opened read-only, and messages are fetched with `BODY.PEEK`. Read/unread state is not changed.
- Attachment contents are not processed. A portion of the message body is sent to the current Hermes model for importance assessment and summarization.
- Limits are 256 KiB of raw data per email and 12,000 body characters. Each run permits up to 20 fetches, 20 model calls, and 20 deliveries by default. Work rotates between stages and accounts.
- No new work starts after 240 seconds into a run. Call timeouts are 20 seconds for IMAP, 45 seconds for the model, and 30 seconds for delivery.
- The model and Telegram are not called when there is no new mail or pending retry work. Model usage costs follow your existing Hermes model configuration.
- After delivery fails, the summary is retained and retried after 5, 10, 20, and 40 minutes, then at intervals of up to 1 hour. Notifications are marked complete only after a valid success response is verified.
- Collection, classification, and delivery have separate retry state. Cached bodies and prepared notifications remain usable during IMAP outages; model retries do not fetch the message again.
- Five malformed model responses produce a review notification. Ten delivery failures place the item on hold; `status` reports held work.
- The default limits are 2,000 unfinished jobs and 200 cached bodies. Full queues apply backpressure without advancing past mail that has not been stored.
- A destination change applies to newly prepared notifications. Existing prepared notifications retain their saved destination.
- Repeated checks do not resend the same email. However, a retry can cause a duplicate if Telegram received the message but its success response was lost, or if the process stopped just before recording completion locally.

### Common Errors

| Error | What to check |
| --- | --- |
| `imap_connection_or_login_failed` | IMAP settings, the service's login ID format, app password, and connectivity from the server to port 993 |
| `credential_unavailable` | Re-enter the password with `setup` in the same profile |
| `credential_save_failed` | The profile's credential-storage permissions and management policy |
| `credentials_not_reloadable` | Move environment-only model/Telegram credentials into the profile's Hermes configuration or a reloadable secret source |
| `credential_reload_check_failed` | Verify that the Hermes credential loader and secret source work in a fresh process |
| `model_unavailable` | The current profile's model configuration, credentials, and connectivity |
| `invalid_model_output` | Invalid model output is retried; five failures produce a review notification |
| `delivery_failed` / `delivery_unverified` | Telegram connection, destination, and Hermes delivery result |
| `cron_operation_failed` | The polling interval in `setup` and the output of `hermes cron status` |
| `uidvalidity_changed` | Inspect the mailbox, then `reset` only the affected account |
| `duplicate_owned_cron_jobs` | Remove duplicate plugin jobs shown by `hermes cron list`, then rerun `setup` |
| `incompatible_hermes` | Update to a Hermes version supporting the required plugin features listed above |

## Storage, Updates, and Removal

Paths are relative to the current profile's `HERMES_HOME`:

- General settings: `plugins.entries.hermes-mail-notifier.settings.configuration` in `config.yaml` (accounts and destination saved as a single snapshot)
- App passwords: `HERMES_MAIL_<ACCOUNT_ID>_APP_PASSWORD` in Hermes credential storage
- UID checkpoints, pending notifications, and processing history: `plugin-data/hermes-mail-notifier/data.db`
- Cron entry point: `scripts/hermes-mail-notifier.py`

Full raw emails and attachments are not stored. Up to 12,000 normalized body characters are cached until classification; after seven days the cache expires and the body can be fetched again. Reasons, evidence, summaries, and completed history are retained for 30 days. Unfinished notifications are retained for retries. Default status output excludes mail bodies and summaries. Settings, passwords, and the database live outside the installed plugin code.

On first use, older databases migrate under the profile lock to schema v3, with a `data.db.v1.bak` or `data.db.v2.bak` backup matching the previous schema. Account IDs, UID checkpoints, pending notifications, and the cron ID survive. **Existing important-sender/keyword rules become AI context rather than forced decisions.** Use the explicit `always_notify_senders` setting for mandatory notifications. `status` shows schema/policy versions, per-stage queues, held work, and the last run.

```sh
hermes plugins update hermes-mail-notifier
```

If the Hermes executable path changes, rerun `setup` to update the cron entry point.

To stop and remove the plugin, remove its cron job first:

```sh
hermes cron pause JOB_ID
hermes cron remove JOB_ID
hermes plugins disable hermes-mail-notifier
hermes plugins remove hermes-mail-notifier
```

You can also manually remove the profile's plugin data, execution script, and account passwords if they are no longer needed.

## Verify Live Connections on Your Server

1. Complete `setup` and confirm that the setup message arrives in Telegram.
2. Send a meaningful work or personal test email requiring a reply **after setup**.
3. Run `hermes mail-notifier check` and confirm that you receive the summary.
4. Run `check` again and confirm that the notification is not duplicated.
5. With the gateway and cron active, send another new email and confirm that its notification arrives automatically.

Automated tests use fake email, model, and delivery services. Use the server-side steps above to verify real account authentication and Telegram delivery.

## Development and Verification

The default tests require no external services, credentials, or additional packages.

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q __init__.py mail_notifier tests
```

In a Python environment with Hermes installed, you can also verify the real host integration contracts. These tests use temporary profiles and do not connect to external email, model, or Telegram services.

```sh
hermes plugins doctor . --ci
HERMES_CONTRACT=1 python -m unittest tests.test_hermes_contract -v
```

CI runs the default tests and syntax checks on Ubuntu with Python 3.11 and 3.14.

## License

This project is licensed under the [MIT License](LICENSE).
