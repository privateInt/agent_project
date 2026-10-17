"""Bounded documented history reads on an already initialized owner client.

An externally supplied binding is a claim, not proof of queue-to-turn origin.
No connection creation, initialization, resume, queue call, retry or polling.
"""
import hashlib
import json
import time
import math

MAX_BYTES = 1048576


class TurnObservationError(Exception):
    pass


def need(value, reason):
    if not value:
        raise TurnObservationError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def observe_bound_turn(client, binding, prompt, *, max_pages=4, timeout=20):
    """Return only the pinned turn's minimal projection; do not retain history."""
    result = {'status': 'UNCONFIRMED', 'reason': None, 'turn_id': None,
              'final_text_sha256': None, 'native_ready': False,
              'binding_origin_verified': False, 'replay_permitted': False}
    try:
        need(type(binding) is dict and type(prompt) is str and type(max_pages) is int
             and 1 <= max_pages <= 4 and type(timeout) in (int, float) and 0 < timeout <= 20, 'OBSERVATION_ARGUMENTS_INVALID')
        thread = binding['thread_id']
        need(client.read_only is True and client.completion_reads is True
             and client.allowed_threads == frozenset((thread,)), 'OWNER_CLIENT_SCOPE_INVALID')
        cursor, seen_cursors, seen_turns, candidates, pages = None, set(), set(), [], []
        total, deadline = 0, time.monotonic() + timeout
        for _index in range(max_pages):
            need(time.monotonic() < deadline, 'OBSERVATION_TIMEOUT')
            response = client.read_turn_page(thread, cursor)
            need(time.monotonic() < deadline, 'OBSERVATION_TIMEOUT')
            raw = canonical(response); total += len(raw)
            need(total <= MAX_BYTES, 'OBSERVATION_BYTE_LIMIT')
            pages.append(sha(raw))
            need(type(response) is dict and set(response) <= {'data', 'nextCursor', 'backwardsCursor'}
                 and 'nextCursor' in response and type(response.get('data')) is list
                 and len(response['data']) <= 20, 'TURN_PAGE_INVALID')
            for turn in response['data']:
                need(type(turn) is dict and type(turn.get('id')) is str and turn['id'] not in seen_turns, 'TURN_AMBIGUOUS')
                seen_turns.add(turn['id'])
                if turn['id'] == binding['turn_id']:
                    candidates.append(turn)
            following = response['nextCursor']
            need(following is None or (type(following) is str and 1 <= len(following.encode('utf8')) <= 1024
                                      and following not in seen_cursors), 'TURN_CURSOR_INVALID')
            if following is None:
                break
            seen_cursors.add(following); cursor = following
        else:
            raise TurnObservationError('TURN_PAGES_INCOMPLETE')
        need(len(candidates) == 1, 'TURN_NOT_FOUND' if not candidates else 'TURN_AMBIGUOUS')
        turn = candidates[0]
        need(turn.get('itemsView', 'full') == 'full' and type(turn.get('items')) is list, 'TURN_ITEMS_INCOMPLETE')
        matches = [x for x in turn['items'] if type(x) is dict and x.get('type') == 'userMessage'
                   and x.get('id') == binding['user_message_item_id']]
        need(len(matches) == 1, 'USER_MESSAGE_AMBIGUOUS')
        item = matches[0]
        need(item.get('clientId') == binding['observed_client_id'], 'USER_CLIENT_LITERAL_MISMATCH')
        content = item.get('content')
        need(type(content) is list and len(content) == 1 and type(content[0]) is dict
             and set(content[0]) in ({'type', 'text'}, {'type', 'text', 'text_elements'})
             and content[0].get('type') == 'text' and content[0].get('text_elements', []) == [], 'USER_INPUT_INVALID')
        need(content[0].get('text') == prompt, 'PROMPT_MISMATCH')
        need(sha(prompt.encode('utf8')) == binding['prompt_sha256'], 'PROMPT_HASH_MISMATCH')
        status = turn.get('status')
        need(status in ('completed', 'failed', 'interrupted', 'inProgress'), 'TURN_STATUS_INVALID')
        result.update(turn_id=turn['id'], page_hashes=pages, turn_status=status)
        if status == 'completed':
            need(turn.get('error') is None, 'TURN_ERROR_INCONSISTENT')
            final = [x for x in turn['items'] if type(x) is dict and x.get('type') == 'agentMessage'
                     and x.get('phase') == 'final_answer']
            need(len(final) == 1 and type(final[0].get('text')) is str
                 and len(final[0]['text'].encode('utf8')) <= 65536, 'FINAL_ANSWER_INCOMPLETE')
            result.update(status='TURN_COMPLETED', reason='BOUND_COMPLETION_OBSERVED',
                          final_text_sha256=sha(final[0]['text'].encode('utf8')))
        else:
            result.update(status={'failed': 'TURN_FAILED', 'interrupted': 'TURN_INTERRUPTED',
                                  'inProgress': 'TURN_IN_PROGRESS'}[status], reason='BOUND_STATUS_OBSERVED')
    except TurnObservationError as error:
        result['reason'] = str(error)
    except Exception:
        result['reason'] = 'OWNER_READ_UNCONFIRMED'
    return result


