# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

### 1. Which indexes did you create and why?

I created a unique index on `employees.emp_code` to prevent duplicate employee records. I also used a unique compound index on `attendance_logs.emp_code` and `attendance_logs.date` to ensure that an employee has only one attendance record per day. Additional indexes support employee filtering and sorting, attendance date-range queries, and analytics aggregations. These indexes reduce query execution time and help avoid collection scans.

### 2. What happens when two identical punch-ins arrive simultaneously?

Both requests attempt to create attendance for the same employee and date. The unique compound index allows only one insert to succeed. The winning request receives `201 Created`, while the other request receives `409 Conflict`. This database-level constraint prevents duplicate attendance records even under concurrent requests.

### 3. What does the leaderboard return for a tie at the cutoff?

The leaderboard uses competition ranking. Employees with equal late-minute totals share the same rank, and the next rank is skipped. Ties are ordered by `emp_code` for deterministic results. The implementation must rank employees before applying the final limit so tied employees at the cutoff are handled consistently.

### 4. How does the department summary count employees with no logs?

Employee headcount is calculated from the employees collection, including employees who joined on or before the reporting period's end date. Attendance statistics are aggregated separately and combined with employee data, so employees without attendance logs are not excluded.

### 5. What would you change for 100 times the data?

I would review query plans and index usage, benchmark aggregation pipelines, and optimize frequently accessed queries. I would also consider pagination improvements, connection-pool tuning, caching suitable summary results, and MongoDB sharding if the measured workload and dataset size justified it.
