# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.

# Code Review — `app/main.py`

## Defects Found

### 1. MongoDB connection is not checked by `/health`
- **Where:** `health()`
- **Problem:** Always returns `200 OK`, even when MongoDB is unavailable.
- **Test symptom:** Stop MongoDB and call `GET /health`; the endpoint still reports success.
- **Fix:** Ping MongoDB and return `503 Service Unavailable` if the database cannot be reached.

### 2. Incorrect database default
- **Where:** MongoDB initialization
- **Problem:** The starter defaults to `attendance_db`, whereas the configured project database is `employee_attendance`.
- **Test symptom:** The application connects to the wrong database when `MONGO_DB` is not configured.
- **Fix:** Read `MONGO_DB` from the environment and use the agreed database name as the local default.

### 3. Environment configuration can override deployment settings
- **Where:** `load_dotenv()`
- **Problem:** Configuration loading should explicitly preserve real environment-variable precedence.
- **Test symptom:** A local `.env` value may interfere with the intended deployment configuration if loading behavior changes.
- **Fix:** Use `load_dotenv(override=False)` and obtain connection settings from environment variables.

### 4. Employee creation has a race condition
- **Where:** `create_employee()`
- **Problem:** Checking for an existing employee before insertion is not atomic.
- **Test symptom:** Two simultaneous requests using the same `emp_code` may both pass the check.
- **Fix:** Create a unique MongoDB index on `emp_code` and handle duplicate-key errors as `409 Conflict`.

### 5. Employee creation does not validate business fields sufficiently
- **Where:** `EmployeeIn`
- **Problem:** The model accepts unrestricted strings for fields such as `emp_code`, `email`, `joined_on`, and shift times.
- **Test symptom:** Invalid dates, malformed email addresses, and invalid shift times may be accepted.
- **Fix:** Add field validation according to `openapi.yaml` and `DATA_MODEL.md`.

### 6. Employee timestamps are not handled consistently
- **Where:** `create_employee()`
- **Problem:** `created_at` uses a naive local `datetime.now()` value.
- **Test symptom:** Timestamp interpretation may differ across environments.
- **Fix:** Store UTC BSON datetimes and convert instants to integer epoch milliseconds at the API boundary.

### 7. Employee pagination uses the wrong offset
- **Where:** `list_employees()`
- **Problem:** `skip = page * page_size` skips the first page when `page=1`.
- **Test symptom:** The first request omits the first 20 employees.
- **Fix:** Use `(page - 1) * page_size`.

### 8. Employee total ignores filters
- **Where:** `list_employees()`
- **Problem:** `count_documents({})` counts every employee rather than employees matching the department filter.
- **Test symptom:** The returned `total` is incorrect when filtering by department.
- **Fix:** Count using the same query used to retrieve the items.

### 9. Pagination inputs are not bounded
- **Where:** `list_employees()` and `list_attendance()`
- **Problem:** Page numbers and page sizes are not validated against the assignment's limits.
- **Test symptom:** Zero or negative values and page sizes above 100 can produce invalid results.
- **Fix:** Enforce `page >= 1` and `1 <= page_size <= 100`, with a default page size of 20.

### 10. Late-minute calculation mishandles early arrivals
- **Where:** `compute_late_minutes()`
- **Problem:** The calculation assumes the punch-in and shift start are on the same calendar date and does not explicitly apply the assignment's time-zone and whole-second rules.
- **Test symptom:** Overnight shifts or timestamps with fractional seconds can produce incorrect results.
- **Fix:** Calculate the shift start using the correct IST calendar date, truncate instants to whole seconds, and apply the strict 10-minute grace rule.

### 11. Work-hour rounding does not guarantee half-up rounding
- **Where:** `compute_work_hours()`
- **Problem:** Python's built-in `round()` uses ties-to-even rounding, which differs from the assignment's required half-up rounding.
- **Test symptom:** Boundary values can produce a different result from the expected two-decimal value.
- **Fix:** Use `Decimal` with `ROUND_HALF_UP`.