def history_snapshot(client, thread_id, *, max_pages=4, timeout=20):
    """Read a complete bounded history, retaining raw turns only in memory.

    This is not an atomic snapshot. Existing user inputs must also survive the
    later scan; normal status/agent-output changes are deliberately excluded.
    """
    need(type(max_pages) is int and 1 <= max_pages <= 4
         and type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 20,
         'HISTORY_ARGUMENTS_INVALID')
    need(client.read_only is True and client.completion_reads is True
         and client.allowed_threads == frozenset((thread_id,)), 'OWNER_CLIENT_SCOPE_INVALID')
    cursor, cursors, turns, users, item_ids, hashes = None, set(), [], {}, set(), []
    total, deadline = 0, time.monotonic() + timeout
    for _index in range(max_pages):
        need(time.monotonic() < deadline, 'HISTORY_TIMEOUT')
        # The installed borrowed transport bounds each read to at most 5s.
        # Narrow its request timeout to the remaining scan budget, never extend
        # its total lifetime or create/reconnect an owner client.
        old_timeout = getattr(client, 'request_timeout', None)
        if old_timeout is not None:
            need(type(old_timeout) in (int, float) and math.isfinite(old_timeout)
                 and 0 < old_timeout <= 5, 'OWNER_DEADLINE_INVALID')
            client.request_timeout = min(old_timeout, deadline - time.monotonic())
        try:
            response = client.read_turn_page(thread_id, cursor)
        finally:
            if old_timeout is not None:
                client.request_timeout = old_timeout
        need(time.monotonic() < deadline, 'HISTORY_TIMEOUT')
        raw = canonical(response); total += len(raw)
        need(total <= MAX_BYTES, 'HISTORY_BYTE_LIMIT')
        hashes.append(sha(raw))
        need(type(response) is dict and set(response) <= {'data', 'nextCursor', 'backwardsCursor'}
             and 'nextCursor' in response and type(response.get('data')) is list
             and len(response['data']) <= 20, 'HISTORY_PAGE_INVALID')
        for turn in response['data']:
            need(type(turn) is dict and type(turn.get('id')) is str
                 and 1 <= len(turn['id']) <= 128 and turn['id'] not in users, 'HISTORY_TURN_AMBIGUOUS')
            need(turn.get('itemsView') == 'full' and type(turn.get('items')) is list,
                 'HISTORY_ITEMS_INCOMPLETE')
            projection = {}
            for item in turn['items']:
                need(type(item) is dict and type(item.get('type')) is str
                     and type(item.get('id')) is str and item['id'], 'HISTORY_ITEM_INVALID')
                if item.get('type') != 'userMessage':
                    continue
                identity, content = item.get('id'), item.get('content')
                need(type(identity) is str and 1 <= len(identity) <= 128 and identity not in item_ids,
                     'HISTORY_USER_AMBIGUOUS')
                need(set(item) <= {'type','id','content','clientId'}
                     and (item.get('clientId') is None or type(item['clientId']) is str)
                     and type(content) is list and content, 'HISTORY_USER_INVALID')
                for piece in content:
                    validate_user_input(piece)
                item_ids.add(identity); projection[identity] = sha(canonical(item))
            users[turn['id']] = projection; turns.append(turn)
        following = response['nextCursor']
        need(following is None or (type(following) is str and 1 <= len(following.encode()) <= 1024
                                  and following not in cursors), 'HISTORY_CURSOR_INVALID')
        if following is None:
            return {'turns': turns, 'users': users, 'turn_ids': sorted(users), 'page_hashes': hashes}
        cursors.add(following); cursor = following
    raise TurnObservationError('HISTORY_PAGES_INCOMPLETE')


