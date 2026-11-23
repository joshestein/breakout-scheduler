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
| Fairness slider (λ) | Traded interest points against people left out. | Nobody could reason about its units. The solver now puts coverage first. |
| Rooms per slot (`timeslot_rooms`) | Each timeslot had its own set of rooms. | Not needed. Every room is used in every slot. |
| Present/away toggle (`round_participants`) | Marked people as away for a round. | Not needed at retreat size. Remove a session whose host left. |
| Merge proposals (`merged_into`) | Folded a duplicate proposal into another. | Not used. Remove the duplicate. |
| Admin min/max form | Let the admin override a session's min and max. | The proposer already sets them. |
| Descriptions toggle | Let the admin turn descriptions off. | Descriptions are always allowed. |
| Publish snapshot, drift warning, archive page | Froze the published grid; attendees saw the copy until re-publish. | One live grid is simpler, and the Markdown copy leaked the draft (problem 4). Past rounds stay in the console. |
| Focus mode, sparkline | Layout toggle; tiny rating histogram. | Noise. |

## What we kept

- Propose, rate (1 to 5 stars), "I'm running this", descriptions.
- Per-proposal min and max attendees.
- Rooms with capacity, timeslots, rounds.
- Generate, pin, place by hand (swap and move), remove and restore.
- Publish and unpublish, the public schedule, the projector view, the QR / join link, and Markdown copy.
- Slot clocks and push alerts.

## Deploy notes

1. **Start from an empty DB.** All migrations are squashed into one initial migration (`5433f972a0e2`). A DB stamped with an older revision cannot upgrade. Delete it, or move it away.
2. Remove `PREVIEW_INTERVAL_S` from any `.env`. The app no longer reads it.
3. Migration `b1afe033c5d9` drops `timeslot_rooms`, `round_participants`, `round_snapshots`, `events.descriptions_enabled`, `proposals.merged_into`, and `rounds.lambda_fairness`. Back up first, then deploy with `RUN_MIGRATIONS=1`.

## Problems still open

Ranked by effect on a 30-person retreat.

| # | Problem | Evidence | Fix |
|---|---|---|---|
| 1 | **Fixed:** sessions with only the host. A host now gets 0 points for their own session, and a session runs only if at least N people besides its hosts are seated. | `tests/test_solver_rules.py` | |
| 2 | **Fixed:** strong ratings won. Points are now ★3 = 1, ★4 = 2, ★5 = 3, and coverage comes before points. | `tests/test_types.py` | Check with real ratings from a retreat. |
| 3 | **Fixed:** minimum attendance checked ratings, not seats. The solver now requires the seats. | `tests/test_solver_rules.py::test_floor_counts_seated_guests_not_ratings` | |
| 4 | **Fixed:** "Publish" leaked the draft through the Markdown copy. Snapshots are gone: attendees see the live grid. | `tests/test_e2e.py::test_published_view_shows_live_grid` | |
| 5 | **Fixed:** Generate emptied the grid when the solver found no solution. `apply_result` now refuses a result that is not OK, and the admin sees an error. | `tests/test_services.py::test_apply_result_rejects_infeasible_and_keeps_grid` | |
| 6 | **Gone:** a session ran with no host if the host was marked absent. The present/away toggle is cut. | | |
| 7 | **Fixed:** coverage hid people who did not rate. The console lists them and shows coverage as "N of M have a session". A "maybe" still counts as covered. | `tests/test_e2e.py::test_console_explains_cards_and_unplaced_sessions` | |
| 8 | **Fixed:** ties depended on proposal order by accident. Now the earlier proposal wins on purpose. | `tests/test_solver_rules.py::test_tie_goes_to_earlier_proposal` | |
| 9 | **The README install command fails.** `pip install -e ".[dev]"` gives a setuptools "multiple top-level packages" error. | Tested. | Set `[tool.setuptools] packages` in `pyproject.toml`, or tell people to install with `uv pip install -r`. |
| 10 | **Anyone can create events after the first one. Event codes are not unique.** | `app/routers/events.py` `create_event`. No unique constraint on codes. | Accepted for now. |
| 11 | **Identity is the display name only.** Two people with the same name share one account. | `app/routers/events.py` `enter`. | Accepted for now. |

## Next steps

1. Fix problem 9.
2. Run a real retreat and see which admin controls are used.
3. Check the rating points (problem 2) with real ratings from a retreat.
