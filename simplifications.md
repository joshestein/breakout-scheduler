# Simplifications

Branch: `simplify-cuts` (from `origin/main`).

## Why

The app had about 28 organizer features. The tests check the placement shape, not who attends what. Most of the solver bugs we found happen where two features interact. For a retreat of about 30 people, we keep the core flow and remove the rest.

Result: app Python goes from 3,164 to 2,743 lines. The test suite goes from 93 to 76 tests. The removed tests covered only cut features.

## What we cut

| Feature | What it did | Why we cut it |
|---|---|---|
| Repeat sessions (`allow_repeat`) | A session could run up to 2 times. | Bug: the solver counted the same people at both runs. It dropped sessions that people wanted and showed 100% coverage. |
| Multi-slot sessions (`min_minutes`, `slots_needed`, `span`) | A long session used adjacent slots in one room. | Bug: people could leave halfway. The second half could have 0 people. Long sessions got double weight. |
| "Elsewhere" sessions (`needs_space`, roomless placements) | A session with no room, no seat cap. | Not needed. It added a sentinel room id (`ELSEWHERE = 0`), nullable `room_id`, and a separate grid column. |
| Per-slot room capacities (`timeslot_rooms.capacity`) | A room could have a different capacity in each slot. | Not needed. |
| Force-run (`must_run`) | A 100,000-point bonus forced a session onto the grid. | Not needed. "Place by hand" in the grid does the same. |
| Carry-over | Showed unplaced proposals from the last round to re-add. | Not needed. |
| Merge people | Folded a duplicate person into another. | Not needed. |
| Anonymous proposals | Hid the proposer's name. | Not needed. The anonymity could also be broken by logging in with the proposer's name. |
| Preview loop | A background job ran the solver every 3 s after each change. | No page read its result. It used CPU that "Generate" needs. |

## What we kept

- Propose, rate (1 to 5 stars), "I'm running this", descriptions.
- Per-proposal min and max attendees.
- Rooms with capacity, timeslots, rounds.
- Generate, fairness slider, pin, move, drop, place by hand.
- Merge duplicate *proposals*.
- Publish and unpublish, the drift warning, the public schedule, the projector view, the QR / join link, and Markdown copy.
- Slot clocks and push alerts.

## Deploy notes

1. **Back up the DB first.** The migration `f6a7b8c9d0e1` drops columns and deletes all roomless ("elsewhere") placements.
2. **Check that the live DB has an Alembic version.** DBs made by `create_all` (any boot without `RUN_MIGRATIONS=1`) have no `alembic_version` table. Run:
   ```bash
   sqlite3 /data/app.db "select version_num from alembic_version"
   ```
   - If the table exists, set `RUN_MIGRATIONS=1` and redeploy.
   - If the table is missing, and the `events` table has the `descriptions_enabled` column, stamp it first. Then set `RUN_MIGRATIONS=1` and redeploy:
     ```bash
     DB_PATH=/data/app.db python -m alembic stamp e5f6a7b8c9d0
     ```
3. After the deploy, set `RUN_MIGRATIONS=0` again.
4. Remove `PREVIEW_INTERVAL_S` from any `.env`. The app no longer reads it.

## Problems still open

Ranked by effect on a 30-person retreat.

| # | Problem | Evidence | Fix |
|---|---|---|---|
| 1 | **Sessions with only the host.** Every proposer is a host with weight 9, so the solver fills spare rooms with sessions that nobody else wants. It also takes the host away from a session they wanted. | Probe: a spare room plus a session with no ratings gives a session with 1 person. In a 30-person test, 2 of 20 sessions seated 2 people or fewer. | Give the host weight 0 in the objective. Add a rule: a session runs only if at least N people other than the host are seated. |
| 2 | **Strong ratings win.** One "must" (9) is worth nine "maybe"s (1 each). A person who rates everything 5 has 9 times the effect of a careful rater. | At λ=0, 1 person with a "must" beats 9 people with a "maybe". | Normalize ratings per person, or use flatter weights (for example 1/2/3). |
| 3 | **Minimum attendance checks ratings, not seats.** | `app/solver_input.py`: `interested_count` counts ratings of 3 or more. The solver has no seat minimum. | Add a rule: if a session runs, at least `min` people are seated. |
| 4 | **"Publish" leaks the draft.** The published grid is a snapshot, but the "copy as markdown" text is built from the live draft. That text is on `/s/{code}` and `/published`. | `tests/test_e2e.py::test_published_view_frozen_until_republish` fails. It failed before these cuts too. | Build the Markdown from the snapshot on attendee pages. |
| 5 | **Generate empties the grid when the solver finds no solution.** The stats then show 100% coverage. | `app/routers/admin.py` `solve` calls `apply_result` without checking the status. | Do not apply a result that is not OK. Show an error. Set `num_search_workers=8`. |
| 6 | **A session runs with no host if the host is marked absent.** | `app/solver_input.py` drops absent hosts but keeps the session. | Do not schedule a session if all its hosts are absent. |
| 7 | **Coverage hides people who did not rate.** A "maybe" counts as covered. | `app/solver_input.py`: `present` includes only people who rated a session 3 or higher, or host one. | Show the non-raters. Show coverage as "N of M people". |
| 8 | **Ties depend on proposal order.** | Probe: with identical ratings, the last proposal in the list wins. | Break ties on purpose: more people interested first, then the earlier proposal. |
| 9 | **The README install command fails.** `pip install -e ".[dev]"` gives a setuptools "multiple top-level packages" error. | Tested. | Set `[tool.setuptools] packages` in `pyproject.toml`, or tell people to install with `uv pip install -r`. |
| 10 | **Anyone can create events after the first one. Event codes are not unique.** | `app/routers/events.py` `create_event`. No unique constraint on codes. | Accepted for now. |
| 11 | **Identity is the display name only.** Two people with the same name share one account. | `app/routers/events.py` `enter`. | Accepted for now. |

## Next steps

1. Rewrite `app/solver.py` with attendance per session, a minimum seat count, and host weight 0 (problems 1, 3, 6).
2. Add invariant tests on simulated 30-person data. For every generated schedule, check these rules: nobody attends a session twice, no session has only its host, every running session has its host, and seats are not more than capacity.
3. Fix problems 4, 5 and 9.
4. Decide on the rating weights (problem 2) with real ratings from the retreat.
