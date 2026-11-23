from app.types import WEIGHT, INTERESTED_STARS, Problem, Candidate, Slot


def test_weight_table_is_single_source_of_truth():
    # 1-2 star = won't attend (zero weight); enthusiasm ramps up
    assert WEIGHT == {1: 0, 2: 0, 3: 1, 4: 4, 5: 9}
    assert INTERESTED_STARS == 3


def test_problem_is_constructible():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 8})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5},
        lam=3.0,
        min_interest=1,
    )
    assert p.lam == 3.0
    assert p.rooms_for(1) == {10: 8}
