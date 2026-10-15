# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index protects employee identity even under concurrent creates. A unique `(emp_code, date)` index guarantees one attendance record per employee-day and makes punch-in races safe. Department/join-date and join-date indexes support headcount queries; attendance indexes support employee/month, date-sorted listing, status filters and punch-out lookup. I would avoid indexing every derived analytics field because it increases write cost without helping the main filters.

========================================================

2. **Punch-in race.** Both requests may initially see no record, but only one insert can satisfy the unique `(emp_code, date)` index. MongoDB rejects the other insert with a duplicate-key error, which the API translates to HTTP 409.

=========================================================

3. **Ties.** The leaderboard uses MongoDB competition ranking, so equal totals share a rank and the next rank is skipped. The limit filters ranks, not row count, so a tie at the cutoff is returned in full; employee code breaks display-order ties.

==========================================================

4. **Headcount.** The department summary starts with eligible employee documents and looks up their logs. Employees without logs therefore remain in the aggregation and count toward headcount.

==========================================================

5. **100x scale.** I would measure query plans and workload first, then consider pre-aggregated daily summaries and cursor pagination for very deep pages, while retaining the raw attendance audit trail.
