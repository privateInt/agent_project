"""Bounded REST client. No S3 credentials, redirects, jobs or runtime control."""
import hashlib
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

MAX_BYTES = 32 << 20
COLLECTIONS = {'source-groups', 'crawl-runs', 'source-documents', 'refinement-runs', 'operations', 'canonical-documents'}


class APIError(Exception):
    def __init__(self, message, status=0, code=''):
        super().__init__(str(message)[:800])
        self.status, self.code = status, code


def identifier(value):
    try:
        parsed = uuid.UUID(str(value))
        if parsed.int and str(parsed) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise ValueError('유효한 UUID를 입력하세요.')


def origin(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
        raise ValueError('Gateway 설정은 http(s) origin이어야 합니다.')
    _ = p.port
    return value.rstrip('/')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def verified(data, metadata):
    digest = metadata.get('sha256', '')
    size = metadata.get('size_bytes')
    if (type(size) is not int or not 0 < size <= MAX_BYTES or
            not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) or
            len(data) != size or hashlib.sha256(data).hexdigest() != digest):
        raise APIError('다운로드 크기 또는 SHA-256이 등록 정보와 다릅니다.', code='INTEGRITY_ERROR')
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, base=None):
        self.base = origin(base or os.environ.get('DATA_GATEWAY_URL', 'http://data-gateway:8000'))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _request(self, method, path, data=None, headers=None, limit=2 << 20, timeout=30, base=None):
        req = urllib.request.Request((base or self.base) + path, data=data, headers=headers or {}, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise APIError('응답 크기 제한을 초과했습니다.', code='RESPONSE_TOO_LARGE')
                return raw
        except urllib.error.HTTPError as e:
            raw = e.read(4096)
            code, message = 'HTTP_ERROR', 'Gateway 요청이 거절되었습니다.'
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    code = value.get('error_code') or value.get('code') or code
                    message = value.get('error_message') or value.get('error') or value.get('message') or message
            except (ValueError, UnicodeError):
                pass
            raise APIError(message, e.code, str(code)[:100]) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise APIError('Gateway 연결 또는 응답 수신에 실패했습니다. 요청이 접수되었을 수 있습니다.', code='CONNECTION_ERROR') from None

    @staticmethod
    def _object(raw):
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except (ValueError, UnicodeError):
            pass
        raise APIError('Gateway 응답이 유효한 JSON 객체가 아닙니다.', code='INVALID_RESPONSE')

    def listing(self, collection, **filters):
        if collection not in COLLECTIONS - {'canonical-documents'}:
            raise ValueError('unsupported collection')
        allowed = {'limit', 'cursor', 'status', 'resource_type', 'resource_id', 'kind', 'phase'} if collection == 'operations' else {'limit', 'cursor', 'q'}
        if collection in ('source-documents', 'refinement-runs'):
            allowed = allowed | {'include_deleted'}
        if collection == 'source-documents':
            allowed = allowed | {'group_id'}
            if filters.get('group_id'): identifier(filters['group_id'])
        if set(filters) - allowed:
            raise ValueError('unsupported filters')
        query = urllib.parse.urlencode({k: v for k, v in filters.items() if v not in ('', None)})
        result = self._object(self._request('GET', '/api/' + collection + '?' + query))
        if not isinstance(result.get('items'), list) or not all(isinstance(x, dict) for x in result['items']) or not isinstance(result.get('next_cursor', ''), str):
            raise APIError('목록 응답 형식이 올바르지 않습니다.', code='INVALID_RESPONSE')
        return result

    def get(self, collection, id):
        if collection not in COLLECTIONS:
            raise ValueError('unsupported collection')
        value = self._object(self._request('GET', '/api/' + collection + '/' + identifier(id)))
        field = {'source-groups': 'group_id', 'crawl-runs': 'crawl_run_id', 'refinement-runs': 'refinement_run_id', 'operations': 'operation_id'}.get(collection, 'document_id')
        if value.get(field) != id:
            raise APIError('조회 응답의 식별자가 요청과 다릅니다.', code='INVALID_RESPONSE')
        return value

    @staticmethod
    def group_response(value, id=None):
        try:
            identifier(value['group_id'])
            if id is not None and value['group_id'] != id: raise ValueError()
            if not isinstance(value['name'], str) or not value['name'].strip(): raise ValueError()
            if type(value['version']) is not int or value['version'] < 1: raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise APIError('그룹 응답을 확인하지 못했습니다. 목록을 새로고침해주세요.', code='INVALID_RESPONSE') from None
        return value

    def create_group(self, name, key):
        identifier(key)
        return self.group_response(self._object(self._request('POST', '/api/source-groups',
            encode({'name': name}), {'Content-Type': 'application/json', 'Idempotency-Key': key})))

    def rename_group(self, id, name, version):
        identifier(id)
        if type(version) is not int or version < 1: raise ValueError('그룹을 다시 조회해주세요.')
        return self.group_response(self._object(self._request('PUT', '/api/source-groups/' + id,
            encode({'name': name, 'expected_version': version}), {'Content-Type': 'application/json'})), id)

    def assign_group(self, document_id, name, key):
        identifier(document_id); identifier(key)
        name = name.strip()
        if not name or len(name) > 200: raise ValueError('그룹 이름은 1~200자로 입력해주세요.')
        value = self._object(self._request('PUT', '/api/source-documents/' + document_id + '/group',
            encode({'name': name}), {'Content-Type': 'application/json', 'Idempotency-Key': key}))
        if value.get('document_id') != document_id:
            raise APIError('그룹 지정 응답의 자료가 요청과 다릅니다.', code='INVALID_RESPONSE')
        group = self.group_response(value.get('group'))
        if group['name'] != name:
            raise APIError('그룹 지정 응답의 이름이 요청과 다릅니다.', code='INVALID_RESPONSE')
        return group

    def group_membership(self, group_id, document_id, present):
        identifier(group_id); identifier(document_id)
        if type(present) is not bool: raise ValueError('invalid membership')
        value = self._object(self._request('PUT' if present else 'DELETE',
            '/api/source-groups/' + group_id + '/documents/' + document_id))
        if value.get('group_id') != group_id or value.get('document_id') != document_id or value.get('present') is not present:
            raise APIError('그룹 변경 완료를 확인하지 못했습니다. 같은 자료로 다시 시도해주세요.', code='INVALID_RESPONSE')
        return value

    def create(self, kind, body, key, file=None):
        paths = {'crawl': 'crawl-runs', 'upload': 'source-documents', 'refine': 'refinement-runs'}
        identifier(key)
        headers = {'Idempotency-Key': key}
        if kind == 'upload':
            filename = body['filename']
            if not file or len(file) > MAX_BYTES or not filename or any(c in filename for c in '\r\n"\\/') or any(ord(c) < 32 for c in filename):
                raise ValueError('파일 이름 또는 크기가 올바르지 않습니다. 최대 32 MiB입니다.')
            boundary = 'data-console-' + uuid.uuid4().hex
            raw = ('--' + boundary + '\r\nContent-Disposition: form-data; name="name"\r\n\r\n' + body['name'] + '\r\n--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + filename + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode('utf-8') + file + ('\r\n--' + boundary + '--\r\n').encode()
            headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
        else:
            raw = encode(body)
            headers['Content-Type'] = 'application/json'
        result = self._object(self._request('POST', '/api/' + paths[kind], raw, headers, timeout=150))
        field = {'crawl': 'crawl_run_id', 'upload': 'document_id', 'refine': 'refinement_run_id'}[kind]
        try:
            identifier(result[field])
            if kind != 'upload':
                identifier(result['operation_id'])
        except (KeyError, ValueError):
            raise APIError('접수 응답의 식별자가 올바르지 않습니다. 같은 요청으로 다시 확인하세요.', code='INVALID_RESPONSE') from None
        return result

    def delete(self, collection, id):
        if collection not in ('source-documents', 'refinement-runs'):
            raise ValueError('unsupported deletion')
        identifier(id)
        value = self._object(self._request('DELETE', '/api/' + collection + '/' + id))
        field = 'document_id' if collection == 'source-documents' else 'refinement_run_id'
        try:
            deleted = datetime.fromisoformat(value.get('deleted_at', '').replace('Z', '+00:00'))
            if value.get(field) != id or deleted.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise APIError('삭제 응답을 확인하지 못했습니다. 같은 항목에서 다시 확인해주세요.', code='INVALID_RESPONSE') from None
        return value

    def purge(self, collection, id):
        if collection not in ('source-documents', 'refinement-runs'):
            raise ValueError('unsupported permanent deletion')
        identifier(id)
        value = self._object(self._request('POST', '/api/' + collection + '/' + id + '/purge', timeout=100))
        field = 'document_id' if collection == 'source-documents' else 'refinement_run_id'
        try:
            when = datetime.fromisoformat(value.get('purged_at', '').replace('Z', '+00:00'))
            if value.get(field) != id or value.get('purge_state') != 'purged' or when.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise APIError('파일 삭제 완료를 확인하지 못했습니다. 같은 항목에서 다시 확인해주세요.', code='INVALID_RESPONSE') from None
        return value

    def download(self, collection, id, metadata, representation='content'):
        if collection not in ('source-documents', 'canonical-documents') or representation not in ('content', 'http-response'):
            raise ValueError('unsupported download')
        # Validate the advertised bound before allocating the response.
        size = metadata.get('size_bytes')
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise APIError('다운로드 크기가 지원 범위를 벗어납니다.', code='INTEGRITY_ERROR')
        raw = self._request('GET', '/api/' + collection + '/' + identifier(id) + '/' + representation, limit=size, timeout=100)
        return verified(raw, metadata)

    def deployment(self, id):
        base = origin(os.environ.get('MODEL_GATEWAY_URL', 'http://gateway:8000'))
        value = self._object(self._request('GET', '/api/deployments/' + identifier(id), base=base, timeout=5))
        if value.get('deployment_id') != id:
            raise APIError('배포 조회 응답의 식별자가 요청과 다릅니다.', code='INVALID_RESPONSE')
        return value

    def deployment_listing(self, cursor=''):
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError('invalid deployment cursor')
        base = origin(os.environ.get('MODEL_GATEWAY_URL', 'http://gateway:8000'))
        query = urllib.parse.urlencode({'limit': 100, 'status': 'ready', 'runtime_state': 'ready', 'cursor': cursor})
        value = self._object(self._request('GET', '/api/deployments?' + query, base=base, timeout=5))
        rows, next_cursor = value.get('items'), value.get('next_cursor', '')
        if not isinstance(rows, list) or len(rows) > 100 or not isinstance(next_cursor, str) or len(next_cursor) > 2048:
            raise APIError('모델 목록 응답 형식이 올바르지 않습니다.', code='INVALID_RESPONSE')
        try:
            ids = [identifier(row.get('deployment_id')) for row in rows if isinstance(row, dict)]
            if len(ids) != len(rows) or len(set(ids)) != len(ids): raise ValueError()
        except ValueError:
            raise APIError('모델 목록 식별자가 올바르지 않습니다.', code='INVALID_RESPONSE') from None
        return value
