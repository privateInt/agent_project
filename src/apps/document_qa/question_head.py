"""Q06b-3 verified question-head CAS. CLI init/status performs no remote I/O."""
import argparse
import json
from pathlib import Path
import sqlite3

from . import question_head_schema as schema, qa_agent_schema, readiness
from .contracts import ContractError, VerifiedHead, digest, fingerprint, integer, strict_json, switch_head, uid
from .intake import UploadError, check
from .qa_agent import QAAgentStore, agent_lane
from .question_head_results import HASH_FIELDS, verified
from .question_store import question_lane, read_state, stored_target
from .question_targets import MAX_REVISION, as_head, encoded_target, target_id
from .search_tool import tool_lane
from .uploads import UploadStore, now
from .workflows import canonical


def request_checked(client_key, plan_id, expected_revision):
    check(uid(client_key) and uid(plan_id) and integer(expected_revision, 0, MAX_REVISION - 1),
          'QUESTION_HEAD_REQUEST_INVALID')
    return {'plan_id': plan_id, 'expected_revision': expected_revision}


def receipt(row):
    return {'client_key': row['client_key'], 'plan_id': row['plan_id'],
            'expected_revision': row['expected_revision'], 'previous_target_id': row['previous_target_id'],
            'head_revision': row['expected_revision'] + 1, 'target_id': row['target_id'],
            'target_sha256': row['target_sha256'], 'verification_sha256': row['proof_sha256'],
            'activated_at': row['created_at'], 'result': 'applied', 'semantic_quality_verified': False}


def read_verified_head(db):
    head, targets = read_state(db)
    check(qa_agent_schema.present(db), 'QA_AGENT_NOT_INITIALIZED', 503)
    check(schema.present(db), 'QUESTION_HEAD_NOT_INITIALIZED', 503)
    rows = db.execute('SELECT * FROM qa_head_activations ORDER BY expected_revision LIMIT ?',
                      (schema.MAX_ACTIVATIONS + 1,)).fetchall()
    check(len(rows) <= schema.MAX_ACTIVATIONS, 'QUESTION_HEAD_ACTIVATION_LIMIT_EXCEEDED', 503)
    previous = None
    for revision, row in enumerate(rows):
        request = strict_json(row['request'].encode())
        proof = strict_json(row['proof'].encode())
        target = targets.get(row['target_id'])
        plan = db.execute('SELECT body_sha256 FROM qa_agent_plans WHERE plan_id=?', (row['plan_id'],)).fetchone()
        check(request == request_checked(row['client_key'], row['plan_id'], row['expected_revision'])
              and row['request'] == canonical(request) and row['request_sha256'] == fingerprint(request)
              and row['expected_revision'] == revision and row['previous_target_id'] == previous
              and target is not None and target['body']['revision'] == revision + 1
              and row['target_sha256'] == target['body_sha256']
              and set(proof) == {'schema'} | HASH_FIELDS and proof['schema'] == 'question-head-proof-v1'
              and all(digest(proof[k]) for k in HASH_FIELDS)
              and proof['target_sha256'] == target['body_sha256'] and plan is not None
              and proof['plan_sha256'] == plan[0] and row['proof'] == canonical(proof)
              and row['proof_sha256'] == fingerprint(proof) and isinstance(row['created_at'], str)
              and bool(row['created_at']), 'QUESTION_HEAD_RECEIPT_INVALID', 503)
        previous = row['target_id']
    check(head['revision'] == len(rows) and head['target_id'] == previous,
          'QUESTION_HEAD_ACTIVATION_UNVERIFIED', 503)
    return head, rows


def local_pins(db):
    """Capture local evidence in one DB snapshot; check it again inside the CAS transaction."""
    tables = ('indexing_head', 'indexing_binding', 'indexing_tasks', 'search_tool_plans',
              'search_tool_steps', 'qa_agent_plans', 'qa_agent_steps', 'qa_agent_chat')
    result = {}
    for table in tables:
        # Internal constant table names; no caller SQL. Histories are already bounded by prior stages.
        rows = [dict(row) for row in db.execute('SELECT * FROM ' + table)]
        result[table] = sorted(rows, key=canonical)
    return fingerprint(result)


