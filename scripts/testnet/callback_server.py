"""Tiny callback receiver for the E2E test — verifies the swap HMAC signature.

    uv run python -m scripts.testnet.callback_server --secret <callback_secret> [--port 9009]

The `callback_secret` is the one printed by `scripts.register_service` for the
service you call swap with. On each POST it recomputes the HMAC over the raw body
(the swap-side scheme) and compares: a valid signature → 200 (order advances to
`notified`); a bad/forged one → 401. That 401 path is the whole point of signing
— a forged 'paid' can't buy a free service.
"""
from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from swap.services import callback


def build_app(secret: str) -> FastAPI:
    app = FastAPI()

    @app.post("/cb")
    async def cb(request: Request) -> dict:
        raw = (await request.body()).decode()
        sig = request.headers.get("X-Swap-Signature", "")
        if not callback.verify(raw, secret, sig):
            print(f"REJECTED callback (bad signature): {raw}")
            raise HTTPException(status_code=401, detail="bad signature")
        print(f"VERIFIED callback: {raw}")
        return {"ok": True}

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", required=True, help="service callback_secret")
    ap.add_argument("--port", type=int, default=9009)
    args = ap.parse_args()
    print(f"callback receiver on :{args.port}  (POST /cb)")
    uvicorn.run(build_app(args.secret), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
