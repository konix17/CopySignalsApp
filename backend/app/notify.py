"""Push alerts to your phone/chat. Set NOTIFY_WEBHOOK_URL to one of:
- an ntfy.sh topic, e.g. https://ntfy.sh/my-secret-topic (install the ntfy app and subscribe)
- a Discord webhook URL
- a Slack incoming-webhook URL
"""

import logging

import httpx

log = logging.getLogger(__name__)


async def send(client: httpx.AsyncClient, url: str, message: str) -> None:
    if not url:
        return
    try:
        if "ntfy" in url:
            await client.post(url, content=message.encode(), headers={"Title": "Copy Signals"})
        elif "discord" in url:
            await client.post(url, json={"content": message})
        else:
            await client.post(url, json={"text": message})
    except httpx.HTTPError as e:
        log.warning("notification failed: %r", e)
