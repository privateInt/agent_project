"""Server-side Gateway client. Never follow model endpoints or retry POSTs."""
import json
import os
import uuid
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class APIError(RuntimeError):
    def __init__(self, message, status=0, code=""):
        super().__init__(message)
        self.status, self.code = status, code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base=None):
        self.base = (base or os.environ.get("MODEL_GATEWAY_URL", "http://127.0.0.1:13000")).rstrip("/")
        url = urlsplit(self.base)
        if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment or url.path:
            raise ValueError("MODEL_GATEWAY_URL must be an HTTP(S) origin")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, method, path, body=None, key=None, timeout=30):
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        return self._request(method, path, data, 'application/json' if body is not None else None, key, timeout)

    def upload_dataset(self, dataset_id, version, data, key, timeout=150):
        dataset_id = str(uuid.UUID(dataset_id))
        if not version.strip() or not isinstance(data, bytes) or not data or len(data) > 64 * 1024 * 1024:
            raise APIError('버전 이름과 64 MiB 이하 JSONL 파일이 필요합니다.', 400, 'INVALID_UPLOAD')
        version_bytes = version.strip().encode('utf-8')
        boundary = uuid.uuid4().hex.encode()
        while boundary in data or boundary in version_bytes:
            boundary = uuid.uuid4().hex.encode()
        payload = (b'--' + boundary + b'\r\nContent-Disposition: form-data; name="version"\r\n\r\n' + version_bytes
                   + b'\r\n--' + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="dataset.jsonl"\r\n'
                   + b'Content-Type: application/jsonl\r\n\r\n' + data + b'\r\n--' + boundary + b'--\r\n')
        return self._request('POST', '/api/datasets/' + dataset_id + '/versions', payload,
                             'multipart/form-data; boundary=' + boundary.decode(), key, timeout)

    def _request(self, method, path, data, content_type, key, timeout):
        if not path.startswith("/api/") or "#" in path:
            raise ValueError("only Gateway API paths are allowed")
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if key:
            headers["Idempotency-Key"] = key
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise APIError("응답이 너무 큽니다.", code="INVALID_RESPONSE")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("object response required")
                return result
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(65536))
                code = error.get("code", error.get("grpc_code", ""))
            except (ValueError, AttributeError):
                code = ""
            raise APIError("요청을 처리하지 못했습니다.", exc.code, code) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise APIError("서버에 연결하지 못했습니다. 잠시 후 다시 확인해 주세요.", code="CONNECTION_ERROR") from None
        except (ValueError, UnicodeError):
            raise APIError("서버 응답을 확인하지 못했습니다.", code="INVALID_RESPONSE") from None

    def listing(self, collection, **filters):
        query = urlencode({k: v for k, v in filters.items() if v not in (None, "")})
        return self.call("GET", "/api/" + collection + ("?" + query if query else ""))
