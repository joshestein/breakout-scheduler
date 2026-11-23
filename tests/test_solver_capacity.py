from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def test_overflow_is_flagged_not_fatal():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 2})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 2, 3, 4, 5}),
        stars={(u, 100): 5 for u in range(1, 6)},
        lam=0.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    assert len(r.placements) == 1
    assert r.placements[0].overflow == 3


def test_forced_uncovered_when_capacity_below_present_interest():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 2})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 2, 3, 4}),
        stars={(u, 100): 5 for u in range(1, 5)},
        lam=20.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert len(r.left_out[1]) >= 2
    assert len(r.forced_uncovered[1]) >= 2


def test_host_max_attendees_caps_seated():
    # one big room (cap 10) but the host caps the session at 2 — only 2 are seated,
    # and the displayed capacity reflects the tighter host cap (min(room, max)).
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10})],
        candidates=[Candidate(id=100, runners=frozenset(), max_attendees=2)],
        present=frozenset({1, 2, 3, 4, 5}),
        stars={(u, 100): 5 for u in range(1, 6)},
        lam=0.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    cell = r.placements[0]
    assert cell.projected_attendance <= 2
    assert cell.capacity == 2          # min(room 10, host cap 2)
    assert cell.overflow == 3          # 5 interested, 2 seats


def test_host_max_routes_overflow_to_next_best():
    # capped session (max 1) and an alternative; overflow people should be covered
    # by the alternative rather than left out — "best preferences, nothing overcounted".
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 5, 11: 5})],
        candidates=[Candidate(id=100, runners=frozenset(), max_attendees=1),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2, 3}),
        stars={**{(u, 100): 5 for u in (1, 2, 3)},
               **{(u, 200): 4 for u in (1, 2, 3)}},
        lam=10.0, min_interest=1,
    )
    r = build_and_solve(p)
    capped = [pl for pl in r.placements if pl.candidate_id == 100]
    assert capped and capped[0].projected_attendance <= 1
    assert not r.left_out.get(1)       # everyone still has somewhere to be


def test_cap_below_runner_count_floors_to_runners():
    # host caps at 1 but 3 co-runners must be present — the cap floors up to the
    # runner count so the session stays placeable and all runners are seated.
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10})],
        candidates=[Candidate(id=100, runners=frozenset({1, 2, 3}), max_attendees=1)],
        present=frozenset({1, 2, 3, 4, 5}),
        stars={(u, 100): 5 for u in range(1, 6)},
        lam=0.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    assert r.placements, "capped session with co-runners must still be placeable"
    assert r.placements[0].projected_attendance >= 3   # all three runners fit
