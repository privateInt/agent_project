#!/usr/bin/env python3
"""Optional real Prometheus process/WAL test. Exporters and Docker ownership are fixtures.

python3 -B native_restart_check.py --prometheus /verified/prometheus --output /scratch/result.json
Uses ephemeral loopback ports and a temporary TSDB; never invokes Docker or production APIs.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_restart_check import Runtime, check, op, stamp


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = ('node_cpu_seconds_total{cpu="0",mode="idle"} 10\n'
                'node_memory_MemTotal_bytes 8589934592\n'
                'node_filesystem_avail_bytes{mountpoint="/",fstype="ext4"} 1234567\n'
                'container_cpu_usage_seconds_total{name="agent-platform-fixture"} 1\n'
                'container_memory_working_set_bytes{name="agent-platform-fixture"} 1024\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prometheus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    version = subprocess.check_output([str(args.prometheus), '--version'], text=True)
    assert 'version 3.13.3 ' in version, version
    with tempfile.TemporaryDirectory(prefix='o01-native-restart-') as td:
        root = Path(td)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            prom_port = sock.getsockname()[1]
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        configuration = {'global': {'scrape_interval': '1s', 'scrape_timeout': '1s'},
                         'scrape_configs': [{'job_name': name, 'static_configs': [
                             {'targets': ['127.0.0.1:' + str(prom_port if name == 'prometheus' else server.server_port)]}]
                         } for name in ('prometheus', 'host', 'containers')]}
        config = root / 'prometheus.yml'
        config.write_text(json.dumps(configuration))
        log = (root / 'prometheus.log').open('w')
        process = None

        def launch():
            return subprocess.Popen([str(args.prometheus), '--config.file=' + str(config),
                                     '--storage.tsdb.path=' + str(root / 'prometheus'),
                                     '--web.listen-address=127.0.0.1:' + str(prom_port)],
                                    stdout=log, stderr=subprocess.STDOUT)

        class NativeRuntime(Runtime):
            def metrics(self, names):
                values = op.values(self.prom.query('up'))
                op.require(len(values) == 3 and all(v == 1 for _, v in values), 'fixture targets not ready')
                return {'targets_up': 3, 'platform_containers_verified': 1}

            def restart(self, containers):
                nonlocal process
                assert check.read_record(self.baseline_path)['restart']['command_returned'] is False
                self.effects.append(containers)
                process.terminate()
                process.wait(timeout=20)
                process = launch()
                for item in self.items.values():
                    item['started_at'] = stamp(time.time())

        runtime = NativeRuntime(root)
        runtime.prom = op.Prometheus('http://127.0.0.1:' + str(prom_port))
        try:
            process = launch()
            deadline = time.monotonic() + 40
            while True:
                assert process.poll() is None, 'Prometheus exited'
                try:
                    check.capture(runtime, runtime.baseline_path)
                    break
                except (ValueError, OSError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
            before = check.read_record(runtime.baseline_path)
            result = check.run_action(runtime, 'run', runtime.baseline_path, 30)
            saved = runtime.baseline_path.read_bytes()
            for action in ('compare', 'run'):
                again = check.run_action(runtime, action, runtime.baseline_path, 0)
                assert again['baseline_sha256'] == before['baseline_sha256']
                assert runtime.baseline_path.read_bytes() == saved
            assert len(runtime.effects) == 1
            result = {'result': 'passed', 'prometheus': '3.13.3',
                      'scope': 'real native Prometheus process restart / temporary TSDB and HTTP exporters',
                      'historical_counts': result['historical_counts'], 'raw_samples_preserved': True,
                      'new_scrapes_and_process_start_verified': True, 'repeat_run_restarts': 0,
                      'production_config_changed': False, 'test_scrape_interval_seconds': 1,
                      'docker_boundary': 'fixture only; actual three-container restart is pending on user server',
                      'inference_executed': False}
            args.output.write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2))
        except Exception:
            try:
                print('Process metric diagnostic:', runtime.prom.query('process_start_time_seconds'))
                print('Self target diagnostic:', runtime.prom.get('/api/v1/targets'))
            except Exception:
                pass
            log.flush()
            print((root / 'prometheus.log').read_text()[-4000:])
            raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    main()
