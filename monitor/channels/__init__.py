"""Alert channels: plug-ins that each implement AlertChannel.send(event). Dispatch fans an
event out to every enabled channel, with a per-channel try/except so one channel's failure
never blocks another or the check loop (Rule 15)."""
import asyncio

from monitor.channels.base import AlertChannel
from monitor.channels.email_gmail import EmailGmailChannel
from monitor.channels.sms_email_gateway import SmsEmailGatewayChannel
from monitor.channels.sms_twilio import SmsTwilioChannel

_REGISTRY: dict[str, type[AlertChannel]] = {
    "email": EmailGmailChannel,
    "sms": SmsTwilioChannel,
    "sms_gateway": SmsEmailGatewayChannel,
}


def build_channels(names: list[str]) -> list[AlertChannel]:
    channels = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        cls = _REGISTRY.get(name)
        if cls is None:
            print(f"[channels] unknown channel {name!r} in ALERT_CHANNELS, skipping")
            continue
        channels.append(cls())
    return channels


async def dispatch(event, channels: list[AlertChannel]) -> None:
    for channel in channels:
        try:
            await asyncio.to_thread(channel.send, event)
        except Exception as exc:  # a broken channel must never break another channel or the loop
            print(f"[channels] {channel.name} failed to send: {exc!r}")