class QuestionHeadStore:
    def __init__(self, uploads):
        self.qa = QAAgentStore(uploads)
        self.uploads, self.root = uploads, uploads.root
        with uploads.connect() as db:
            read_verified_head(db)

    @classmethod
    def initialize(cls, uploads):
        QAAgentStore(uploads)  # Prerequisites must already exist; no reset or silent migration.
        with agent_lane(uploads.root), tool_lane(uploads.root), question_lane(uploads.root):
            with uploads.connect(write=True) as db:
                read_state(db)
                if not schema.present(db):
                    schema.install(db)
                read_verified_head(db)
        return cls(uploads)

    def by_request(self, client_key):
        check(uid(client_key), 'QUESTION_HEAD_CLIENT_KEY_INVALID')
        with self.uploads.connect() as db:
            _, rows = read_verified_head(db)
            row = next((row for row in rows if row['client_key'] == client_key), None)
            check(row is not None, 'QUESTION_HEAD_REQUEST_NOT_FOUND', 404)
            return receipt(row)

    def replay(self, client_key, plan_id, expected_revision):
        body = request_checked(client_key, plan_id, expected_revision)
        with self.uploads.connect() as db:
            _, rows = read_verified_head(db)
            prior = next((row for row in rows if row['client_key'] == client_key), None)
            if prior is None:
                return None
            check(prior['request'] == canonical(body), 'QUESTION_HEAD_CLIENT_KEY_CONFLICT', 409)
            return receipt(prior)

    def activate(self, client_key, plan_id, expected_revision, agent, knowledge):
        body = request_checked(client_key, plan_id, expected_revision)
        with agent_lane(self.root), tool_lane(self.root), question_lane(self.root):
            with self.uploads.connect() as db:
                head, rows = read_verified_head(db)
                prior = next((row for row in rows if row['client_key'] == client_key), None)
                if prior:
                    check(prior['request'] == canonical(body), 'QUESTION_HEAD_CLIENT_KEY_CONFLICT', 409)
                    return receipt(prior)  # Works after later heads; no repeated remote calls.
                check(head['revision'] == expected_revision, 'QUESTION_HEAD_REVISION_CONFLICT', 409)
                check(len(rows) < schema.MAX_ACTIVATIONS, 'QUESTION_HEAD_ACTIVATION_LIMIT_EXCEEDED', 409)
                pins = local_pins(db)
            plan = self.qa._plan(plan_id, agent, current=True)
            target = self.qa.target(plan_id, expected_revision + 1)
            check(head['target'] is None or
                  {k: v for k, v in head['target']['body'].items() if k != 'revision'} !=
                  {k: v for k, v in target.items() if k != 'revision'},
                  'QUESTION_HEAD_TARGET_ALREADY_ACTIVE', 409)
            proof = verified(self.qa, plan, target, agent, knowledge)
            # These locks serialize all adapter-managed evidence. The DB snapshot also
            # catches a writer bypassing a lane between remote verification and commit.
            with self.uploads.connect(write=True) as db:
                current, rows = read_verified_head(db)
                check(current == head and local_pins(db) == pins, 'QUESTION_HEAD_LOCAL_EVIDENCE_CHANGED', 409)
                switch_head(as_head(current['target']['body']) if current['target'] else None,
                            VerifiedHead(as_head(target)), expected_revision)
                identifier = target_id(target)
                existing = db.execute('SELECT * FROM question_targets WHERE target_id=?', (identifier,)).fetchone()
                if existing:
                    check(stored_target(existing, head['library_id'])['body'] == target,
                          'QUESTION_TARGET_CONFLICT', 409)
                else:
                    check(db.execute('SELECT count(*) FROM question_targets').fetchone()[0] < 100,
                          'QUESTION_TARGET_LIMIT_EXCEEDED', 409)
                    db.execute('INSERT INTO question_targets VALUES(?,?,?,?,?)',
                               (identifier, head['library_id'], encoded_target(target).decode(), fingerprint(target), now()))
                changed = db.execute('UPDATE question_head SET revision=?,target_id=? WHERE singleton=1 AND revision=? AND target_id IS ?',
                                     (expected_revision + 1, identifier, expected_revision, head['target_id'])).rowcount
                check(changed == 1, 'QUESTION_HEAD_REVISION_CONFLICT', 409)
                db.execute('INSERT INTO qa_head_activations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (client_key, canonical(body), fingerprint(body), plan_id, expected_revision,
                            head['target_id'], identifier, fingerprint(target), canonical(proof), fingerprint(proof), now()))
                read_verified_head(db)
                result = receipt(db.execute('SELECT * FROM qa_head_activations WHERE client_key=?', (client_key,)).fetchone())
            return result  # Commit precedes response; lookup resolves a lost response.

    def accept(self, client_key, question):
        return self.qa.accept(client_key, question, head_gate=read_verified_head)

    def status(self):
        with self.uploads.connect() as db:
            head, rows = read_verified_head(db)
        return {**self.qa.status(), 'question_head_schema': schema.VERSION,
                'head_activation_implemented': True, 'head_revision': head['revision'],
                'head_target_id': head['target_id'], 'head_active': head['target'] is not None,
                'activation_count': len(rows), 'max_activations': schema.MAX_ACTIVATIONS,
                'question_ready': False, 'semantic_quality_verified': False,
                'production_question_verified': False,
                'next_step': 'Q05c_1_first_document_live_indexing_when_GPU_available'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'status'))
    parser.add_argument('--state', default=str(Path.home() / '.local/state/agent-project-document-qa/uploads'))
    args = parser.parse_args(argv)
    try:
        uploads = UploadStore(args.state)
        store = QuestionHeadStore.initialize(uploads) if args.action == 'init' else QuestionHeadStore(uploads)
        result = store.status()
        result.update(result='passed', production_api_requests=0, inference_requested=False, GPU_requested=False)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False)); return 0
    except (UploadError, readiness.ReadinessError) as exc:
        code = exc.code
    except (OSError, sqlite3.Error, ContractError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        code = 'LOCAL_QUESTION_HEAD_STATE_UNCERTAIN'
    print(json.dumps({'result': 'attention_required', 'code': code, 'question_ready': False,
                      'production_api_requests': 0, 'inference_requested': False, 'GPU_requested': False})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
