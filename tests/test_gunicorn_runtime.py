"""Exercise the production process topology with real HTTP workers."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


class GunicornRuntimeTest(unittest.TestCase):
    def test_reader_capacity_and_worker_local_initialization(self):
        with tempfile.TemporaryDirectory(prefix='wikihub-worker-test-') as directory:
            root = Path(directory)
            root.joinpath('fixture.py').write_text('''
import json, os, threading, time
from pathlib import Path
initialized_pid = os.getpid()
root = Path(os.environ['WORKER_TEST_ROOT'])
root.joinpath(f'initialized-{initialized_pid}').write_text(str(initialized_pid))
def app(environ, start_response):
    if environ['PATH_INFO'].startswith(f'/hold/{os.getpid()}/'):
        root.joinpath(environ['PATH_INFO'].split('/')[-1]).write_text(
            json.dumps({'pid': os.getpid(), 'thread': threading.get_ident()}))
        deadline = time.monotonic() + 15
        while not root.joinpath('release').exists() and time.monotonic() < deadline:
            time.sleep(.02)
    body = json.dumps({'pid': os.getpid(), 'initialized_pid': initialized_pid}).encode()
    start_response('200 OK', [('Content-Type', 'application/json'), ('Content-Length', str(len(body)))])
    return [body]
''')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            command = [sys.executable, '-m', 'gunicorn', '-c', str(Path(__file__).resolve().parents[1] / 'deploy/gunicorn.conf.py'), '--bind', f'127.0.0.1:{port}', '--keep-alive', '30', 'fixture:app']
            with root.joinpath('server.log').open('w') as log:
                process = subprocess.Popen(command, cwd=root, env={**os.environ, 'WORKER_TEST_ROOT': directory}, stdout=log, stderr=log)
                def fetch(path, timeout=2):
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=timeout) as response:
                        return json.load(response)
                try:
                    deadline = time.monotonic() + 12
                    while True:
                        try:
                            initial = fetch('/health')
                            break
                        except OSError:
                            if process.poll() is not None or time.monotonic() > deadline:
                                self.fail(root.joinpath('server.log').read_text())
                            time.sleep(.05)
                    self.assertEqual(initial['pid'], initial['initialized_pid'])
                    deadline = time.monotonic() + 5
                    while len(list(root.glob('initialized-*'))) < 2:
                        self.assertLess(time.monotonic(), deadline, 'two workers must initialize independently')
                        time.sleep(.02)
                    worker_pids = {int(path.read_text()) for path in root.glob('initialized-*')}
                    self.assertEqual(len(worker_pids), 2)
                    self.assertNotIn(process.pid, worker_pids)
                    self.assertIn(initial['pid'], worker_pids)

                    connections = {}
                    for pid in worker_pids:
                        for i in range(4):
                            deadline = time.monotonic() + 8
                            while True:
                                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
                                connection.request('GET', '/health')
                                response = connection.getresponse()
                                result = json.loads(response.read())
                                self.assertEqual(result['pid'], result['initialized_pid'])
                                if result['pid'] == pid:
                                    self.assertFalse(response.will_close)
                                    connections[f'ready-{pid}-{i}'] = connection
                                    break
                                connection.close()
                                self.assertLess(time.monotonic(), deadline, f'could not reach worker {pid}')
                                time.sleep(.01)

                    def hold_in_worker(pid, marker):
                        connection = connections[marker]
                        try:
                            connection.request('GET', f'/hold/{pid}/{marker}')
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            result = json.loads(response.read())
                            self.assertEqual(result['pid'], pid)
                            return result
                        finally:
                            connection.close()

                    def wait_for_entries(markers):
                        deadline = time.monotonic() + 8
                        while not all(root.joinpath(marker).exists() for marker in markers):
                            self.assertLess(time.monotonic(), deadline, 'concurrent reads must enter each worker')
                            time.sleep(.02)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                        waiting = []
                        markers = []
                        try:
                            for pid in worker_pids:
                                for i in range(3):
                                    marker = f'ready-{pid}-{i}'
                                    markers.append(marker)
                                    waiting.append(pool.submit(hold_in_worker, pid, marker))
                            wait_for_entries(markers)
                            health = fetch('/health', timeout=2)
                            self.assertEqual(health['pid'], health['initialized_pid'])
                            self.assertIn(health['pid'], worker_pids)
                            for pid in worker_pids:
                                marker = f'ready-{pid}-3'
                                markers.append(marker)
                                waiting.append(pool.submit(hold_in_worker, pid, marker))
                            wait_for_entries(markers)
                            for pid in worker_pids:
                                entries = [json.loads(root.joinpath(f'ready-{pid}-{i}').read_text()) for i in range(4)]
                                self.assertEqual({entry['pid'] for entry in entries}, {pid})
                                self.assertEqual(len({entry['thread'] for entry in entries}), 4)
                            self.assertTrue(all(not future.done() for future in waiting))
                        finally:
                            root.joinpath('release').touch()
                        results = [future.result(timeout=3) for future in waiting]
                        self.assertEqual({result['pid'] for result in results}, worker_pids)
                        for result in results:
                            self.assertEqual(result['pid'], result['initialized_pid'])
                finally:
                    root.joinpath('release').touch()
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
