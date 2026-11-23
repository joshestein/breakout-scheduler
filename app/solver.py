from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from app.types import WEIGHT, INTERESTED_STARS, Problem, Result, PlacementOut

MAX_TIME_S = 4.0


def _weight(problem: Problem, u: int, cid: int, runners: frozenset[int]) -> int:
    """Points for seating `u` in `cid`. A host earns nothing for their own session, so
    a session never runs just because its host is keen on it."""
    if u in runners:
        return 0
    return WEIGHT.get(problem.stars.get((u, cid), 0), 0)


def _new_solver() -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_TIME_S
    solver.parameters.random_seed = 1
    solver.parameters.num_search_workers = 8
    return solver


def build_and_solve(problem: Problem) -> Result:
    m = cp_model.CpModel()
    slots = sorted(problem.slots, key=lambda s: s.position)
    runner_of = {c.id: c.runners for c in problem.candidates}

    # place[(cid, slot, room)]: the session runs in this room in this slot
    place: dict[tuple[int, int, int], cp_model.IntVar] = {}
    for c in problem.candidates:
        for s in slots:
            for r in s.rooms:
                place[(c.id, s.id, r)] = m.new_bool_var(f"place_{c.id}_{s.id}_{r}")

    # rooms_of[(cid, slot)] -> [(room_id, var)]: where the session could run in that slot
    rooms_of: dict[tuple[int, int], list[tuple[int, cp_model.IntVar]]] = defaultdict(list)
    for (cid, sid, r), v in place.items():
        rooms_of[(cid, sid)].append((r, v))

    def placed_in(cid: int, slot_id: int):
        return sum(v for _, v in rooms_of[(cid, slot_id)])

    # fixed-grid mode: freeze every placement var to the given grid (score as-is)
    if problem.fixed is not None:
        for key, var in place.items():
            m.add(var == (1 if key in problem.fixed else 0))

    # each session runs at most once
    for c in problem.candidates:
        runs = [v for (cid, _, _), v in place.items() if cid == c.id]
        if runs:
            m.add(sum(runs) <= 1)

    # one session per (room, slot)
    for s in slots:
        for room_id in s.rooms:
            m.add(sum(place[(c.id, s.id, room_id)] for c in problem.candidates) <= 1)

    # attend[(u, cid, slot)] gated: interested (>=3) or runner, only for present people
    attend: dict[tuple[int, int, int], cp_model.IntVar] = {}
    for c in problem.candidates:
        for u in problem.present:
            interested = problem.stars.get((u, c.id), 0) >= INTERESTED_STARS
            is_runner = u in runner_of[c.id]
            if not (interested or is_runner):
                continue
            for s in slots:
                if not rooms_of[(c.id, s.id)]:
                    continue
                v = m.new_bool_var(f"att_{u}_{c.id}_{s.id}")
                attend[(u, c.id, s.id)] = v
                if is_runner:
                    m.add(v == placed_in(c.id, s.id))   # runner present exactly when it runs
                else:
                    m.add(v <= placed_in(c.id, s.id))

    def attending(cid: int, slot_id: int, *, include_runners: bool = True):
        return [attend[(u, cid, slot_id)] for u in problem.present
                if (u, cid, slot_id) in attend
                and (include_runners or u not in runner_of[cid])]

    # one place per person per slot
    for u in problem.present:
        for s in slots:
            here = [attend[(u, c.id, s.id)] for c in problem.candidates
                    if (u, c.id, s.id) in attend]
            if here:
                m.add(sum(here) <= 1)

    # hard seat cap (non-runners) so attendance cannot exceed the placed room's seats
    for c in problem.candidates:
        for s in slots:
            non_runner = attending(c.id, s.id, include_runners=False)
            if non_runner:
                m.add(sum(non_runner) <= sum(s.rooms[r] * v for r, v in rooms_of[(c.id, s.id)]))

    # host's per-session cap on people besides the hosts (same basis as room seats and
    # the floor). Overflow people are routed to their next-best session by the
    # coverage objective. Hosts always fit.
    for c in problem.candidates:
        if c.max_attendees is None:
            continue
        for s in slots:
            guests = attending(c.id, s.id, include_runners=False)
            if guests:
                m.add(sum(guests) <= c.max_attendees)

    # uncovered[(u, slot)] >= 1 - sum_c attend
    uncovered: dict[tuple[int, int], cp_model.IntVar] = {}
    for u in problem.present:
        for s in slots:
            uv = m.new_bool_var(f"unc_{u}_{s.id}")
            uncovered[(u, s.id)] = uv
            here = [attend[(u, c.id, s.id)] for c in problem.candidates
                    if (u, c.id, s.id) in attend]
            m.add(uv >= 1 - sum(here)) if here else m.add(uv == 1)

    # pins (validated): fix place==1 where the key exists, else report conflict
    pin_conflicts: list[tuple[int, int, int]] = []
    for key in problem.pins:
        if key in place:
            m.add(place[key] == 1)
        else:
            pin_conflicts.append(key)

    # floor: a running session seats at least `min_seated` non-hosts. Pinned sessions
    # (an admin's explicit choice) and fixed-grid scoring are exempt, so a hand edit can
    # never make the model infeasible.
    pinned_cids = {cid for (cid, _, _) in problem.pins}
    if problem.fixed is None:
        for c in problem.candidates:
            if c.id in pinned_cids:
                continue
            floor = max(1, c.min_seated)
            for s in slots:
                if not rooms_of[(c.id, s.id)]:
                    continue
                guests = attending(c.id, s.id, include_runners=False)
                m.add(sum(guests) >= floor * placed_in(c.id, s.id))

    # Objective, in strict priority order:
    #   1. leave as few people as possible with nothing to attend in a slot;
    #   2. then maximise interest points of the people seated;
    #   3. then, on exact ties, prefer the earlier proposal.
    # One weighted sum: each level is scaled past the full range of the levels below it,
    # so a lower level can never outweigh a higher one.
    n_uncovered = sum(uncovered.values())
    interest = sum(_weight(problem, u, cid, runner_of[cid]) * v
                   for (u, cid, _sid), v in attend.items())
    rank = {c.id: i for i, c in enumerate(sorted(problem.candidates, key=lambda c: c.id))}
    tie_scale = sum(rank.values()) + 1   # > the largest possible tie-break penalty
    tie_penalty = sum(rank[cid] * v for (cid, _, _), v in place.items())
    # each person earns at most the top weight once per slot
    max_interest = max(WEIGHT.values()) * len(problem.present) * len(slots)
    coverage_scale = (max_interest + 1) * tie_scale

    m.maximize(interest * tie_scale - tie_penalty - coverage_scale * n_uncovered)
    solver = _new_solver()
    status = solver.solve(m)
    ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    status_name = {cp_model.OPTIMAL: "OPTIMAL",
                   cp_model.FEASIBLE: "FEASIBLE"}.get(status, "INFEASIBLE")
    val = solver.value

    max_of = {c.id: c.max_attendees for c in problem.candidates}

    def cell_cap(cid: int, slot_id: int, room_id: int) -> int:
        """Effective seats for people besides the hosts: the room, lowered by the
        host max if set."""
        cap = problem.rooms_for(slot_id)[room_id]
        if max_of.get(cid) is not None:
            cap = min(cap, max_of[cid])
        return cap

    placements: list[PlacementOut] = []
    total_interest = 0
    seated: set[tuple[int, int]] = set()
    if ok:
        for (cid, slot_id, room_id), v in place.items():
            if val(v) != 1:
                continue
            demand = sum(1 for u in problem.present
                         if problem.stars.get((u, cid), 0) >= INTERESTED_STARS
                         and u not in runner_of[cid])
            cap = cell_cap(cid, slot_id, room_id)
            placements.append(PlacementOut(
                candidate_id=cid, slot_id=slot_id, room_id=room_id,
                seated_guests=sum(val(a) for a in attending(cid, slot_id, include_runners=False)),
                capacity=cap, overflow=max(0, demand - cap)))
        seated = {(u, cid) for (u, cid, _sid), var in attend.items() if val(var) == 1}
        total_interest = sum(_weight(problem, u, cid, runner_of[cid]) for (u, cid) in seated)

    # % of present people who got into one of their MOST preferred (max-star) sessions
    top_choice_pct = 0.0
    if ok and problem.present:
        seated_by_user: dict[int, set[int]] = defaultdict(set)
        for (u, cid) in seated:
            seated_by_user[u].add(cid)
        best_star: dict[int, int] = {}
        top_cids: dict[int, set[int]] = defaultdict(set)
        for (u, cid), st in problem.stars.items():
            if u not in problem.present:
                continue
            if st > best_star.get(u, 0):
                best_star[u] = st
                top_cids[u] = {cid}
            elif st == best_star.get(u, 0):
                top_cids[u].add(cid)
        hits = sum(1 for u in problem.present
                   if best_star.get(u, 0) >= INTERESTED_STARS
                   and seated_by_user[u] & top_cids[u])
        top_choice_pct = round(100.0 * hits / len(problem.present), 1)

    coverage_pct: dict[int, float] = {}
    left_out: dict[int, list[int]] = {}
    forced: dict[int, list[int]] = {}
    n_present = max(1, len(problem.present))
    for s in slots:
        unc_here = sorted(u for u in problem.present
                          if ok and val(uncovered[(u, s.id)]) == 1)
        left_out[s.id] = unc_here
        coverage_pct[s.id] = round(
            100.0 * (len(problem.present) - len(unc_here)) / n_present, 1)
        placed_cids = {pl.candidate_id for pl in placements if pl.slot_id == s.id}
        forced[s.id] = sorted(
            u for u in unc_here
            if any(problem.stars.get((u, cid), 0) >= INTERESTED_STARS
                   for cid in placed_cids))

    return Result(status=status_name, placements=placements,
                  total_interest=total_interest, n_present=len(problem.present),
                  coverage_pct=coverage_pct, left_out=left_out,
                  forced_uncovered=forced, pin_conflicts=pin_conflicts,
                  top_choice_pct=top_choice_pct)