### 12. Overtime calculation ignores the minimum threshold
- **Where:** `compute_overtime()`
- **Problem:** Any positive overtime is counted, even when it is less than 30 minutes.
- **Test symptom:** A punch-out 20 minutes after shift end returns 20 instead of 0.
- **Fix:** Count whole overtime minutes only when the duration is at least 30 minutes, accounting for overnight shifts.

### 13. Punch-in does not reject unknown employees
- **Where:** `punch_in()`
- **Problem:** The code does not check whether `emp` is `None` before reading `emp["shift_start"]`.
- **Test symptom:** Punching in with an unknown employee code can produce an internal server error instead of `404`.
- **Fix:** Return `404 Not Found` when the employee does not exist.

### 14. Punch-in mishandles timestamps and status validation
- **Where:** `PunchInIn` and `punch_in()`
- **Problem:** The timestamp is interpreted using the machine's local time zone, and zero or invalid values can silently trigger the current-time fallback. Status values are unrestricted strings.
- **Test symptom:** Invalid timestamps may be accepted, and unsupported statuses may be stored.
- **Fix:** Require a valid integer epoch-millisecond timestamp when supplied, convert it to UTC/IST explicitly, truncate to whole seconds, and validate the permitted statuses.

### 15. Punch-in uniqueness is not concurrency-safe
- **Where:** `punch_in()`
- **Problem:** A read-before-insert check cannot prevent concurrent duplicate attendance records.
- **Test symptom:** Parallel punch-in requests for the same employee and date can create duplicate records.
- **Fix:** Create a unique compound index on `(emp_code, date)` and map duplicate-key errors to `409 Conflict`.

### 16. Punch-in returns an ID that the API contract forbids
- **Where:** `punch_in()`
- **Problem:** The response includes an `id` derived from MongoDB's `_id`.
- **Test symptom:** The response exposes an identifier that the assignment says must not appear in the API.
- **Fix:** Remove both `_id` and the synthetic `id` from responses.

### 17. Attendance listing fetches and sorts the entire result set in Python
- **Where:** `list_attendance()`
- **Problem:** It loads every matching document into memory, sorts them in Python, and only then paginates.
- **Test symptom:** Performance and memory usage deteriorate as attendance records grow toward 100,000.
- **Fix:** Use indexed MongoDB filtering, sorting, counting, `skip`, and `limit`.

### 18. Attendance total ignores the intended filtered query in its current implementation
- **Where:** `list_attendance()`
- **Problem:** Although the current count uses the filtered in-memory documents, this requires loading the entire result set and is not an efficient database count.
- **Test symptom:** Large filtered queries become unnecessarily slow.
- **Fix:** Use `count_documents(q)` and apply database-side pagination.

### 19. Attendance records can leak internal MongoDB fields
- **Where:** `list_attendance()`
- **Problem:** The query retrieves `_id` and other stored fields without an explicit response contract.
- **Test symptom:** Internal identifiers or unexpected fields can appear in the response.
- **Fix:** Explicitly serialize responses according to the API contract and omit `_id`.

### 20. Required endpoints are missing
- **Where:** End of `app/main.py`
- **Problem:** Punch-out, attendance regularization, monthly employee analytics, department summary, late leaderboard, department trend, and explain endpoints are not implemented.
- **Test symptom:** Requests to these documented routes return `404 Not Found`.
- **Fix:** Implement every endpoint defined in `openapi.yaml`, including the required MongoDB aggregation pipelines, audit history, concurrency safeguards, and execution-statistics explain output.

## Areas examined and retained for further verification

- The application uses FastAPI, Pydantic, and PyMongo, which are suitable for the required stack.
- The attendance filters include employee code, date range, and status, but require indexed database-side execution and contract validation.
- The helper functions are reusable after correcting their time-zone, rounding, and overnight-shift behavior.
- All fixes must be verified against the specified business rules and automated tests.

