# REVIEW.md — starter code review

| # | Where | What is wrong | How to notice | Fix applied |

===========================================================

 1. `compute_late_minutes`:- It subtracts shift start on the punch-in's calendar date, not the attendance date; overnight shifts are wrong. A shift 22:00–06:00 with a punch-in after midnight gets a nonsensical late value. Anchor shift start to the attendance date in IST.

===========================================================

 2. `compute_late_minutes`:- It discards the grace-period rule incorrectly if elapsed time is fractional/seconds and does not truncate the instant first. Compare 09:40:00.900 and 09:40:01 for a 09:30 shift. Truncate instants to seconds and apply strict `> 600` seconds, while reporting whole minutes from
  shift start.

===========================================================

 3. `compute_work_hours`:- Python `round` uses ties-to-even and float arithmetic. Durations at a half-centesimal boundary can round down unexpectedly. Use `Decimal` and `ROUND_HALF_UP`.

===========================================================

 4. `compute_overtime`:- It anchors shift end to a date without consistently handling overnight shifts.Punch-out at 06:40 after a 22:00–06:00 shift is compared to the wrong day. Calculate shift end on the next day when the shift is overnight.

===========================================================

 5. `/health`:- It always returns healthy without pinging MongoDB. Stop MongoDB and call `/health`; starter still returns 2 Ping MongoDB and return 503 when it is unavailable.

===========================================================

 6. `create_employee`:- A check-then-insert alone cannot prevent concurrent duplicate employee codes; shift times can be identical. Send parallel POST requests with the same code or set both shift times to 09:30. Add a unique database index, map duplicate-key errors to 409, and validate different sh
 ift times.

===========================================================

 7. `create_employee`:- `created_at` is a naive local datetime rather than an explicit UTC instant returned as epoch milliseconds. Compare results across machines/time zones. Store UTC BSON datetimes and serialize instants as epoch milliseconds.

===========================================================

 8. `list_employees` :-`skip = page * page_size` skips the first page. | Page 1 with size 20 starts at record 21. Use `(page - 1) * page_size`.

===========================================================

 9. `list_employees` :-  `total` counts all employees, not the filtered department. Filter by a department and compare `total` to the number of matching records. Count with the same query as the page.

===========================================================

 10. `list_employees`:-  Results are not sorted by `emp_code`, and page/page-size constraints are missing. Repeated calls may return unstable ordering; page 0 or size 1000 is accepted. Sort ascending and validate page >= 1 and page_size 1–100.

===========================================================

 11. `punch_in`:- It dereferences an unknown employee and can return a 500; status is not restricted. Punch in using an unknown code or `ABSENT`.Return 404 and accept only pre
 sence statuses.

===========================================================

 12. `punch_in`:- Check-then-insert races; timestamp units, timezone, sub-second truncation and overnight attendance date are mishandled. Parallel same-day requests or epoch seconds instead of milliseconds. Validate epoch milliseconds, truncate to seconds, calculate IST attendance date, and rely on a uniq
 ue `(emp_code,date)` index. 

===========================================================

 13. `list_attendance`:- It loads the entire matching collection into Python and paginates in memory. Large dataset causes slow responses and high memory use. Count and page in MongoDB with indexed filters, sort, skip and limit.

===========================================================

 14. `list_attendance`:- Date range order and page limits are not validated; response leaks MongoDB identity by renaming `_id` to `id`, even though records are addressed by `(emp_code, date)`. `date_from > date_to`, page 0, or inspect response fields. Validate parameters, sort by date descending the
 n employee code ascending, and omit `_id`.

===========================================================

 15. Missing endpoints:- Punch-out, regularization, monthly/department analytics, leaderboard, trend and explain are absent. Contract calls return 404. | Implement all seven routes an
 d their core database logic.

===========================================================

 16. All endpoints / startup Required indexes are not created, and database errors can leak as 500s or hide readiness failures. | Inspect indexes and run explain against a large collection. Create idempotent unique and query indexes on startup; health checks the database.

## Reviewed and not treated as defects

- `emp_code` is the business identifier; MongoDB `_id` should not be exposed or used as the API identifier.
=============================================================
- Calendar dates are strings in `YYYY-MM-DD` format, while instants are BSON datetimes internally and epoch milliseconds at the API boundary.
=============================================================
- `history` is reserved for manual PATCH corrections; normal punch-in and punch-out operations should not append audit entries.
