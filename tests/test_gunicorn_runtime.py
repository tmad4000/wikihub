"""Exercise the production process topology with real HTTP workers."""
import concurrent.futures
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
import json, os, time
from pathlib import Path
initialized_pid = os.getpid()
root = Path(os.environ['WORKER_TEST_ROOT'])
def app(environ, start_response):
    if environ['PATH_INFO'].startswith('/hold/'):
        root.joinpath(environ['PATH_INFO'].split('/')[-1]).touch()
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
            command = [sys.executable, '-m', 'gunicorn', '-c', str(Path(__file__).resolve().parents[1] / 'deploy/gunicorn.conf.py'), '--bind', f'127.0.0.1:{port}', 'fixture:app']
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
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        waiting = [pool.submit(fetch, f'/hold/ready-{i}', 20) for i in range(2)]
                        try:
                            deadline = time.monotonic() + 5
                            while not all(root.joinpath(f'ready-{i}').exists() for i in range(2)):
                                self.assertLess(time.monotonic(), deadline, 'both concurrent reads must enter')
                                time.sleep(.02)
                            # Two slow readers must not consume all service capacity.
                            health = fetch('/health', timeout=2)
                            self.assertEqual(health['pid'], health['initialized_pid'])
                        finally:
                            root.joinpath('release').touch()
                        for result in waiting:
                            value = result.result(timeout=3)
                            self.assertEqual(value['pid'], value['initialized_pid'])
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