def validate_user_input(piece):
    """Required shapes from the pinned installed UserInput union; no URL/file IO."""
    need(type(piece) is dict and type(piece.get('type')) is str, 'HISTORY_USER_INVALID')
    kind = piece['type']
    if kind == 'text':
        need({'type','text'} <= set(piece) <= {'type','text','text_elements'}
             and type(piece['text']) is str and type(piece.get('text_elements',[])) is list,
             'HISTORY_USER_TEXT_INVALID')
        for element in piece.get('text_elements',[]):
            need(type(element) is dict and {'byteRange'} <= set(element) <= {'byteRange','placeholder'}
                 and (element.get('placeholder') is None or type(element['placeholder']) is str), 'HISTORY_TEXT_ELEMENT_INVALID')
            span = element['byteRange']
            need(type(span) is dict and set(span) == {'start','end'}
                 and all(type(span[x]) is int for x in span)
                 and 0 <= span['start'] <= span['end'] <= len(piece['text'].encode()), 'HISTORY_TEXT_ELEMENT_INVALID')
    elif kind in ('image','localImage'):
        required = {'type','path'} if kind == 'localImage' else {'type'}
        allowed = required | ({'detail'} if kind == 'localImage' else {'url','fileId','detail'})
        need(required <= set(piece) <= allowed
             and (kind == 'localImage' or 'url' in piece or 'fileId' in piece)
             and all(type(piece[x]) is str for x in ('path','url','fileId') if x in piece)
             and piece.get('detail') in (None,'auto','low','high','original'), 'HISTORY_USER_IMAGE_INVALID')
    elif kind in ('audio','localAudio','skill','mention'):
        required = {'type','url'} if kind == 'audio' else ({'type','path'} if kind == 'localAudio' else {'type','name','path'})
        need(set(piece) == required and all(type(piece[x]) is str for x in required), 'HISTORY_USER_INPUT_INVALID')
    else:
        raise TurnObservationError('HISTORY_USER_INPUT_UNSUPPORTED')


def marker_items(snapshot, marker):
    return [(turn, item) for turn in snapshot['turns'] for item in turn['items']
            if item.get('type') == 'userMessage'
            and any(type(x.get('text')) is str and marker in x['text'] for x in item['content'])]


def match_task_turn(client, fence, prompt, task_id):
    """Own-message correlation only, never a native ACK or client-ID proof."""
    result = {'status': 'UNCONFIRMED', 'reason': None, 'turn_id': None,
              'native_ready': False, 'native_ack_verified': False,
              'binding_origin_verified': False, 'client_id_identity_verified': False,
              'replay_permitted': False, 'correlation_kind': 'OWN_EXACT_USER_MESSAGE'}
    try:
        snapshot = history_snapshot(client, fence['thread_id'])
        need(all(snapshot['users'].get(tid) == projection for tid, projection in fence['users'].items()),
             'BASELINE_HISTORY_CHANGED')
        marker = '[agent-project-task:v1:' + task_id + ']'
        candidates = marker_items(snapshot, marker)
        need(len(candidates) == 1, 'TASK_MESSAGE_NOT_FOUND' if not candidates else 'TASK_MESSAGE_AMBIGUOUS')
        turn, item = candidates[0]
        need(turn['id'] not in fence['turn_ids']
             and all(item['id'] not in ids for ids in fence['users'].values()), 'TASK_MESSAGE_PREEXISTING')
        content = item['content']
        need(len(content) == 1 and set(content[0]) in ({'type','text'}, {'type','text','text_elements'})
             and content[0].get('type') == 'text' and content[0].get('text_elements', []) == [],
             'TASK_MESSAGE_SHAPE_MISMATCH')
        need(content[0].get('text') == prompt, 'TASK_MESSAGE_BODY_MISMATCH')
        client_id = item.get('clientId')
        need(client_id is None or (type(client_id) is str and len(client_id.encode()) <= 1024),
             'TASK_CLIENT_LITERAL_INVALID')
        status = turn.get('status')
        need(status in ('completed','failed','interrupted','inProgress'), 'TURN_STATUS_INVALID')
        result.update(turn_id=turn['id'], user_message_item_id=item['id'], observed_client_id=client_id,
                      page_hashes=snapshot['page_hashes'], prompt_sha256=sha(prompt.encode()), turn_status=status)
        if status == 'completed':
            need(turn.get('error') is None, 'TURN_ERROR_INCONSISTENT')
            final = [x for x in turn['items'] if x.get('type') == 'agentMessage' and x.get('phase') == 'final_answer']
            need(len(final) == 1 and type(final[0].get('text')) is str
                 and len(final[0]['text'].encode()) <= 65536, 'FINAL_ANSWER_INCOMPLETE')
            result.update(status='TURN_COMPLETED', reason='EXACT_TASK_MESSAGE_COMPLETION_OBSERVED',
                          final_text_sha256=sha(final[0]['text'].encode()))
        else:
            result.update(status={'inProgress':'TURN_IN_PROGRESS','failed':'TURN_FAILED',
                                  'interrupted':'TURN_INTERRUPTED'}[status], reason='EXACT_TASK_MESSAGE_STATUS_OBSERVED')
    except TurnObservationError as error:
        result['reason'] = str(error)
    except Exception:
        result['reason'] = 'OWNER_HISTORY_UNCONFIRMED'
    return result
