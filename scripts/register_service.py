"""Mint a calling service: prints its API key + callback secret (shown once).

Usage:  uv run python -m scripts.register_service "service name"

The API key authenticates the service to swap; the callback secret is the
HMAC-SHA256 key swap uses to sign callbacks to it. Store both in the service's
own secrets — they are not retrievable later.
"""
from __future__ import annotations

import asyncio
import secrets
import sys

from swap import db, repository


async def main(name: str) -> None:
    await db.connect()
    api_key = "swk_" + secrets.token_urlsafe(24)
    callback_secret = secrets.token_urlsafe(32)
    service_id = await repository.create_service(name, api_key, callback_secret)
    await db.close()
    print(f"service_id       {service_id}")
    print(f"name             {name}")
    print(f"api_key          {api_key}")
    print(f"callback_secret  {callback_secret}")
    print("\nStore these now — they are not shown again.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.register_service <service name>")
    asyncio.run(main(sys.argv[1]))
