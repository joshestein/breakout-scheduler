from __future__ import annotations

from dataclasses import dataclass, field

# Single source of truth for interest weighting and the "would attend" threshold.
# The 5 levels mean: 1 = won't go, 2 = rather not, 3 = maybe, 4 = want, 5 = must.
# 1-2 are negative (won't attend) -> weight 0 and NOT counted as interested.
# 3+ means "would attend"; weight ramps so enthusiasm (4-5) dominates.
WEIGHT: dict[int, int] = {1: 0, 2: 0, 3: 1, 4: 4, 5: 9}
STAR_LABELS: dict[int, str] = {1: "won't go", 2: "rather not", 3: "maybe",
                               4: "want", 5: "must"}
INTERESTED_STARS: int = 3  # >= this counts as "would attend" (coverage + eligibility)


@dataclass(frozen=True)
class Slot:
    id: int
    position: int
    rooms: dict[int, int]  # room_id -> capacity (only rooms available this slot)


@dataclass(frozen=True)
class Candidate:
    id: int                      # proposal id (merge-root)
    runners: frozenset[int]      # participant ids who must be present
    max_attendees: int | None = None   # host seat cap; effective cap = min(room, this)


@dataclass(frozen=True)
class Problem:
    slots: list[Slot]
    candidates: list[Candidate]
    present: frozenset[int]                 # engaged & present participant ids
    stars: dict[tuple[int, int], int]       # (participant_id, candidate_id) -> 1..5 (folded)
    lam: float                              # fairness weight
    min_interest: int                       # eligibility threshold (already applied)
    pins: frozenset[tuple[int, int, int]] = field(default_factory=frozenset)  # (cand, slot, room)
    # When set, the grid is FIXED to exactly these placements (nothing else may run).
    # Used to score the actual current grid instead of re-optimising it.
    fixed: frozenset[tuple[int, int, int]] | None = None

    def rooms_for(self, slot_id: int) -> dict[int, int]:
        return next(s.rooms for s in self.slots if s.id == slot_id)


@dataclass(frozen=True)
class PlacementOut:
    candidate_id: int
    slot_id: int
    room_id: int
    projected_attendance: int
    capacity: int
    overflow: int


@dataclass(frozen=True)
class Result:
    status: str                              # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE"
    placements: list[PlacementOut]
    total_interest: int
    n_present: int                           # engaged+present people (coverage denominator)
    coverage_pct: dict[int, float]           # slot_id -> % of present covered
    left_out: dict[int, list[int]]           # slot_id -> [participant_id] (chosen uncovered)
    forced_uncovered: dict[int, list[int]]   # slot_id -> [participant_id] (capacity-impossible)
    pin_conflicts: list[tuple[int, int, int]] = field(default_factory=list)
    # % of present people seated in (one of) their highest-rated session(s)
    top_choice_pct: float = 0.0
