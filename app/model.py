"""The clean shape we map DraftKings into: game -> market -> side -> line + odds."""
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class Game:
    id: str
    name: str
    away: str
    home: str
    start: Optional[float]  # unix seconds
    status: str = "NOT_STARTED"


@dataclass
class Market:
    id: str
    game_id: str
    kind: str  # moneyline | spread | total
    suspended: bool = False
    as_of: float = 0.0  # DraftKings publish time of the last state we applied


@dataclass
class Outcome:
    """One side of one market, e.g. 'DAL spread'. Keyed by (market_id, side).

    The selection id is NOT the key: DraftKings bakes the line into the id
    (0HC84695450N300_1 = -3), so a line move arrives as a new selection.
    """
    market_id: str
    side: str  # home | away | over | under
    selection_id: str
    line: Optional[float]
    american: Optional[int]
    decimal: Optional[float]
    available: bool = True
    prev_line: Optional[float] = None
    prev_american: Optional[int] = None
    changed_at: Optional[float] = None  # our server clock, when the value last changed
    as_of: float = 0.0  # DraftKings publish time of the value we hold


@dataclass
class SelectionRow:
    """A selection as it appears in a snapshot, before it goes into the book."""
    id: str
    market_id: str
    side: str
    line: Optional[float]
    american: int
    decimal: Optional[float]


@dataclass
class Snapshot:
    games: Dict[str, Game] = field(default_factory=dict)
    markets: Dict[str, Market] = field(default_factory=dict)
    selections: List[SelectionRow] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)  # why rows were skipped


def to_dict(obj) -> dict:
    return asdict(obj)
