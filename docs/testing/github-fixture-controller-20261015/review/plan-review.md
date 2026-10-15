# C01 independent pre-implementation review

Verdict: **PASS** for `C01-D29-v1` with the frozen contract resolutions below.

- Reviewed plan: `review/test-plan.md`
- Exact plan SHA-256: `227df66ea111e1d29e5982290630deac9b3b40b9e034e064a08283540142962e`
- Requirements SHA-256: `0c10da556e6ee01f689d6c74f5a8d1fe7d2736c2e74cf115bc82fdf38219778f`
- Reviewer role: independent reviewer; no production or test code authored or executed.
- Source parent: `e1eff75b47cdf3fae1088d51d35aaea701f81477`. The four workspace source/test hashes match `source-provenance.json`.
- Scope: isolated fixture-controller component. This is approval to implement the reviewed component, not final implementation acceptance, protected installation, head execution, native sandbox acceptance, signing, App publication or merging.

## Review conclusion

C01-C12 cover the changed controller boundary rather than repeating the previously accepted ledger/collector cases. Actual file-backed SQLite, real fixed collector cases, acknowledged durable claims, process interruption/racing, three distinct COMMIT-loss stages and exact row/readback preservation are necessary for D30/D32. Fault injection remains separately attributed. Connection/schema/input denial and preservation checks supply the scoped D35 evidence; they do not attest protected state origin. D29 requires final review of the actual pinned source, new tests and results after implementation. No existing suites or held sandbox diagnostics should run.

No missing prerequisite requires new permissions for this isolated development component. The following decisions resolve the plan's proposed-contract gaps before implementation; they are normative together with the exact reviewed plan. Freeze the concise workspace contract to these values before implementing source. If implementation needs a different public contract or removes a required case, obtain a delta review first.

## Frozen API, schema and result contract

Public API: exactly the three functions/signatures in the plan. No callback, path, connection factory, retry/reset, CLI or arbitrary executor argument. Reuse unchanged collector and ledger APIs; do not extend ledger schema1 or change existing sources/tests. The controller receives two distinct caller-owned connections and validates both prerequisites before accepting work. Foreign/uninitialized schema cannot be silently initialized by run/read. No live transaction or DB lock crosses collection.

Controller application ID: `0x41504643` / `1095779907`; user_version `1`. One `WITHOUT ROWID` table only, with this exact DDL:

```sql
CREATE TABLE fixture_controller_jobs (job_id TEXT NOT NULL PRIMARY KEY, assignment_sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('CLAIMED','PREPARATION_FAILED')), preparation_reason TEXT, CHECK ((state='CLAIMED' AND preparation_reason IS NULL) OR (state='PREPARATION_FAILED' AND preparation_reason IS NOT NULL AND preparation_reason IN ('ARGUMENTS_INVALID','STATE_FD_INVALID','ASSIGNMENT_FILE_INVALID','ASSIGNMENT_HASH_MISMATCH','ASSIGNMENT_INVALID','REAL_EXECUTION_BLOCKED')))) WITHOUT ROWID
```

The explicit `preparation_reason IS NOT NULL` is required: SQLite CHECK accepts NULL, so an IN-only expression would not enforce terminal-state reason presence. Enforce exact job/pin/type/length/state/reason validation on reads as well. Inspect schema/row sizes with bounded SQL before materializing malformed text/blobs, and close cursors. No migrations, deletes, state resets, automatic schema repair or borrowed-connection cleanup. Caller-supplied connection callbacks/path/ownership remain outside this component's authenticity claim.

Accept the proposed exact `{report, record, artifact}` result and the plan's exact 20 report keys. `record` is the original validated ledger record or None. `artifact` is original collected/stored strict bytes or None; preserve the collected bytes on uncertain artifact-record outcome. Do not serialize bytes or private inputs into the report or stdout/stderr. Rejected job/pin or inconsistent-state requests return no artifact/record disclosure.

Report scope: `FIXTURE_CONTROLLER_ONLY`. Status is OBSERVED only for `CONTROLLER_READY` and `ARTIFACT_STORED`; every other reason is BLOCKED. `control_state` is None, CLAIMED or PREPARATION_FAILED. `preparation_reason` is None or one of the six preparation-whitelist literals. `collection_reason` is None or a structurally validated pinned-collector reason; unknown/injected text is not forwarded. Strict boolean fields and all nine fixed false trust flags follow the plan. `artifact_storage_confirmed` means validated stored bytes, never child/test/reviewer success. Return each connection's actual transaction-closed state independently.

