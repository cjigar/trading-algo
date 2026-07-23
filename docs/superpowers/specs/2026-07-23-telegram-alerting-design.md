# Telegram alerting (reliability hardening — phase 1)

**Date:** 2026-07-23
**Status:** Approved (design)

## Problem

The stack self-recovers from crashes and dead feeds, but nothing *tells the operator* when
something goes wrong — a crash-loop (e.g. bad credentials), a dead feed, or a kill-switch halt all
happen silently. For a live-money system you need to know within seconds.

## Goal

Near-real-time Telegram alerts for four categories: **critical safety**, **process health**,
**unexpected errors**, and **trade activity** — delivered from the `algo` process, throttled so an
error storm or the trade tape can't flood the chat, and fail-safe so alerting never destabilises the
trading loop.

## Design (log-driven forwarder → queued Telegram sender)

Every meaningful event already flows through structlog as a named, structured, **redacted** event.
Add one processor after redaction that classifies each event and hands a formatted line to a
background sender. Near-zero call-site changes; automatic coverage of all four categories.

### Units

1. **`observability/alerts.py` — `TelegramAlerter`**
   - Background daemon thread + `queue.Queue`; `send(text, key)` only enqueues (logging stays fast).
   - Worker POSTs to `https://api.telegram.org/bot<token>/sendMessage` via **stdlib `urllib`** (no new
     dependency), short timeout, best-effort.
   - **Throttle/dedup:** collapse repeats of the same `key` within `alert_throttle_seconds`; global
     cap `alert_rate_limit_per_min` with a coalesced "…+N more" summary (avoids Telegram 429/ban).
   - **Fail-safe:** never raises into the caller; no-op when disabled / token or chat-id missing;
     internal send failures log at **WARNING** (not ERROR) so they can't re-trigger the error alert.
   - `init_alerts(settings)` builds the module singleton; `alert_event(event_dict)` classifies +
     sends (no-op when the singleton is absent). `register_secret(token)` so it's scrubbed from logs.

2. **`observability/logging.py`** — add `_alert_processor` to the structlog chain **after**
   `_redact_processor`; it calls `alerts.alert_event(event_dict)` and returns the dict unchanged.
   Skips events from the alerter's own logger to avoid loops.

3. **Event → category mapping** (in `alerts.py`):
   | Category | Triggers (event names / level) | Prefix |
   |---|---|---|
   | Critical safety | `kill_switch`, `manual_halt`, `order_rejected` | 🔴 |
   | Process health | `starting`, `boot_armed`, `reexec`, `session_stopped` | 🟡 |
   | Unexpected errors | any record with level ≥ ERROR (throttled catch-all) | ⚠️ |
   | Trade activity | fill events (entry/exit) — gated by `alert_trade_fills` | 💹 |

   Each formats a concise message from the event + a few whitelisted fields; redaction has already
   run, so no secret can appear in a message.

4. **`config/settings.py`** (env prefix `ALGO_`):
   - `alerts_enabled: bool = False` → `ALGO_ALERTS_ENABLED`
   - `telegram_chat_id: str = ""` → `ALGO_TELEGRAM_CHAT_ID`
   - `alert_trade_fills: bool = True` → toggle the noisier tape
   - `alert_throttle_seconds: int = 300`, `alert_rate_limit_per_min: int = 20`
   The **bot token** is read directly from `os.environ["TELEGRAM_BOT_TOKEN"]` (a secret, never in
   `Settings`, never committed, `register_secret`'d).

5. **Wiring:** `run_algo.main()` calls `alerts.init_alerts(settings)` after `configure_logging()`.
   Only the `algo` process initialises alerts (the API/web are read layers), so there is no
   double-send. `starting` is already logged with `mode`/`live_armed`, so boot/crash-loop alerts need
   no extra call site.

### Error handling

Alerter is entirely best-effort and isolated: a Telegram outage, a bad token, or a network error
logs at WARNING and drops the message; the trading loop is unaffected. Disabled/unconfigured → silent
no-op.

## Non-goals

- **"Whole process is dead" detection** (the algo hard-dies and never restarts) — an in-process
  alerter can't send then. Covered by the external watchdog in phase C (heartbeat + healthcheck).
- Two-way control (Telegram `/stop` etc.) — out of scope for phase 1.

## Tests

- `TelegramAlerter` with an injected fake transport: message sent; dedup collapses repeats within the
  window; global rate cap + coalesced summary; fail-safe on transport error (no raise); no-op when
  disabled / token missing.
- `alert_event` classification: each category maps to the right prefix/message from a synthetic
  event_dict; trade tape gated by `alert_trade_fills`; a `register_secret`'d value never appears in a
  forwarded message.
- Settings defaults + env override.

## Operational setup (operator)

1. Create the bot via **@BotFather** → token. (If a token was ever pasted into a chat, `/revoke` it
   and use the fresh one.)
2. Get the numeric **chat id** (message the bot; @userinfobot, or `getUpdates`).
3. In the server `.env` (gitignored): `TELEGRAM_BOT_TOKEN=…`, `ALGO_TELEGRAM_CHAT_ID=…`,
   `ALGO_ALERTS_ENABLED=true`. Deploy. A "🟡 algo started" message confirms it works.

## Verification

- `make check` + `apps/api` tests + web `tsc` (web unaffected).
- After the operator adds the token/chat-id and deploys: a startup alert arrives; a forced test
  (e.g. temporary manual-halt) delivers a 🔴 alert; repeated errors collapse into one throttled
  message.
