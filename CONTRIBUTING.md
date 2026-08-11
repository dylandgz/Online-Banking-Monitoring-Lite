# Contributing (adding a new alert channel)

`email` (Gmail) and `sms` (Twilio) are the two live alert channels. The channels seam is
still open for another provider (e.g. a different SMS/Slack/webhook integration).
Everything else in `monitor/` is being built stage-by-stage against `CLAUDE.md` — please
don't send unsolicited PRs against `check.py`, `state.py`, `journey.py`, `db.py`, `web.py`,
or `main.py`.

## What you're building

A new file in `monitor/channels/`, e.g. `monitor/channels/slack_webhook.py`, implementing
`AlertChannel` and registered in `monitor/channels/__init__.py`'s `_REGISTRY`.
`monitor/channels/sms_twilio.py` is a working reference implementation to copy the shape of.

## The contract

Every alert channel is a class implementing `monitor.channels.base.AlertChannel`:

```python
class AlertChannel(ABC):
    name: str = "channel"

    @abstractmethod
    def send(self, event: AlertEvent) -> None:
        ...
```

- `event` is a `DownEvent`, `RecoveryEvent`, or `ConfigErrorEvent` (see `monitor/state.py`)
  — plain, already-computed data. You don't call anything else in the codebase; you just
  format `event` into a message and send it.
- `send()` runs synchronously in a worker thread (`asyncio.to_thread`), so blocking network
  calls (e.g. a provider's HTTP API) are fine — don't add `async`.
- Raise on failure. `monitor/channels/__init__.py`'s `dispatch()` wraps every channel call
  in its own `try/except`; a raised exception is logged and the loop keeps going. Other
  channels still fire even if yours fails. Don't swallow errors yourself — raising is how
  the dispatcher knows to log a failure instead of a silent no-op.

## Message content

Keep alert text terse and write your own formatter — don't import or reuse
`email_gmail.down_message` / `recovery_message` or `sms_twilio.down_message` /
`recovery_message`. Each channel formats the same event fields for its own medium.

## Files you may touch

- `monitor/channels/<your_channel>.py` (new file)
- `tests/test_channels_<your_channel>.py` (new file)
- `.env.example` and `.env` — add whatever variables your provider needs, under a
  `# --- <channel> channel ---` heading. Core code (`config.py`) never reads channel-specific
  vars directly; your channel module reads its own env vars (see `sms_twilio.py`).
- `requirements.txt` — one new dependency for your provider's SDK is fine. More than one,
  or anything that pulls in a background service, needs a discussion first — see
  CLAUDE.md's "no second process" rule.
- `monitor/channels/__init__.py` — only to add your class to `_REGISTRY` (one import, one
  dict entry). Don't touch `dispatch()`'s fan-out logic.

## What NOT to touch

`monitor/channels/base.py`, `monitor/channels/__init__.py`'s `dispatch()` fan-out logic,
`monitor/channels/email_gmail.py`, `monitor/channels/sms_twilio.py`, or anything outside
`monitor/channels/`. If the contract itself needs to change, open an issue first — don't
just widen it in a PR.

## Testing expectations

Mock the actual network call (whatever HTTP client / SDK you use) — tests must not send a
real message. At minimum, see `tests/test_channels_sms.py` for the pattern:

- `send()` on a `DownEvent`, `RecoveryEvent`, and `ConfigErrorEvent` each produce the
  expected outbound call (right recipient, right message shape).
- A failure from the provider (e.g. the mock raises) propagates out of `send()` rather
  than being swallowed — that's what lets `dispatch()`'s per-channel try/except do its job.

Run the existing suite too — `pytest` — to confirm you haven't broken anything else:

```
pytest
```

## Enabling a channel

A maintainer opts in via `.env`:

```
ALERT_CHANNELS=email,sms
```

Channels stay off unless listed there. `ALERT_CHANNELS=email` is the default.
