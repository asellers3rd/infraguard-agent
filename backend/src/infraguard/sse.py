"""SSE response helpers."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from sse_starlette.sse import EventSourceResponse

from .store import RunStore, event_to_dict


async def stream_run_events(store: RunStore, run_id: str) -> AsyncIterator[dict]:
    """Yield SSE-formatted events for a single run until close_stream() is called."""
    queue = store.get_queue(run_id)
    if queue is None:
        yield {"event": "error", "data": json.dumps({"message": f"unknown run {run_id}"})}
        return

    # Replay any events already in the run (so late subscribers see history)
    run = store.get_run(run_id)
    if run is not None:
        for past_event in run.events:
            yield {
                "event": past_event.type,
                "data": json.dumps(event_to_dict(past_event)),
            }

    while True:
        event = await queue.get()
        if event is None:
            yield {"event": "done", "data": "{}"}
            break
        yield {
            "event": event.type,
            "data": json.dumps(event_to_dict(event)),
        }


def sse_response(store: RunStore, run_id: str) -> EventSourceResponse:
    return EventSourceResponse(stream_run_events(store, run_id))
