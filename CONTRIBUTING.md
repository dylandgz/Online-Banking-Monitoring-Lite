# Contributing (currently: the SMS channel)

The only open contribution seam in this project right now is the SMS alert channel.
Everything else in `monitor/` is being built stage-by-stage against `CLAUDE.md` — please
don't send unsolicited PRs against `check.py`, `state.py`, `journey.py`, `db.py`, `web.py`,
or `main.py`.

## What you're building

`monitor/channels/sms_stub.py` currently raises `NotImplementedError`. Replace its
contents with a real implementation that sends an SMS when `send()` is called.

## The contract

Every alert channel is a class implementing `monitor.channels.base.AlertChannel`:

```python
class AlertChannel(ABC):
    name: str = "channel"

    @abstractmethod
    def send(self, event: AlertEvent) -> None:
        ...
```

- `event` is either a `DownEvent` or `RecoveryEvent` (see `monitor/state.py`) — plain,
  already-computed data. You don't call anything else in the codebase; you just format
  `event` into a message and send it.
- `send()` runs synchronously in a worker thread (`asyncio.to_thread`), so blocking network
  calls (e.g. a provider's HTTP API) are fine — don't add `async`.
- Raise on failure. `monitor/channels/__init__.py`'s `dispatch()` wraps every channel call
  in its own `try/except`; a raised exception is logged and the loop keeps going. Other
  channels (email) still fire even if SMS fails. Don't swallow errors yourself — raising is
  how the dispatcher knows to log a failure instead of a silent no-op.

## Message content

Keep SMS text short — this is not the same message `email_gmail.py` sends. Write your own
terse formatter from the `DownEvent`/`RecoveryEvent` fields (status, since_ts,
fail_reason, duration_s). Do not import or reuse `email_gmail.down_message` /
`recovery_message`.

## Files you may touch

- `monitor/channels/sms_stub.py` (or rename it `sms_<provider>.py` if you like — just
  update the registry import in `monitor/channels/__init__.py`)
- `tests/test_channels_sms.py` (new file)
- `.env.example` — add whatever `SMS_*` variables your provider needs, under a
  `# --- SMS channel ---` heading. Core code (`config.py`) never reads `SMS_*` directly;
  your channel module reads its own env vars.
- `requirements.txt` — one new dependency for your SMS provider's SDK is fine. More than
  one, or anything that pulls in a background service, needs a discussion first — see
  CLAUDE.md's "no second process" rule.

## What NOT to touch

`monitor/channels/base.py`, `monitor/channels/__init__.py`'s `dispatch()` fan-out logic,
`monitor/channels/email_gmail.py`, or anything outside `monitor/channels/`. If the contract
itself needs to change, open an issue first — don't just widen it in a PR.

## Testing expectations

Mock the actual network call (whatever HTTP client / SDK you use) — tests must not send a
real SMS. At minimum:

- `send()` on a `DownEvent` and a `RecoveryEvent` each produce the expected outbound call
  (right recipient, right message shape).
- A failure from the provider (e.g. the mock raises) propagates out of `send()` rather
  than being swallowed — that's what lets `dispatch()`'s per-channel try/except do its job.

Run the existing suite too — `pytest` — to confirm you haven't broken anything else:

```
pytest
```

## Enabling it

Once built, a maintainer opts in via `.env`:

```
ALERT_CHANNELS=email,sms
```

It stays off (`ALERT_CHANNELS=email`, the default) until someone deliberately turns it on.
