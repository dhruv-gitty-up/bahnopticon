"""Manual SSE smoke check: python test_ws.py (start the backend first)."""
import asyncio
import json

import httpx


async def test():
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        async with client.stream("GET", "http://127.0.0.1:8000/stream") as response:
            response.raise_for_status()
            count = 0
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    snapshot = json.loads(line[6:])
                    print(f"Snapshot {count + 1}: {len(snapshot['features'])} vehicles")
                    count += 1
                    if count == 5:
                        break


if __name__ == "__main__":
    asyncio.run(test())
