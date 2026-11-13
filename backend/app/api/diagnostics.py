"""Bounded transport check that never calls an AI provider."""

import asyncio
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["Integration diagnostics"])


@router.get("/diagnostics/stream")
async def stream_probe():
    async def stream():
        for seq in range(3):
            yield (
                "data: "
                + json.dumps(
                    {
                        "event": "heartbeat",
                        "sequence": seq,
                        "scope": "transport_check_no_ai",
                    }
                )
                + "\n\n"
            )
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
