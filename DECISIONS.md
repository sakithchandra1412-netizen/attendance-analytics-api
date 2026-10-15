# DECISIONS.md

1. **Indexes.**
   On `employees`, `{emp_code: 1}` (unique) guarantees uniqueness, while `{department: 1, joined_on: 1}` backs headcount filtering. On `attendance_logs`, `{emp_code: 1, date: 1}` (unique) prevents duplicate punches and speeds monthly queries; `{emp_code: 1, punch_in: -1}` optimizes punch-out; `{date: -1, emp_code: 1}` backs attendance pagination; `{date: 1, late_minutes: 1, emp_code: 1}` powers late rankings. An index on `{status: 1}` alone was considered and rejected due to low cardinality, which causes inefficient scans compared to compound keys.

2. **Punch-in race.**
   Both requests compute the attendance date concurrently and attempt `insert_one()`. At the storage layer, one insert commits first and returns 201. The second insert violates the unique compound index on `(emp_code, date)`, triggering a `DuplicateKeyError`. The exception handler catches this and returns 409.

3. **Ties.**
   Using MongoDB's `$rank` window function (standard competition ranking), tied employees share a rank. Filtering occurs after ranking (`rank <= limit`), returning all tied employees at the cutoff (more than `limit` rows), ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation begins on `employees` filtered by `joined_on <= period_end`. It `$lookup`s attendance logs and `$unwind`s with `preserveNullAndEmptyArrays: true`. Employees without logs are retained and counted in headcount via `{$size: "$employees"}`, contributing zero to log aggregates.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily and monthly attendance metrics into a summary collection using MongoDB Change Streams or write-time updates, replacing heavy cross-collection aggregations with instant index point lookups.
