"""Read-only revalidation of index, fixed assets and their completed tests."""
from uuid import uuid5

from .contracts import NAMESPACE, fingerprint, strict_json, uid
from .indexing_results import checked_index, checked_version
from .intake import check
from .qa_agent_results import agent_checked, model_checked, run_checked, version_checked
from .search_tool_results import call_checked, connection_checked, tool_checked, version_checked as tool_version_checked
from .search_tool_transport import request_checked as tool_request_checked
from .workflows import canonical

HASH_FIELDS = {'plan_sha256', 'index_sha256', 'tool_version_sha256', 'tool_test_sha256',
               'agent_version_sha256', 'agent_test_sha256', 'child_result_sha256',
               'connections_sha256', 'chat_sha256', 'target_sha256'}


def verified(qa, plan, target, agent, knowledge):
    """Never accepts caller-provided success flags and never dispatches a run or write."""
    source, tools = plan['source'], qa.tools
    binding = tools.indexing.binding()
    check(binding is not None and knowledge.origin == binding['knowledge_origin'],
          'QUESTION_HEAD_KNOWLEDGE_ORIGIN_CHANGED', 409)
    row = tools.indexing.row(source['indexing_job_id'])
    check(row['state'] == 'activated' and row['receipt'] is not None,
          'QUESTION_HEAD_INDEX_NOT_VERIFIED', 409)
    receipt = strict_json(row['receipt'].encode())
    row = {**row, 'documents': source['documents']}
    index = checked_index(knowledge.get('/api/indexes/' + source['index_id']), source['index_id'],
                          source['config']['embedding'], 'document-qa-' + source['library_id'])
    check(index['active_version_id'] == source['index_version_id'], 'QUESTION_HEAD_INDEX_VERSION_CHANGED', 409)
    index_version = knowledge.get('/api/index-versions/' + source['index_version_id'])
    check(checked_version(index_version, receipt, row, source['config']) == 'complete',
          'QUESTION_HEAD_INDEX_NOT_VERIFIED', 409)
    activation = strict_json(row['activation_body'].encode())
    check(index['revision'] >= activation['expected_revision'] + 1, 'QUESTION_HEAD_INDEX_ACTIVATION_UNVERIFIED', 409)

    connections = {}
    for key in ('knowledge_connection', 'model_connection'):
        selected = source['selection'][key]
        connections[key] = connection_checked(agent.get('/api/connection-versions/' + selected['version_id']), selected)
    check(connections == plan['connections'], 'QUESTION_HEAD_CONNECTION_CHANGED', 409)
    steps = {op: tools._step(source['plan_id'], op) for op in
             ('agent.tool.create', 'agent.tool.version', 'agent.tool_call', 'agent.tool.publish')}
    check(all(s and s['state'] == 'verified' and s['receipt'] for s in steps.values()),
          'QUESTION_HEAD_TOOL_NOT_VERIFIED', 409)
    create, draft, test, publish = [steps[op] for op in steps]
    for step in steps.values():
        tool_request_checked('POST', step['path'], step['body'], step['request_key'])
    tool_checked(agent.get('/api/tools/' + plan['tool']['tool_id']), create['body'], plan['tool']['tool_id'])
    version = tool_version_checked(agent.get('/api/tool-versions/' + plan['tool']['tool_version_id']), source,
                                    plan['tool']['tool_id'], plan['tool']['tool_version_id'], draft['receipt'])
    check(version == plan['tool'] == publish['receipt'] and version['state'] == 'published'
          and publish['path'] == '/api/tool-versions/' + version['tool_version_id'] + '/publish'
          and publish['attempts'] > 0 and version['revision'] == publish['body']['expected_revision'] + 1,
          'QUESTION_HEAD_TOOL_PUBLICATION_CHANGED', 409)
    check(test['body'] == {'tool_version_id': version['tool_version_id'],
                          'arguments': test['body']['arguments'], 'mode': 'test'}
          and uid(test['receipt']['resource_id']) and test['receipt']['version_id'] == version['tool_version_id']
          and test['receipt']['status_url'] == '/api/tool-calls/' + test['receipt']['resource_id'],
          'QUESTION_HEAD_TOOL_TEST_CHANGED', 409)
    state, summary = call_checked(agent.get(test['receipt']['status_url']), source, version, test)
    check(state == 'verified' and summary == test['summary'], 'QUESTION_HEAD_TOOL_TEST_CHANGED', 409)

    create = qa._step(plan['plan_id'], 'agent.agent.create')
    check(create and create['state'] == 'verified' and create['receipt'], 'QUESTION_HEAD_AGENT_NOT_VERIFIED', 409)
    agent_checked(agent.get('/api/agents/' + create['receipt']['agent_id']), create['body'], create['receipt']['agent_id'])
    saved = qa._version(plan)
    actual = version_checked(agent.get('/api/agent-versions/' + target['agent_version_id']), plan,
                             saved['agent_id'], saved['agent_version_id'], saved)
    published = qa._step(plan['plan_id'], 'agent.agent.publish')
    check(actual == published['summary'] and actual['state'] == 'published',
          'QUESTION_HEAD_AGENT_PUBLICATION_CHANGED', 409)
    test = qa._step(plan['plan_id'], 'agent.run')
    check(test['body']['mode'] == 'test' and test['body']['agent_version_id'] == target['agent_version_id']
          and test['receipt']['version_id'] == target['agent_version_id'], 'QUESTION_HEAD_AGENT_TEST_CHANGED', 409)
    run = agent.get(test['receipt']['status_url'])
    child_id = run.get('result', {}).get('tool_call_id') if isinstance(run.get('result'), dict) else None
    check(uid(child_id), 'QUESTION_HEAD_AGENT_TEST_CHANGED', 409)
    child = agent.get('/api/tool-calls/' + child_id)
    state, result = run_checked(run, plan, actual, test, child)
    check(state == 'verified' and result == test['summary'], 'QUESTION_HEAD_AGENT_TEST_CHANGED', 409)

    selected = plan['selection']['chat_connection']
    conn = connection_checked(agent.get('/api/connection-versions/' + selected['version_id']), selected)
    body = {'action': 'resolve', 'connection_version_id': selected['version_id'],
            'deployment_id': plan['selection']['chat_deployment_id']}
    status, raw = agent.request('POST', '/api/agent-models/inspect', body,
                                str(uuid5(NAMESPACE, 'q06b3:inspect:' + plan['plan_id'])))
    check(type(status) is int and status == 200, 'QUESTION_HEAD_CHAT_CHANGED', 502)
    chat = {'connection': conn, 'model': model_checked(strict_json(raw), plan)}
    with qa.uploads.connect() as db:
        row = db.execute('SELECT * FROM qa_agent_chat WHERE plan_id=?', (plan['plan_id'],)).fetchone()
    check(row and row['snapshot'] == canonical(chat) and row['snapshot_sha256'] == fingerprint(chat),
          'QUESTION_HEAD_CHAT_CHANGED', 409)
    # Refuse assets that changed while other components were being inspected.
    check(tool_version_checked(agent.get('/api/tool-versions/' + version['tool_version_id']), source,
                                version['tool_id'], version['tool_version_id'], version) == version,
          'QUESTION_HEAD_TOOL_PUBLICATION_CHANGED', 409)
    check(version_checked(agent.get('/api/agent-versions/' + actual['agent_version_id']), plan,
                          actual['agent_id'], actual['agent_version_id'], actual) == actual,
          'QUESTION_HEAD_AGENT_PUBLICATION_CHANGED', 409)
    check(checked_index(knowledge.get('/api/indexes/' + source['index_id']), source['index_id'],
                        source['config']['embedding'], index['name']) == index,
          'QUESTION_HEAD_INDEX_VERSION_CHANGED', 409)
    return {'schema': 'question-head-proof-v1', 'plan_sha256': fingerprint(plan),
            'index_sha256': fingerprint(index_version), 'tool_version_sha256': fingerprint(version),
            'tool_test_sha256': summary['result_sha256'], 'agent_version_sha256': fingerprint(actual),
            'agent_test_sha256': result['result_sha256'], 'child_result_sha256': fingerprint(child['result']),
            'connections_sha256': fingerprint(connections), 'chat_sha256': fingerprint(chat),
            'target_sha256': fingerprint(target)}
