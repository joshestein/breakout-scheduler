"""The scheduling rules: coverage first, then interest, then earlier proposal; hosts earn
no points; a running session seats at least `min_seated` non-hosts unless pinned."""
import random

from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def _placed(r):
    return {pl.candidate_id for pl in r.placements}


def test_coverage_beats_interest():
    # C is hosted by 1, who would rather be in A. Running C pulls 1 out of A (less
    # interest) but gives 5 somewhere to be. Coverage wins.
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10, 11: 10})],
        candidates=[Candidate(id=100, runners=frozenset()),           # A
                    Candidate(id=200, runners=frozenset()),           # B
                    Candidate(id=300, runners=frozenset({1}))],       # C
        present=frozenset({1, 2, 3, 4, 5}),
        stars={**{(u, 100): 5 for u in (1, 2, 3, 4)},
               **{(u, 200): 4 for u in (1, 2, 3, 4)},
               (5, 300): 3},
    )
    r = build_and_solve(p)
    assert _placed(r) == {100, 300}
    assert r.left_out[1] == []


def test_host_only_session_never_runs():
    # nobody but the host wants 100; the floor keeps it off the grid even though
    # running it would "cover" the host
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10, 11: 10})],
        candidates=[Candidate(id=100, runners=frozenset({1})),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5, (2, 200): 5},
    )
    r = build_and_solve(p)
    assert _placed(r) == {200}
    assert r.left_out[1] == [1]


def test_host_earns_no_points_for_own_session():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10})],
        candidates=[Candidate(id=100, runners=frozenset({1}))],
        present=frozenset({1, 2}),
        stars={(1, 100): 5, (2, 100): 3},
    )
    r = build_and_solve(p)
    assert _placed(r) == {100}
    assert r.total_interest == 1          # only the guest's ★3


def test_floor_counts_seated_guests_not_ratings():
    # A needs 2 guests. Both 1 and 2 want A, but 2 hosts B in the same (only) slot,
    # so at most 1 guest can sit in A: A must not run.
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10, 11: 10})],
        candidates=[Candidate(id=100, runners=frozenset(), min_seated=2),
                    Candidate(id=200, runners=frozenset({2}))],
        present=frozenset({1, 2, 3}),
        stars={(1, 100): 5, (2, 100): 5, (3, 200): 3},
    )
    r = build_and_solve(p)
    assert 100 not in _placed(r) or 200 not in _placed(r)
    for pl in r.placements:
        if pl.candidate_id == 100:
            assert pl.seated_guests >= 2


def test_pin_skips_floor():
    # nobody wants 100, but the admin pinned it: it runs and the model stays feasible
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10, 11: 10})],
        candidates=[Candidate(id=100, runners=frozenset(), min_seated=3),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1}),
        stars={(1, 200): 5},
        pins=frozenset({(100, 1, 10)}),
    )
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    assert _placed(r) == {100, 200}


def test_tie_goes_to_earlier_proposal():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 10})],
        candidates=[Candidate(id=200, runners=frozenset()),
                    Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 4, (2, 100): 4, (1, 200): 4, (2, 200): 4},
    )
    assert _placed(build_and_solve(p)) == {100}


def test_invariants_on_random_retreats():
    """For many random 30-person retreats: every running session that isn't pinned
    seats at least its floor of non-hosts, never more non-hosts than its seats, and
    every present host of a running session is not left out in that slot."""
    rng = random.Random(7)
    for _ in range(5):
        people = list(range(1, 31))
        cands = []
        for cid in range(100, 116):
            hosts = frozenset(rng.sample(people, rng.choice([0, 1, 1, 2])))
            cands.append(Candidate(id=cid, runners=hosts, min_seated=rng.choice([1, 2, 3]),
                                   max_attendees=rng.choice([None, None, 4, 8])))
        stars = {(u, c.id): rng.randint(1, 5) for u in people for c in cands
                 if rng.random() < 0.4}
        slots = [Slot(id=s, position=s, rooms={10: 8, 11: 6, 12: 10}) for s in (1, 2)]
        p = Problem(slots=slots, candidates=cands, present=frozenset(people), stars=stars)
        r = build_and_solve(p)
        assert r.status in ("OPTIMAL", "FEASIBLE")
        by_id = {c.id: c for c in cands}
        assert len(_placed(r)) == len(r.placements)          # each session runs once
        for pl in r.placements:
            c = by_id[pl.candidate_id]
            guests = pl.seated_guests
            assert guests >= c.min_seated
            assert guests <= pl.capacity
            for h in c.runners:
                assert h not in r.left_out[pl.slot_id]
