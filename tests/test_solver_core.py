from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def one_slot(rooms):
    return [Slot(id=1, position=0, rooms=rooms)]


def test_places_the_single_most_wanted_session_when_one_room():
    p = Problem(
        slots=one_slot({10: 100}),
        candidates=[Candidate(id=100, runners=frozenset()),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2, 3}),
        stars={(1, 200): 5, (2, 200): 5, (3, 100): 5})
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    assert {pl.candidate_id for pl in r.placements} == {200}


def test_coverage_runs_both_sessions_when_rooms_allow():
    p = Problem(
        slots=one_slot({10: 100, 11: 100}),
        candidates=[Candidate(id=100, runners=frozenset()),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5, (2, 200): 5})
    r = build_and_solve(p)
    assert {pl.candidate_id for pl in r.placements} == {100, 200}
    assert r.coverage_pct[1] == 100.0


def test_attend_is_gated_low_interest_does_not_count_as_covered():
    p = Problem(
        slots=one_slot({10: 100}),
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 3}),
        stars={(1, 100): 5, (3, 100): 2})
    r = build_and_solve(p)
    assert 3 in r.left_out[1]
    assert 1 not in r.left_out[1]
