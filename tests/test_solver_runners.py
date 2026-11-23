from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def two_slots(rooms):
    return [Slot(id=1, position=0, rooms=rooms), Slot(id=2, position=1, rooms=rooms)]


def test_runner_running_two_sessions_cannot_have_both_in_same_slot():
    p = Problem(
        slots=two_slots({10: 50}),
        candidates=[Candidate(id=100, runners=frozenset({9})),
                    Candidate(id=200, runners=frozenset({9}))],
        present=frozenset({9, 1, 2}),
        stars={(1, 100): 5, (2, 200): 5})
    r = build_and_solve(p)
    by_slot: dict[int, set[int]] = {}
    for pl in r.placements:
        by_slot.setdefault(pl.slot_id, set()).add(pl.candidate_id)
    for cands in by_slot.values():
        assert not {100, 200}.issubset(cands)


def test_runner_is_not_counted_left_out_while_running():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 50})],
        candidates=[Candidate(id=100, runners=frozenset({9}))],
        present=frozenset({9, 1}),
        stars={(1, 100): 5})
    r = build_and_solve(p)
    assert 9 not in r.left_out[1]
