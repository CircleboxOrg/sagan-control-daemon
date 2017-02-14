import sys

import time
from codecs import decode

import os
from io import BytesIO
from time import sleep

from requests import get, put
from threading import Thread, Event
from subprocess import Popen, PIPE, TimeoutExpired
from requests.exceptions import ConnectionError
import websocket

from websocket import WebSocketConnectionClosedException

_current_poller = None


def websocket_recv(ws: websocket.WebSocket, in_stream):
    while ws.connected:
        try:
            message = ws.recv()
            in_stream.write(message.encode())
        except (WebSocketConnectionClosedException, BrokenPipeError):
            break


def process_read(out_stream, ws: websocket.WebSocket, log_stream):
    while True:
        try:
            data = os.read(out_stream.fileno(), 512)
        except OSError:
            break
        if data == b'':
            break
        try:
            ws.send(decode(data))
            log_stream.write(data)
        except (BrokenPipeError, WebSocketConnectionClosedException):
            break


def process_error(out_stream, ws: websocket.WebSocket, log_stream):
    while True:
        try:
            data = os.read(out_stream.fileno(), 512)
        except OSError:
            break
        if data == b'':
            break
        try:
            ws.send(decode(data))
            log_stream.write(data)
        except (BrokenPipeError, WebSocketConnectionClosedException):
            break


def heart_beat(url, heart_beat_time, stop_trigger: Event):
    while not stop_trigger.is_set():
        response = put(url, {})
        if response.status_code not in (200, 204):
            exit(1)
        sleep(heart_beat_time)


def main():
    global _current_poller
    _current_poller = Poller(int(sys.argv[1]), sys.argv[2])
    _current_poller.go()


class Poller:
    def __init__(self, device_id, host):
        self.device_id = device_id
        self.host = host
        self.run_job = None
        self.experiment_process = None  # type: Popen
        self.results_stream = None
        self.error_stream = None
        self.stdout_text = b''
        self.stderr_text = b''
        self.state = 'polling'
        self.state_machine = {
            'polling': self.check_for_jobs,
            'running': self.run_experiment,
            'termination_requested': self.kill_subproc
        }

    def go(self):
        print('Device id: {}'.format(self.device_id))
        print('Awaiting work.')
        url = '{0}/dispatch/devices/{1}/heartbeat'.format(self.host, self.device_id)
        stop_event = Event()
        heart_beat_thread = Thread(target=heart_beat, args=(url, 5, stop_event))
        heart_beat_thread.start()
        while self.state is not 'exit':
            try:
                self.state_machine[self.state]()
            except ConnectionError as error:
                print(error)
                exit(1)

        stop_event.set()
        heart_beat_thread.join()

    def heartbeat(self):
        url = '{0}/dispatch/devices/{1}/heartbeat'.format(self.host, self.device_id)
        response = put(url, {'state': 0 if self.state is 'polling' else 1})
        if response.status_code not in (200, 204):
            exit(1)

    def check_for_jobs(self):
        url = '{0}/dispatch/devices/{1}/queue'.format(self.host, self.device_id)
        jobs = get(url).json()
        if len(jobs) > 0:
            next_job = jobs[0]
            print('Found job id {}, fetching experiment.'.format(next_job['id']))
            self.run_job = next_job['id']
            experiment = self.get_experiment(next_job['experiment'])
            self.start_experiment(experiment)
            self.notify_start()
            self.state = 'running'
        else:
            time.sleep(0.5)
        return

    def get_experiment(self, experiment_id):
        url = '{0}/dispatch/experiments/{1}'.format(self.host, experiment_id)
        return get(url).json()

    def get_state(self):
        url = '{0}/dispatch/jobs/{1}'.format(self.host, self.run_job)
        return get(url).json()['state']

    def notify_start(self):
        return put('{0}/dispatch/jobs/{1}/start'.format(self.host, self.run_job), {})

    def post_results(self):
        return put(
            '{0}/dispatch/jobs/{1}/finish'.format(self.host, self.run_job),
            {
                'out': self.out_log.getvalue(),
                'error': self.stderr_text
            }
        )

    def start_experiment_proc(self, experiment):
        with open('file.py', 'w') as f:
            f.write(experiment['code_string'])

        self.experiment_process = Popen(
            [sys.executable, '-u', 'file.py'],
            stdin=PIPE,
            stdout=PIPE,
            # stderr=PIPE,
            bufsize=0

        )

    def open_websocket(self, url):
        self.websocket = websocket.WebSocket()
        self.websocket.connect(url)

    def start_experiment(self, experiment):
        print('Starting experiment "{}".'.format(experiment['title']))
        self.open_websocket('ws://echo.websocket.org')
        self.start_experiment_proc(experiment)
        self.in_thread = Thread(target=websocket_recv, args=(self.websocket, self.experiment_process.stdin))
        self.out_log = BytesIO()
        self.out_thread = Thread(target=process_read,
                                 args=(self.experiment_process.stdout, self.websocket, self.out_log))
        self.in_thread.start()
        self.out_thread.start()

    def end_experiment(self):
        try:
            self.close_websocket()
            self.in_thread.join()
            self.out_thread.join()
            self.post_results()
        except:
            pass
        print('Job finished, awaiting work.')
        self.experiment_process = None
        self.stderr_text = b''
        self.stdout_text = b''

    def close_websocket(self):
        self.websocket.close()

    def run_experiment(self):
        try:
            self.experiment_process.wait(1)
            self.end_experiment()
            self.state = 'polling'
        except TimeoutExpired:
            state = self.get_state()
            if state == 2:
                self.kill_subproc()

    def kill_subproc(self):
        print('Terminating job.')
        self.experiment_process.terminate()
        try:
            self.experiment_process.wait(timeout=10)
        except TimeoutExpired:
            print('Process taking to long to terminate, killing.')
            self.experiment_process.kill()
            try:
                self.experiment_process.wait(timeout=10)
            except TimeoutExpired:
                print('WARNING: Experiment failed to stop.')
        self.post_results()
        self.end_experiment()
        self.state = 'polling'


if __name__ == '__main__':
    main()