# DECISIONS.md

I added a unique index on employee code so two people cannot be created with the same code. Employees also have indexes for department listings and joined dates. Attendance has a unique `(emp_code, date)` index, which stops duplicate punch-ins. Other attendance indexes support the date list, employee monthly report, and trend date range. I did not add separate indexes for every status because the main filters and sort orders are more useful.

If two identical punch-ins arrive together, both can find the employee. The first insert creates the attendance record. The second insert hits the unique index, so the code catches that database error and returns 409 instead of adding another record.

The late leaderboard uses normal competition ranking. If two employees tie at the limit, both are returned with the same rank. The next rank is skipped. This is fair because the limit is based on rank, not only the number of rows.

The department summary starts with employees and looks up their logs. It keeps employees even when the lookup has no result, so zero-log employees still count in headcount.

For 100 times more data, I would make pre-calculated daily and monthly reporting collections, updated when attendance changes.
