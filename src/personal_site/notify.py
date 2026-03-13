from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class NtfyConfig:
    base_url: str
    topic: str
    token: str | None = None
    user: str | None = None
    password: str | None = None


class NotificationError(RuntimeError):
    pass


def send_ntfy(
    *,
    config: NtfyConfig,
    message: str,
    title: str | None = None,
    tags: list[str] | None = None,
    priority: int | None = None,
    click: str | None = None,
) -> str | None:
    if not config.topic:
        raise NotificationError("NTFY_TOPIC is required")

    url = f"{config.base_url}/{config.topic}"

    headers: dict[str, str] = {
        "Content-Type": "text/plain; charset=utf-8",
        "Accept": "application/json",
    }
    if title:
        headers["Title"] = title
    if tags:
        headers["Tags"] = ",".join(tags)
    if priority is not None:
        headers["Priority"] = str(priority)
    if click:
        headers["Click"] = click
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    auth: tuple[str, str] | None = None
    if config.user and config.password:
        auth = (config.user, config.password)

    try:
        resp = requests.post(
            url, data=message.encode("utf-8"), headers=headers, auth=auth, timeout=10
        )
    except requests.RequestException as exc:
        raise NotificationError(str(exc)) from exc

    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise NotificationError(f"ntfy request failed ({resp.status_code}): {detail}")

    # ntfy returns JSON containing an id; if we can't parse it, just return None.
    try:
        body = resp.json()
        msg_id = body.get("id") if isinstance(body, dict) else None
        return str(msg_id) if msg_id else None
    except Exception:
        return None


def listen_ntfy(
    *,
    config: NtfyConfig,
    since: str | None = None,
):
    """Yield incoming ntfy events for a topic.

    This uses the JSON streaming endpoint: GET /<topic>/json.
    Yields decoded dicts for events that include a message id.
    """

    if not config.topic:
        raise NotificationError("NTFY_TOPIC is required")

    url = f"{config.base_url}/{config.topic}/json"

    headers: dict[str, str] = {"Accept": "application/json"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    params: dict[str, str] = {}
    if since:
        params["since"] = since

    auth: tuple[str, str] | None = None
    if config.user and config.password:
        auth = (config.user, config.password)

    try:
        with requests.get(
            url,
            headers=headers,
            params=params,
            auth=auth,
            stream=True,
            timeout=(5, 60),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue

                if not isinstance(event, dict):
                    continue
                # Only "message" events include the published message.
                if event.get("event") != "message":
                    continue
                if not event.get("id"):
                    continue
                yield event
    except requests.RequestException as exc:
        raise NotificationError(str(exc)) from exc
