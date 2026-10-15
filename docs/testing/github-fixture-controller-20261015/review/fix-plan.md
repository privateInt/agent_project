# C01-P2-v2 post-record disclosure correction

Minor D29 delta to C01-D29-v1. Original plan and first final review remain unchanged.
Independent finding: review/final-review.md, P2, controller final artifact restoration.
The active user requires preserving all results and not repeating passed tests.

Proposed implementation: one shared, fixed no-disclosure reason set containing
ASSIGNMENT_PIN_MISMATCH, ASSIGNMENT_CONFLICT, STATE_CONFLICT. Use it in existing
_result redaction and in the final post-record artifact-restoration guard. No
other API, state, reason, SQL, claim or collector/ledger behavior changes.

Three narrowly focused new regression methods only:
1. Real fixed collection and successful record, then test-owned control pin
   changes before final read. Actual read returns BLOCKED/STATE_CONFLICT and
   run must retain record=None/artifact=None. Preserve the conflicting DB rows.
2. Same path but test-owned control row becomes PREPARATION_FAILED with a valid
   reason after successful record. Actual read rejects inconsistent state;
   run returns no record/artifact and does not repair the DBs.
3. Successful artifact record, then an injected control read COMMIT response
   error on the final read. Run returns DATABASE_UNCONFIRMED, keeps original
   collected artifact bytes and withholds storage confirmation. Independent
   reopened read after removing the fault observes exact stored bytes/hash.
   This directly covers preservation in the changed restoration branch, without
   rerunning already passed C09's record-COMMIT cases.

Execute only the two NEW conflict targets on the old source once to retain
semantic reproduction of P2 (FAIL, not import/environment ERROR). Then make the
approved guard fix and run those two failed targets plus the uncertainty target
for its FIRST execution. This avoids repeating even a passed positive subcase.
Keep both logs/pins. Never rerun
passed C01-C12 methods, old R03/R04/R15 or either whole suite. Existing 13-method
and D38 records remain attributed to their original pins. The changed guard is
outside M1/M2 paths; reviewer must assess scoped evidence reuse, no mutation rerun.

Test changes append three methods and one shared fixture helper only; all
original methods remain byte-for-byte unchanged.
Code and fixtures stay within /tmp/agent-controller-dev-ur5octch. No credentials,
operational settings, GitHub access, head work or sandbox diagnostics.

Independent reviewer approves this exact delta SHA before source changes and
later reviews the final diff/current pins, initial failure and focused success.