Freeze the plan's 16 public reason literals unchanged. Map stored-row corruption/incompatible cross-DB state to STATE_CONFLICT. Do not forward an arbitrary ledger/collector exception or reason outside this allowlist. Infrastructure write/response uncertainty is DATABASE_UNCONFIRMED; unconfirmed execution or a remaining claim without a stored artifact is OUTCOME_UNKNOWN. Validate actual child/no-output fields before recognizing a preparation outcome; artifact=None or status=BLOCKED alone is insufficient.

## State precedence and ambiguity decisions

1. The durable claim's acknowledged COMMIT is the execution gate. Only the invocation that inserted and confirmed a new matching claim may collect. Existing claims are never execution permission; CLAIMED has no lease expiry or implicit retry. Collection is outside transactions. Caller-visible DB response failure must never authorize collection.
2. PREPARATION_FAILED is terminal. A later repaired FD/file cannot erase, replace or recollect that failure. Only a fully coherent pre-Popen whitelist response may commit this transition. SPAWN_FAILED, cleanup/IO/unexpected/malformed results and exceptions remain CLAIMED/unknown.
3. PREPARATION_FAILED plus any stored artifact is STATE_CONFLICT, preserving both DBs without returning success or disclosing the artifact. An orphan control claim or conflicting control pin also yields STATE_CONFLICT. Read with an incorrect caller expectation yields ASSIGNMENT_PIN_MISMATCH and no record/artifact.
4. A valid stored artifact without a terminal/conflicting controller row may confirm storage on read/run without collecting. Missing control state in that case does not invent a past claim or establish authentic invocation. A ledger assignment without a claim is incomplete state: read returns OUTCOME_UNKNOWN and never writes or executes. Missing both job and controller state is JOB_NOT_FOUND.
5. COMMIT error is not proof of rollback/absence. In the failing claim invocation, collector calls are zero whether COMMIT happened or not. Independent read remains read-only and may distinguish absent claim, durable CLAIMED and terminal failure. If a later explicit run creates a new acknowledged claim after a genuinely absent prior claim, that is allowed: the prior unconfirmed claim invocation never collected. This is not permission to reset/retry a committed claim, and no product retry loop is allowed. This resolution accepts the plan's two-state schema; permanent refusal after proven absence would require a different durable-intent contract and is not claimed here.
6. For preparation-transition loss, do not falsely report terminal persistence; reopened state decides CLAIMED versus PREPARATION_FAILED. For artifact-record loss, retain original bytes for the caller while withholding storage confirmation; later independent read may establish committed storage. If no artifact was committed, remain claimed/unknown without recollecting. Do not automatically replay writes inside this API.
7. Two DBs do not provide one atomic transaction, permanent pairing or rollback resistance. Preserve partial states; report contradictions rather than repairing them. In particular, no exactly-once process execution, install-origin or active-head guarantee follows from this controller.

## Selected D38 targets — exactly two

- **M1: existing claim permits collection again.** Use the proposed mutation of only the fresh-committed-claim guard, so a matching existing CLAIMED row is treated as execution permission. Run only C05's focused repeated-call target on the isolated mutant. It must fail a semantic assertion about collector count/no new execution or unknown state, with ERROR0. An unchanged artifact CAS must not mask duplicate collection. Reuse normal successful target evidence.
- **M2: SPAWN_FAILED becomes terminal preparation failure.** Add only SPAWN_FAILED to the definitive whitelist in an isolated mutant; use C04's coherent SPAWN_FAILED response with valid no-child/no-output fields. Run only that subcase. It must fail the OUTCOME_UNKNOWN / durable CLAIMED expectation, with ERROR0; malformed-shape rejection must not accidentally kill the mutant. Reuse normal successful target evidence.

These target distinct new risks: duplicate execution authorization and false definitive failure classification. C07-C09 already directly inspect the separately critical claim/failure/artifact COMMIT ambiguities; a third mutation or old R03/R04/R15 rerun is unnecessary for this unit. Record isolated mutant/source SHA, precise change, the failing semantic assertion and unchanged original source. Mutants are never production artifacts.

## Final-review evidence required

Preserve the first new-suite log, initial errors and any narrow correction history. Link exact reviewed plan/resolution hashes, final controller/new-test/contract hashes, case counts, actual versus injected results, real child/profile and SQLite versions, controller/collector counts, reopen bytes/hash/binding evidence, D38 semantic failures and original/evidence before/after comparisons. Read-only original checkout and all prior acceptance/error files must remain unchanged.

This pre-review did not run old or new tests, write implementation, access credentials/settings/network, create threads or reopen sandbox investigation. Final implementation/evidence review remains required.
