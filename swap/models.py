"""Order status enum + API request/response schemas.

The status set is small thanks to the fixed rate: no requote / quote_expired,
and no fulfill_failed (service-level fulfillment is not swap's concern).
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class OrderStatus(StrEnum):
    CREATED = "created"
    AWAITING_PAYMENT = "awaiting_payment"
    CONFIRMED = "confirmed"            # USDT seen + enough confirmations + AML clear
    EMC_DELIVERED = "emc_delivered"    # EMC sent to destination (emc_txid known)
    NOTIFIED = "notified"              # signed callback acknowledged (terminal/done)
    UNDERPAID = "underpaid"            # paid < amount → top-up or partial refund
    OVERPAID = "overpaid"             # paid > amount → refund excess
    AML_HOLD = "aml_hold"             # sender on blacklist → manual review
    DELIVER_FAILED = "deliver_failed"  # EMC send failed → retry, else refund USDT
    EXPIRED = "expired"               # no payment before TTL


# --- API contract (REST + MCP share these) ---------------------------------

class BuyEmcRequest(BaseModel):
    amount_usdt: float = Field(..., gt=0, description="USDT to collect (≤ cap)")
    destination_emc_address: str = Field(..., description="where EMC is delivered")
    callback_url: str = Field(..., description="signed POST lands here when paid")
    ref: str = Field(..., description="caller's invoice id — idempotency key")


class BuyEmcResponse(BaseModel):
    order_id: int
    deposit_address: str = Field(..., description="unique TRON address to send USDT to")
    amount_usdt: float
    emc_amount: float
    status: OrderStatus
    expires_at: str


class OrderResponse(BaseModel):
    order_id: int
    ref: str
    status: OrderStatus
    amount_usdt: float
    emc_amount: float
    destination_emc_address: str
    deposit_address: str
    emc_txid: str | None = None
    expires_at: str
    created_at: str
    updated_at: str
