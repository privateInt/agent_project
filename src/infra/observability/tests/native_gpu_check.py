#!/usr/bin/env python3
"""Real Prometheus configuration transition/WAL check with HTTP GPU fixtures.

Requires the official Prometheus 3.13.3 binary and PyYAML in the test environment.
Production CLI needs only Python stdlib. No Docker, NVIDIA device or inference used.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

import yaml
from native_restart_check import Handler as HostHandler
from test_gpu import gpu, DEVICES, VALUES, UUID, text_metrics


class Handler(HostHandler):
    def do_GET(self):
        if self.path != '/gpu':
            return super().do_GET()
        body = (text_metrics().replace('gpu="0"', 'gpu="0",pod="discard-me"')
                + '\nDCGM_FI_PROF_GR_ENGINE_ACTIVE{gpu="0"} 0.5\n').encode()
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
    assert 'version 3.13.3 ' in subprocess.check_output([str(args.prometheus), '--version'], text=True)
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='o01-native-gpu-') as td:
        root = Path(td)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        config = root / 'prometheus.yml'
        def configuration(name):
            data = yaml.safe_load((source / name).read_text())
            data['global']['scrape_interval'] = '1s'
            data['global']['scrape_timeout'] = '1s'
            for job in data['scrape_configs']:
                target = port if job['job_name'] == 'prometheus' else server.server_port
                job['static_configs'] = [{'targets': ['127.0.0.1:' + str(target)]}]
                if job['job_name'] == 'gpu':
                    job['metrics_path'] = '/gpu'
            config.write_text(json.dumps(data))
        process = None
        prom = gpu.op.Prometheus('http://127.0.0.1:' + str(port))
        log = (root / 'prometheus.log').open('w')
        def launch():
            return subprocess.Popen([str(args.prometheus), '--config.file=' + str(config),
                                     '--storage.tsdb.path=' + str(root / 'tsdb'),
                                     '--web.listen-address=127.0.0.1:' + str(port)],
                                    stdout=log, stderr=subprocess.STDOUT)
        try:
            configuration('prometheus/prometheus.yml')
            process = launch()
            def before_ready():
                end = '%.3f' % (time.time() - 15)
                history = gpu.retained.history(prom, end)
                gpu.op.require(all(len(row['values']) >= 2 for rows in history.values() for row in rows),
                               'Need two historical scrapes')
                return end, history, gpu.retained.process_start(prom)
            end, history, start = gpu.wait_for(before_ready, 45)
            process.terminate()
            process.wait(timeout=20)
            configuration('gpu/prometheus.yml')
            process = launch()
            def after_ready():
                assert process.poll() is None, 'Prometheus exited'
                gpu.op.require(gpu.retained.process_start(prom) > start, 'Need new process')
                values = {metric: gpu.op.values(prom.query(metric + '{job="gpu"}')) for metric in gpu.METRICS}
                report = gpu.validate_metrics(values, DEVICES)
                assert report[UUID] == VALUES
                assert all('pod' not in labels for rows in values.values() for labels, _ in rows)
                assert not prom.query('DCGM_FI_PROF_GR_ENGINE_ACTIVE')
                targets = prom.get('/api/v1/targets')['activeTargets']
                gpu.op.require(len(targets) == 4 and all(t['health'] == 'up' for t in targets), 'Need four healthy targets')
                assert gpu.retained.history(prom, end) == history
                return report
            gpu.wait_for(after_ready, 30)
            result = {'result': 'passed', 'prometheus': '3.13.3',
                      'scope': 'real native Prometheus / HTTP GPU fixture / same temporary TSDB',
                      'targets_up': 4, 'gpu_fields_verified': list(gpu.METRICS),
                      'unselected_metrics_and_labels_dropped': True,
                      'historical_counts': gpu.retained.counts(history), 'raw_history_preserved': True,
                      'test_scrape_interval_seconds': 1, 'production_configuration_changed': False,
                      'real_gpu_and_dcgm_container_assessed': False, 'inference_executed': False}
            args.output.write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2))
        except Exception:
            log.flush()
            print((root / 'prometheus.log').read_text()[-4000:])
            raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            log.close()
            server.shutdown(); server.server_close(); worker.join()


if __name__ == '__main__':
    main()
