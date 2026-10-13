"""Synthetic GET-only upstreams for isolated Docker acceptance, never production data."""
from datetime import datetime, timezone
import http.server
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

# Imported from the copied source, not from production state or any test package.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.ops.platform.alerts import GROUP, RULES, SCOPE
from src.ops.platform.ops.discovery_metrics import SIGNATURES, probe_query


def response(role, path, control):
    now = datetime.now(timezone.utc)
    version = int(control.get('version', 0))
    jobs = {r: 'acceptance-' + r + ('-v' + str(version) if r == 'host' else '') for r in SIGNATURES}
    labels = lambda r: {'job': jobs[r], 'instance': 'synthetic-' + r + ':8080'}
    sample = lambda lab, val='1': {'metric': lab, 'value': [now.timestamp(), val]}
    vector = lambda values: {'status': 'success', 'data': {'resultType': 'vector', 'result': values}}
    if path == '/fixture/ready': return 200, {'ready': True, 'synthetic': True}
    if control.get('health_fault') and role == 'prometheus': return 503, {'error': 'synthetic_outage'}
    if role in ('model','manual-model','data','knowledge') and path == '/health':
        return 200, {'status': 'ok', 'service': 'model-gateway'}
    if role == 'agent' and path == '/healthz': return 200, {'status': 'ok', 'platform': 'agent'}
    if role == 'langfuse' and path.startswith('/api/public/health'):
        return 200, {'status': 'OK', 'version': 'synthetic'}
    if role == 'alertmanager':
        if path == '/api/v2/status': return 200, {'versionInfo': {'version': 'synthetic'}}
        if path.startswith('/api/v2/alerts?'): return 200, []
    if role == 'prometheus':
        if path == '/api/v1/status/buildinfo': return 200, {'status': 'success', 'data': {'version': 'synthetic'}}
        if path == '/api/v1/targets?state=active':
            return 200, {'status': 'success', 'data': {'activeTargets': [
                {'labels': labels(r), 'health': 'up', 'lastScrape': now.isoformat()} for r in jobs]}}
        if path.startswith('/api/v1/rules?'):
            if control.get('rules_fault'): return 503, {'error': 'synthetic_rules_outage'}
            return 200, {'status': 'success', 'data': {'groups': [{'name': GROUP, 'rules': [
                {'name': name, 'type': 'alerting', 'duration': duration, 'labels': {'scope': SCOPE},
                 'state': 'inactive', 'health': 'ok', 'lastEvaluation': now.isoformat(), 'alerts': []}
                for name, (_, duration) in RULES.items()]}]}}
        if path.startswith('/api/v1/query?'):
            query = parse_qs(urlsplit(path).query).get('query', [''])[0]
            for r, metrics in SIGNATURES.items():
                if query in {probe_query(metric) for metric in metrics}: return 200, vector([sample(labels(r))])
            if query.startswith('count by (job,instance,cluster,mountpoint)'):
                return 200, vector([sample({**labels('host'), 'mountpoint': v}) for v in ('/', '/synthetic/data')])
            # Protocol fixture only. No PromQL evaluation or real resource measurement is claimed.
            if query.startswith('up{'): return 200, vector([sample(labels(r)) for r in jobs])
            if 'node_cpu_seconds_total' in query: return 200, vector([sample(labels('host'), '12')])
            if 'node_memory_' in query: return 200, vector([sample(labels('host'), '34')])
            if 'node_filesystem_' in query:
                return 200, vector([sample({**labels('host'), 'mountpoint': '/', 'device': 'synthetic', 'fstype': 'ext4'}, '45')])
            if 'container_cpu_usage_seconds_total' in query:
                return 200, vector([sample({**labels('containers'), 'name': 'synthetic-container'}, '0.1')])
            if 'container_memory_working_set_bytes' in query:
                return 200, vector([sample({**labels('containers'), 'name': 'synthetic-container'}, '1024')])
            if 'DCGM_FI_' in query:
                return 200, vector([sample({**labels('gpu'), 'UUID': 'GPU-synthetic', 'gpu': '0',
                    'ops_metric_name': metric}, '1') for metric in SIGNATURES['gpu']])
    return 404, {'error': 'fixture_path_not_supported'}


def serve(role, control, host='0.0.0.0', port=8080):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def send(self, status, body):
            raw=json.dumps(body).encode(); self.send_response(status)
            self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            # Upstreams must never receive Ops login, Agent keys or Kubernetes credentials.
            if self.headers.get('Authorization') or self.headers.get('Cookie'):
                self.send(400, {'error':'fixture_credentials_refused'}); return
            try: status, body=response(role,self.path,json.loads(control.read_text()))
            except (OSError,ValueError,KeyError): status,body=503,{'error':'fixture_control_unavailable'}
            self.send(status,body)
        def do_POST(self): self.send(405,{'error':'fixture_get_only'})
        do_PUT=do_PATCH=do_DELETE=do_POST
    return http.server.ThreadingHTTPServer((host,port),Handler)


if __name__ == '__main__':
    serve(sys.argv[1],Path(sys.argv[2])).serve_forever()
