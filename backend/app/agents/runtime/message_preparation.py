from __future__ import annotations

from typing import Any


def prepare_messages_for_model_request(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] | None = None
    for index, message in enumerate(messages):
        if "usage" not in message:
            continue
        if prepared is None:
            prepared = [dict(item) for item in messages]
        prepared[index].pop("usage", None)
    return prepared if prepared is not None else messages
