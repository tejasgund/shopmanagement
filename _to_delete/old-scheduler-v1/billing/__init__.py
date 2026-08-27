"""
The business rules the scheduler's tasks apply.

These moved here from the application when the scheduler was made standalone.
The scheduler owns them now, and the app imports them for its manual
"Generate rent now" button - one definition, and the dependency runs
app -> scheduler so this folder stays independently runnable.

Nothing in here knows about scheduling. `rent.py` knows what a rent bill is;
`penalty.py` knows what a late fee is; neither knows when it runs, what a task
row is, or that a scheduler exists. That is what lets a task be retried,
backfilled or run by hand without any of it leaking into the rules.
"""
