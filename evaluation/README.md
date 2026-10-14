# Authored mail evaluation cases

`mail_cases.jsonl` contains 120 fictional, independently authored Korean/English emails, 20 per category. Addresses use reserved `.example` domains. These cases contain no user mail. They are distributed under this repository's MIT license.

Each row records an expected decision and a short annotation (`rationale`). `critical` marks must-not-miss schedule/security changes. The evaluator sends only the `mail` fields to the classifier; labels and rationales are not supplied to the model. Incomplete bodies are explicitly marked.

Run on your configured Hermes profile:

```sh
hermes mail-notifier evaluate --runs 3 --output evaluation-report.json
```

This makes up to 360 real model calls. `--limit 5 --runs 1` is a smoke check and cannot pass the release gate. Evaluation does not fetch mail, change the monitoring database, or send Telegram messages. Compare reports only with matching dataset hash, policy version, settings fingerprint and model identity.

Recall counts only `notify` as a successful important-mail decision. `review` and call errors remain in the denominator; review is separately reported. Both notify and review produce alerts and therefore count as promotion false alarms. Unknown costs remain null. Passing this finite authored set does not establish accuracy on arbitrary real inboxes.

`precision` is the proportion of all alerts (`notify` and `review`) that are correct, confirmed important-mail notifications. Reviews do not count as confirmed positives, including appropriate reviews of incomplete input. `notify_precision` separately measures only the `notify` decisions. Returned usage is retained even when output validation fails. Unresolved host attribution such as `auto`/`default` cannot qualify a release.
