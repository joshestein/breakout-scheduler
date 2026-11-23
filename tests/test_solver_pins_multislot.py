from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def test_pin_is_honored():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 50, 11: 50})],
        candidates=[Candidate(id=100, runners=frozenset()),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5, (2, 200): 5},
        lam=0.0, min_interest=1,
        pins=frozenset({(100, 1, 11)}),
    )
    r = build_and_solve(p)
    pinned = [pl for pl in r.placements if pl.candidate_id == 100]
    assert pinned and pinned[0].room_id == 11


def test_conflicting_pin_is_reported_not_crash():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 50})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1}),
        stars={(1, 100): 5},
        lam=0.0, min_interest=1,
        pins=frozenset({(100, 1, 999)}),
    )
    r = build_and_solve(p)
    assert (100, 1, 999) in r.pin_conflicts
