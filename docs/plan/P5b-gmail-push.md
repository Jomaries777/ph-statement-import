# P5b. Gmail push: import a statement seconds after it arrives

Status: In progress · Estimate: about 1–2 days, 2 PRs · Needs: P5

## Goal

A new statement email is imported within seconds, not at the next 15-minute check. Nothing changes on the page: the same mapping, the same run log, the same "Turn off and delete".

## How it works

1. When a user turns auto-import on, the app calls Gmail's `users.watch`. From then on, every change in that user's inbox makes Gmail publish a small message to the Pub/Sub topic `gmail-push`. The message holds only the email address and a change number, never what changed.
2. A push subscription posts each message to `POST /tasks/gmail-push`, with an OIDC token from the `pubsub-push` service account.
3. The app checks the token and finds the user with auto-import on for that address. Then it runs the same per-user import as the poll (`runner.process_push()` → `process_user()`).

```
Gmail inbox ─► Pub/Sub topic gmail-push ─► POST /tasks/gmail-push ─► runner.process_push(email)
Cloud Scheduler, every 15 min ─────────►  POST /tasks/poll-gmail  ─► runner.poll_all()
                                                both ─► process_user(user)
```

## Why it's built this way

- **The 15-minute poll stays.**
  - A watch ends after 7 days, and the poll renews it when less than a day is left (`WATCH_RENEW_BEFORE`).
  - Google doesn't promise every push arrives, so the poll is the safety net.
  - It still costs nothing.
- **YNAB is only called when there's a statement to import.** Gmail pushes on every inbox change: each email, and each one you read. So most runs find nothing. YNAB replaces its refresh token on every refresh, so the runner gets a YNAB token only after it has found a new statement email.
- **One run at a time.** A push and a poll can arrive together. Two refreshes with the same single-use YNAB refresh token would fail one of them. Cloud Run runs one instance, so a lock in memory is enough (`runner._lock`). A push that finds the lock busy gets a 503, and Pub/Sub sends it again 10 seconds later.
- **Failed pushes aren't retried by Pub/Sub.** The route answers 204 to everything except "busy". A user whose Gmail access was revoked would otherwise be retried for as long as Pub/Sub keeps the message. The next poll tries again instead.
- **Only the inbox is watched** (`labelIds: ["INBOX"]`). A statement that a Gmail filter moves past the inbox is still found by the poll.
- **Gmail search can lag a few seconds** behind a new email. If a push's search finds nothing, the next inbox change or the next poll imports the statement. So the delay is usually seconds, and at worst 15 minutes.
- **`gmail.readonly` is enough** for `watch`, so there's no new consent screen.

## Cost check

| Service | Use | Free tier |
|---|---|---|
| Pub/Sub | dozens of ~100-byte messages a day per user | 10 GiB a month |
| Cloud Run | one short request per push; most do one Gmail search and stop | 180,000 vCPU-seconds a month; about 45,000 at 300 pushes a day, with cold starts |
| Firestore | per push: a few reads to find the user, one write for `last_checked` | 50,000 reads and 20,000 writes a day |

## Before you start

- P5 live: auto-import works for GCash, and the `poll-gmail` Scheduler job exists (P5 §C).

## Steps

### A. Google Cloud setup (owner, once)

- [ ] Create the topic: `gcloud pubsub topics create gmail-push`
- [ ] Let Gmail publish to it: `gcloud pubsub topics add-iam-policy-binding gmail-push --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher`
- [ ] Create the push subscription. It reuses `pubsub-push`, the service account budget-guard uses. Each subscription has its own audience, so their tokens can't be swapped.
  ```bash
  gcloud pubsub subscriptions create gmail-push --topic=gmail-push \
    --push-endpoint=<PUBLIC_URL>/tasks/gmail-push \
    --push-auth-service-account=pubsub-push@<project>.iam.gserviceaccount.com \
    --push-auth-token-audience=<PUBLIC_URL>/tasks/gmail-push \
    --ack-deadline=60 --message-retention-duration=10m \
    --min-retry-delay=10s --max-retry-delay=60s
  ```
- [ ] Add the GitHub `production` variable `GMAIL_PUSH_TOPIC=projects/<project>/topics/gmail-push`. Empty leaves push off: the poll alone still works.

### PR 1: `feat/gmail-push-runner`, "feat: Gmail watch, and the runner gets a YNAB token only when needed"

- [ ] `autoimport/gmail.py`: `watch(access_token, topic)` returns when the watch ends; `stop(access_token)`.
- [ ] `autoimport/runner.py`:
  - `process_user()` finds new emails first, and refreshes YNAB only if there are any.
  - It renews the watch when less than a day is left (`_renew_watch()`).
  - `process_push(email)` and `start_watch(user)`.
  - A lock so only one run goes at a time.
- [ ] Tests:
  - `tests/test_autoimport_push.py`
  - watch/stop in `tests/test_autoimport_gmail.py`
  - `tests/test_autoimport.py`: the token-rotation test now has an email to import

### PR 2: `feat/gmail-push-route`, "feat: import statement emails as soon as Gmail pushes them"

- [ ] `web/auto_import.py`:
  - `POST /tasks/gmail-push`, with the same OIDC check as `/tasks/poll-gmail`, factored into one helper. It answers 204, or 503 when busy.
  - Saving the settings calls `runner.start_watch()`.
  - "Turn off and delete" calls `gmail.stop()` before revoking.
- [ ] `deploy.yml`: pass `GMAIL_PUSH_TOPIC`.
- [ ] Privacy page: Google notifies the app when your inbox changes; the notification holds your address and a change number, never an email's content.
- [ ] README "Auto-import (hosted)" and `docs/architecture.md`: the push path.

## Tests

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- After turning auto-import on, Firestore shows `gmail_watch_expires` about 7 days out.
- Any email to yourself makes a push arrive (log line `Auto-import push`), with no YNAB call and no run.
- A real GCash statement email is imported within about a minute of arriving, unapproved. Opening it afterwards creates no duplicate.
- "Turn off and delete" stops the pushes: no more `Auto-import push` lines for that user.
- Billing → Reports still shows **$0.00** after a few days.

## Risks and rollback

| Risk | What to do |
|---|---|
| Pushes pile up (a backlog in `gcloud pubsub subscriptions describe gmail-push`) | Check the route's logs for 403s (audience or service account) or 503s (a run stuck holding the lock). |
| The watch fails to start ("couldn't start Gmail push" in the logs) | Usually the `gmail-api-push` publisher grant on the topic is missing. The poll keeps importing in the meantime. |
| Too many pushes for the free tier | Unset `GMAIL_PUSH_TOPIC` and let the watches end. The 15-minute poll carries on alone. |

Rollback: empty `GMAIL_PUSH_TOPIC` and redeploy. Watches end within 7 days. Pushes that still arrive are answered and ignored, and the poll keeps working. Then delete the subscription and the topic.
