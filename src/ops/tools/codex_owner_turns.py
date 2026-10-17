"""Bounded documented history reads on an already initialized owner client.

An externally supplied binding is a claim, not proof of queue-to-turn origin.
No connection creation, initialization, resume, queue call, retry or polling.
"""
import hashlib
import json
import time

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
