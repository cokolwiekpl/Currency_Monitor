"""Shared configuration and quote models, with decimal validation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Rule:
    id: str
    currency: str
    rate_type: str
    direction: str
    threshold: Decimal


@dataclass(frozen=True)
class Profile:
    id: str
    source_id: str
    webhook_env: str
    alerts: tuple[Rule, ...]


@dataclass(frozen=True)
class Config:
    sources: dict
    profiles: tuple[Profile, ...]


@dataclass(frozen=True)
class Quote:
    marker: str
    rate: Decimal
    label: str
    published: str


def as_decimal(value, desc: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{desc}: expected a number.")
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"{desc}: invalid number.") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{desc}: rate or threshold must be greater than zero.")
    return result


