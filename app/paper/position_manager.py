"""Virtual positions: fills accumulate, markets settle to $1 / $0 at resolution."""
from __future__ import annotations

from dataclasses import dataclass

from ..storage.models import Market, Position


@dataclass
class Settlement:
    condition_id: str
    token_id: str
    label: str
    size: float
    avg_price: float
    payout: float   # per share
    pnl: float


class PositionManager:
    def __init__(self):
        self.positions: dict[str, Position] = {}  # token_id -> Position

    def on_fill(self, condition_id: str, token_id: str, label: str, price: float, size: float) -> Position:
        pos = self.positions.get(token_id)
        if pos is None:
            pos = Position(condition_id=condition_id, token_id=token_id, label=label)
            self.positions[token_id] = pos
        total_cost = pos.avg_price * pos.size + price * size
        pos.size += size
        pos.avg_price = total_cost / pos.size if pos.size > 0 else 0.0
        return pos

    def has_position(self, token_id: str) -> bool:
        pos = self.positions.get(token_id)
        return pos is not None and pos.size > 0

    def exposure_usd(self, condition_id: str) -> float:
        return sum(p.cost_usd for p in self.positions.values() if p.condition_id == condition_id)

    def total_exposure_usd(self) -> float:
        return sum(p.cost_usd for p in self.positions.values())

    def settle_market(self, market: Market, outcome_yes: float) -> list[Settlement]:
        """Remove and settle all positions of the market. YES pays outcome_yes,
        NO pays 1 - outcome_yes (per share)."""
        out: list[Settlement] = []
        for token_id in list(self.positions):
            pos = self.positions[token_id]
            if pos.condition_id != market.condition_id or pos.size <= 0:
                continue
            payout = outcome_yes if token_id == market.yes_token_id else 1.0 - outcome_yes
            pnl = pos.size * (payout - pos.avg_price)
            out.append(Settlement(
                condition_id=pos.condition_id, token_id=token_id, label=pos.label,
                size=pos.size, avg_price=pos.avg_price, payout=payout, pnl=pnl,
            ))
            del self.positions[token_id]
        return out
