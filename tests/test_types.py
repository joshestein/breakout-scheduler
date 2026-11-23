from app.types import WEIGHT, INTERESTED_STARS, Problem, Candidate, Slot


def test_weight_table_is_single_source_of_truth():
    # 1-2 star = won't attend (zero weight); linear points above that
    assert WEIGHT == {1: 0, 2: 0, 3: 1, 4: 2, 5: 3}
    assert INTERESTED_STARS == 3


def test_problem_is_constructible():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 8})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5},
    )
    assert p.rooms_for(1) == {10: 8}
