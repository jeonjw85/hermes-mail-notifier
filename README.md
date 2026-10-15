# Hermes Mail Notifier

[![Tests](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jeonjw85/hermes-mail-notifier/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [한국어](README.ko.md)

A Hermes Agent plugin that **summarizes important new emails from Naver Mail, Daum Mail, and Gmail and sends them through your existing Telegram connection**. It supports multiple accounts and reuses the model and Telegram connection configured in Hermes.

## Installation

Install the plugin on the server running Hermes:

```sh
hermes plugins install jeonjw85/hermes-mail-notifier --enable
```

Open a private chat with the profile's existing Telegram bot and send **`/mail`**. Choose **한국어 / English**, then the mail service. Non-secret management works without a credential page. **No public credential-page URL is configured in this version**: new connections and password replacement require the operator to configure a trusted HTTPS `credential_page_url` first. Never send passwords in chat.

Optional secure terminal setup:

```sh
hermes mail-notifier setup --ui-language en
```

Run installation and setup in **the same Hermes profile that already uses Telegram**. The terminal alternative uses hidden password input; Telegram uses only the encrypted Mini App opened by the bot's secure reply-keyboard button.

Requirements:
- Linux or macOS with Python 3.11 or later. The plugin runs with the Python interpreter used by your Hermes installation.
- Hermes with native plugins, plugin CLI commands, `ctx.llm.complete_structured`, profile-scoped settings/storage, script-only cron, and `hermes send --json`.
- A configured model and Telegram connection in the current profile. Automatic checks require that profile's **Hermes gateway to be running**.

Scheduled runs reload credentials in a new process. Store model and Telegram credentials in the same profile's Hermes configuration (`.env`, authentication files, or a reloadable secret source). The `setup` preflight check detects **credentials available only through shell/systemd environment variables** and reports them before enabling the scheduled job. Save those credentials in the profile's Hermes configuration, then run setup again.

Host integration contracts have been verified against Hermes source revision `d5aa9d7adf308ca466a118206c201254cf3ca977`. The plugin declares `cryptography>=50.0.1,<51` as a Python runtime dependency for encrypted credential input. Ensure it is installed in the same Python environment as Hermes (for a source checkout, `python -m pip install -e .`). Telegram SDK modules are loaded lazily from Hermes; they are not a separate plugin dependency. If cryptography is missing, encrypted input fails closed before a credential is requested; existing check/status commands and plugin registration remain available. Node is only used for development/CI interoperability tests, not at runtime.

## Verification and Release Status

Offline verification includes pinned-Hermes admission/loader/reload contracts, encrypted-input interoperability, management and process recovery. A 50-account, 10,000-message simulation completed without lost or duplicate deliveries. These are not live Telegram, mail-server or model-accuracy tests.

Public hosting/Pages activation and a release-default credential-page URL remain unconfigured. Live Android/iOS/Desktop reply-keyboard and encrypted-return checks, provider accounts, model/delivery verification, and the remote/container/no-inbound deployment matrix remain release gates. No Telegram client or deployment form is claimed live-verified. The Mini App needs HTTPS and Telegram/Web Crypto support; it does not require an installer dashboard, domain or inbound port. A blocked/unavailable page affects credential entry, not existing notifications or non-secret management. Use the optional CLI rather than plaintext chat if secure entry is unavailable.

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

## Telegram Setup and Management

`/mail` uses the authenticated private conversation, not model-supplied IDs. Choose a service, enter the login address, read its app-password guide, and use **Secure app password** when a trusted page is configured. Review and confirm before saving; only then do model/baseline checks, credential/settings publication, scheduling and test delivery run. New defaults: **5 minutes, INBOX, the current authenticated private chat/topic**, and the selected/detected UI language proposed for summaries. Existing settings are preserved.

Later, select accounts by their numbered label/address/status buttons (five per page), without typing UUIDs. The panel supports adding accounts, labels/login/mailbox/service changes, password replacement, disabling/enabling accounts, 5/10/15/30/60-minute schedules, separate UI/summary languages, and sender/domain/keyword/instruction rules with explicit clearing. Existing advanced schedules and arbitrary summary languages are preserved until explicitly changed. All edits, including using the current chat for future notifications, show a preview and need confirmation. Existing group/topic destinations remain supported; there is no new group-discovery UI.

**Status and recovery** shows cron/backlog information and offers a confirmed settings/test refresh. Recorded cron or test-delivery failures retry only unfinished steps, without repeating saved credentials, baselines or model checks. Paused automation never resumes implicitly: confirm **Resume automatic checks**, or explicitly keep it paused and continue management. Conflicts show current settings and the prior candidate; acknowledging retains evidence, does not roll back or finish failed writes, and starts a fresh review from current settings. Prepared notifications keep their text/destination, and existing account IDs, UID cursors and work are retained; changing service/login/mailbox stops the old connection and creates a new one.

Non-secret drafts last up to 24 hours without changes; pending passwords stay in process memory for at most 10 minutes and need re-entry after expiry/restart. Text edits are limited to **16 KiB UTF-8**; oversized inputs are rejected, never silently shortened. Long existing values are preserved and abbreviated only for plain-text display. Encrypted Mini App submissions retain their separate 4096-byte wire limit. The page operator, Telegram client and Hermes host are trust boundaries. Users authorized for the same Hermes profile share that profile's management authority; this is not a multi-tenant account service.

## Terminal Setup Wizard

Use `setup` to manage accounts and notification rules together.

1. On a new installation, select the service and enter the login address directly; no action menu is required.
2. The address is the suggested label and the mailbox is INBOX. Optionally edit these advanced account fields, then enter an **app password** using hidden terminal input.
3. Review the connection result and settings preview. Global advanced settings (destination, schedule, languages and importance rules) are optional.
4. Enter `yes` to explicitly confirm publication. Setup checks a synthetic email with the model, takes a fresh mailbox starting point, saves credentials/settings, updates its owned scheduled job and verifies a Telegram test-message ID.
5. For later management, use `add`, `edit`, `disable`, `enable`, then `done`. Multiple accounts from the same service are supported. These changes also require confirmation before publication.

Terminal new-installation defaults are **every 5 minutes**, **summaries in the selected/detected UI language**, **INBOX**, and **the current profile's Telegram home channel**. Existing schedules, destinations, summary languages (including values such as `fr`), rules and operational limits are preserved. Destination examples include `telegram`, `telegram:123456789`, and `telegram:-1001234567890:456`. In the last example, the final number is the topic ID.

- `edit`: Change an account's label, password, or connection details. Enter its account ID or displayed number.
- `disable` / `enable`: Stop or resume checks for an account. Pending notifications are preserved.
- Enter: Keep the current value. Use `-` to clear sender lists, keyword lists, or additional criteria.
- Ctrl-C during input: Cancel. If an external step fails after settings have been saved, setup reports the completed steps and an error code. Run `setup` again to recover.
- Invalid fields can be corrected in place. Connection failures offer `retry`, `password`, `back`, or `cancel`; other account drafts are preserved until you save.
- Non-secret drafts are kept in separate `setup.db` storage for at most 24 hours without changes. Validated passwords stay only in memory for up to 10 minutes. After expiry/restart, enter them again; an ambiguous credential write is never treated as a successful password replacement. Cron/test-message repair does not require re-entering an already verified, saved password or resetting the mailbox baseline.
- A settings conflict reports `needs_review` instead of overwriting another session's changes. `already_running` preserves inputs for retry. After publication begins, setup reports completed steps rather than claiming cancellation rolled back external writes. A lost test-send response may cause a duplicate test message; it carries the same attempt ID.

Only **mail arriving after an account is first connected** is checked; existing mail is not sent in bulk. New mail is processed even if it has already been read. Changing a label, password, or importance criteria does not reset the starting point. Changing the service, login ID, or mailbox disables the old connection and creates a new one.

Re-running setup preserves an existing cron job's paused state and reports `scheduled` / `resume_cron`, not automatic-check completion. Re-run `setup` to explicitly approve resuming it, or keep it paused and continue account management. Explicit `accounts enable/disable` commands can also continue management without resuming automation. The prior attempt is retained as `acknowledged`, not completed. Disabling all accounts also pauses the cron job. You can resume separately with `hermes cron resume JOB_ID`.

After a settings conflict, setup shows the prior candidate and `current_settings`. You can explicitly acknowledge the old attempt and review a new draft from current settings. External-write evidence is retained; nothing is rolled back or automatically merged, and new changes require confirmation.

### Languages

The existing `language` setting controls model summaries and the fixed labels, fallback reasons, setup confirmation and diagnostic test notifications. Choose `ko` or `en` in `setup`, or use `hermes mail-notifier policy set --language en`. Other existing summary-language values remain supported: the model receives the value unchanged, while fixed messages fall back to English. Already prepared notifications keep their saved text and destination.

The separate `ui_language` setting selects Korean/English setup, status and diagnostic guidance without changing an existing summary `language`. Choose it in `/mail`, or use `setup --ui-language ko|en` / `accounts add/edit --ui-language ko|en`. Resolution is explicit selection → saved UI language → Telegram `language_code` or CLI `LC_ALL`, `LC_MESSAGES`, `LANG` (first nonempty value) → English. Regional locales such as `ko-KR` and `en_US.UTF-8` are normalized; unsupported locales use English. CLI help shows both languages. Secure Mini App entry still requires the trusted HTTPS page configuration and live-release checks described above.

## Operations and Corrections

```sh
hermes mail-notifier accounts list --json
hermes mail-notifier accounts add --ui-language en
hermes mail-notifier accounts edit ACCOUNT_ID --ui-language en
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

Hermes conversation tools: `mail_notifier_check`, `mail_notifier_status`, `mail_notifier_history`, `mail_notifier_queue`, `mail_notifier_feedback`, `mail_notifier_policy`, `mail_notifier_doctor`, and argument-free `mail_notifier_setup`. The setup tool only opens the panel for a verified live Telegram origin; elsewhere it directs you to `/mail`. Existing explicit policy tools and CLI commands remain available. Enter passwords only through secure Mini App entry or hidden terminal setup.

## Importance Rules

Rules are applied in this order:

1. **Excluded senders**: Skip the message without calling the model.
2. **Explicit always-notify senders** (`always_notify_senders`): Honor the user's explicit notification instruction and summarize with AI.
3. **Other mail**: Hermes AI judges the purpose, relevance, and need to read or act. Important senders, keywords, and natural-language criteria provide context rather than overriding its judgment.

Sender rules accept exact addresses such as `boss@example.com` or exact domains such as `@example.com`. Matching is case-insensitive, and domain rules do not automatically include subdomains. A keyword alone never forces an advertisement to be important.

Default criteria prioritize meaningful work and personal correspondence, deadlines, schedule/contract changes, payment failures, and significant account security changes. Promotions, newsletters, and routine receipts are suppressed by default. Automated reservation cancellations can still need attention. For example, add `Prioritize interview schedule changes and contract deadlines` as an additional criterion.

Notifications include the account, sender, subject, reason, priority, summary, action, deadline, and stable mail ID. Categories are work, personal, transactional, promotion, newsletter, and unknown; decisions are notify, suppress, and review. Missing actions/deadlines show `Not specified` in English or `명시 없음` in Korean. Incomplete bodies show `Check the email body` or `본문 확인 필요`; an attempted suppression of an incomplete message becomes a review notification. Machine-readable codes, tool fields and CLI commands are not translated.

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
