"""O04 projections of the chosen engines. No rule evaluation or write transport."""
from datetime import datetime, timezone
import hashlib
import re
import urllib.parse

from .client import Client
from .contracts import require, timestamp
from .observations import safe_label
from .transport import Transport

SCOPE = 'agent-project-o04'
GROUP = 'agent-project-o04'
DEFAULT_URL = 'http://127.0.0.1:16330'
RULES = {
    'OpsTargetDown': ('수집 연결 실패', 120),
    'OpsTargetMissing': ('수집 대상 누락', 300),
    'OpsHostCpuHigh': ('호스트 CPU 사용량 높음', 900),
    'OpsHostMemoryHigh': ('호스트 메모리 부족', 600),
    'OpsFilesystemHigh': ('디스크 여유 공간 부족', 900),
    'OpsGpuTemperatureHigh': ('GPU 온도 높음', 300),
    'OpsGpuTemperatureMissing': ('GPU 온도 관측값 없음', 300),
    'OpsRuleEvaluationFailures': ('경보 규칙 평가 실패', 120),
}
CONDITIONS = {
    'OpsTargetDown':'수집 실패가 2분 지속',
    'OpsTargetMissing':'90초 동안 수집값 없음이 5분 지속',
    'OpsHostCpuHigh':'5분 평균 사용률 90% 초과가 15분 지속',
    'OpsHostMemoryHigh':'사용률 90% 초과가 10분 지속',
    'OpsFilesystemHigh':'대상 파일시스템 사용률 90% 초과가 15분 지속',
    'OpsGpuTemperatureHigh':'관측 온도 85°C 초과가 5분 지속',
    'OpsGpuTemperatureMissing':'수집기는 응답하지만 유효 온도가 없음이 5분 지속',
    'OpsRuleEvaluationFailures':'최근 5분의 평가 오류 증가가 2분 지속',
}
LABELS = {'alertname','scope','severity','job','instance','device','mountpoint','UUID','gpu','modelName'}


def labels(raw):
    require(isinstance(raw, dict) and len(raw) <= 32)
    require(all(isinstance(k,str) and isinstance(v,str) and len(k)<=128 and len(v)<=512
                for k,v in raw.items()))
    require(raw.get('scope') == SCOPE and raw.get('alertname') in RULES, 'ALERT_SCOPE_MISMATCH')
    require(raw.get('severity') in ('warning', 'critical'))
    return {k: safe_label(v) for k, v in raw.items() if k in LABELS}


def recent(value):
    value = timestamp(value)
    require(value is not None)
    normalized = re.sub(r'\.(\d+)',lambda m:'.'+(m.group(1)+'000000')[:6],value)
    age = (datetime.now(timezone.utc)-datetime.fromisoformat(normalized.replace('Z','+00:00'))).total_seconds()
    return value, -5 <= age <= 90


class ArrayTransport(Transport):
    root_type = list


class Alerts(Client):
    def __init__(self, source, url, times, timeout=5):
        super().__init__('model', url, timeout=timeout, success_times=times)
        self.platform = source
        self.binding = hashlib.sha256((source+'|o04|'+url).encode()).hexdigest()
        if source == 'alertmanager':
            self.transport = ArrayTransport(url, timeout=timeout)

    def rules(self):
        def convert(raw, status):
            require(raw.get('status') == 'success' and isinstance(raw.get('data'), dict))
            groups = raw['data'].get('groups')
            require(isinstance(groups, list) and len(groups) == 1, 'ALERT_RULESET_UNAVAILABLE')
            group = groups[0]
            require(isinstance(group, dict) and group.get('name') == GROUP, 'ALERT_RULESET_UNAVAILABLE')
            rows = group.get('rules')
            require(isinstance(rows, list) and len(rows) == len(RULES), 'ALERT_RULESET_UNAVAILABLE')
            result, seen, alert_count = [], set(), 0
            for rule in rows:
                require(isinstance(rule, dict))
                name = rule.get('name')
                require(name in RULES and name not in seen, 'ALERT_RULESET_UNAVAILABLE')
                seen.add(name)
                require(rule.get('type') == 'alerting' and rule.get('duration') == RULES[name][1]
                        and isinstance(rule.get('labels'), dict) and rule['labels'].get('scope') == SCOPE,
                        'ALERT_RULESET_UNAVAILABLE')
                require(rule.get('state') in ('inactive','pending','firing'))
                evaluated, fresh = recent(rule.get('lastEvaluation'))
                require(rule.get('health') in ('ok','err','unknown'))
                health = rule['health'] if fresh else 'stale'
                alerts = rule.get('alerts')
                require(isinstance(alerts, list))
                alert_count += len(alerts)
                require(alert_count <= 500, 'RESPONSE_TOO_LARGE')
                active, identities = [], set()
                for alert in alerts:
                    require(isinstance(alert, dict) and alert.get('state') in ('pending','firing'))
                    lab = labels(alert.get('labels'))
                    require(lab['alertname'] == name)
                    # Full label identity: avoid merging different filesystems/GPUs.
                    identity = tuple(sorted(alert['labels'].items()))
                    require(identity not in identities, 'DUPLICATE_IDENTITY')
                    identities.add(identity)
                    active.append({'labels':lab, 'state':alert['state'], 'active_at':timestamp(alert.get('activeAt'))})
                require((rule['state']=='inactive') == (not active))
                result.append({'id':name, 'title':RULES[name][0], 'state':rule['state'],
                               'condition':CONDITIONS[name],
                               'health':health, 'last_evaluation':evaluated, 'hold_seconds':RULES[name][1],
                               'alerts':active})
            return {'items':result, 'active_count':alert_count,
                    'evaluation_healthy':all(r['health']=='ok' for r in result) and not (raw.get('warnings') or raw.get('infos'))}
        path = '/api/v1/rules?'+urllib.parse.urlencode({'type':'alert','rule_group[]':GROUP})
        out = self._read(path, convert)
        out.pop('source_path', None)
        if out['status'] == 'ok' and not out['data']['evaluation_healthy']:
            out['status'] = 'partial'
        return out

    def active(self):
        def convert(raw, status):
            require(len(raw) <= 500, 'RESPONSE_TOO_LARGE')
            result, seen = [], set()
            for alert in raw:
                require(isinstance(alert, dict))
                lab = labels(alert.get('labels'))
                fp = alert.get('fingerprint')
                require(isinstance(fp, str) and re.fullmatch('[0-9a-f]{16}', fp))
                require(fp not in seen, 'DUPLICATE_IDENTITY'); seen.add(fp)
                state = alert.get('status')
                require(isinstance(state, dict) and state.get('state') in ('active','suppressed','unprocessed'))
                for key in ('silencedBy', 'inhibitedBy'):
                    require(isinstance(state.get(key), list) and len(state[key]) <= 100)
                result.append({'labels':lab, 'fingerprint':fp, 'state':state['state'],
                               'silenced':bool(state['silencedBy']), 'inhibited':bool(state['inhibitedBy']),
                               'started_at':timestamp(alert.get('startsAt')), 'ends_at':timestamp(alert.get('endsAt'))})
            return {'items':result, 'count':len(result)}
        path = '/api/v2/alerts?'+urllib.parse.urlencode({'filter':'scope="'+SCOPE+'"',
                'active':'true','silenced':'true','inhibited':'true','unprocessed':'true'})
        out = self._read(path, convert); out.pop('source_path', None)
        return out
