# REVIEW.md

| # | Where | What was wrong | How I noticed it | Fix |
|---|---|---|---|---|
| 1 | `health()` | It always returned OK even if MongoDB was down. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 on error. |
| 2 | `create_employee()` | `created_at` used a local, timezone-free datetime. | The stored time can mean different things on different servers. | Store `datetime.now(timezone.utc)`. |
| 3 | `create_employee()` | The response returned a datetime instead of epoch milliseconds. | It did not match the API schema. | Convert response datetimes with `to_epoch_ms()`. |
| 4 | `list_employees()` | Page 1 skipped the first set of employees. | With page size 20, page 1 started at employee 21. | Use `(page - 1) * page_size`. |
| 5 | `list_employees()` | The total ignored the department filter. | Filtering a small department still showed every employee. | Count using the same query object. |
| 6 | `list_employees()` | There was no fixed employee-code sort. | Result order could change between calls. | Sort by `emp_code` ascending in MongoDB. |
| 7 | `punch_in()` | The date calculation depended on the server timezone. | A late-night UTC punch could get the wrong IST date. | Read timestamps as UTC and convert using IST rules. |
| 8 | `punch_in()` | Unknown employees caused a server error. | Sending an unused employee code gave a 500. | Check for the employee and return 404. |
| 9 | `punch_in()` | Two simultaneous requests could create two records. | Both requests could pass a normal duplicate check. | Add a unique `(emp_code, date)` index and catch `DuplicateKeyError`. |
| 10 | `compute_late_minutes()` | Shift time was compared without properly handling IST. | Late minutes were wrong when the server timezone was not IST. | Build the shift start as an IST datetime. |
| 11 | `compute_overtime_minutes()` | Overnight shift end was put on the wrong day. | An overnight employee could get incorrect overtime. | Move the end time to the next date for overnight shifts. |
| 12 | `list_attendance()` | It loaded the full collection before paging and sorting. | This becomes slow or runs out of memory on many logs. | Use MongoDB sort, skip, and limit. |
| 13 | `punch_out()` | A second simultaneous punch-out could look like a missing record. | One request succeeded and the other could return 404. | Check for the closed record and return 409 for that race. |

I also checked the half-day threshold. The comparison is intentionally strict: exactly 4.50 hours is not a half day.
