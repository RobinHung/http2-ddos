from __future__ import print_function

import argparse
import datetime
import functools
import itertools
import logging
import signal
import ssl
import time

from hyperframe.frame import SettingsFrame

from tornado_http2_client import H2Client

from tornado import gen
from tornado import queues
from tornado.simple_httpclient import SimpleAsyncHTTPClient
from tornado.ioloop import IOLoop, PeriodicCallback

logger = logging.getLogger()

__author__ = 'bennettaur'


class RequestHTTP2TestRunner(object):

    def __init__(self, target_list, rate, concurrency, workers, host, port):
        self.io_loop = IOLoop()
        self.io_loop.make_current()
        self.target_list_iterator = self.make_target_list_iterator(target_list)
        self.rate = rate
        self.concurrency = concurrency
        self.workers = workers
        self.host = host
        self.port = int(port)

        self.queue = queues.Queue()
        self.periodic_callback_time = 1000 / (self.rate / self.concurrency)
        self.periodic_callback = PeriodicCallback(
            self.add_to_queue,
            self.periodic_callback_time,
            io_loop=self.io_loop
        )

        self.connection = None
        self.create_connection_object()
        logger.debug("Created a {} connection".format(self.connection))
        self.running_workers = 0
        self.running = False
        self.start_time = None
        self.requests_made = 0
        self.requests_finish = 0
        self.status_codes = {}

    @gen.coroutine
    def add_to_queue(self):
        for _ in xrange(self.concurrency):
            target = next(self.target_list_iterator)
            try:
                self.queue.put_nowait(target)
            except queues.QueueFull:
                pass
            except Exception as e:
                logger.exception("Exception while adding {} to the queue: {}".format(targets, e))

    def create_connection_object(self):
        self.connection = H2Client(io_loop=self.io_loop)

    def create_connection(self):
        return self.connection.connect(self.host, self.port)

    def clean_up(self):
        self.connection.close_connection()

    def get_data_size(self):
        return self.connection.data_received_size

    def handle_result(self, result):
        headers = result.get('headers', [])
        status = None
        for header, value in headers:
            if header == ":status":
                status = value
                break
        logger.info("Result was {}".format(status))
        try:
            self.status_codes[status] += 1
        except KeyError:
            self.status_codes[status] = 1

    def make_target_list_iterator(self, target_list):
        return itertools.cycle(target_list)

    def make_request(self, target):
        return self.connection.fetch(target)

    @gen.coroutine
    def run(self):
        self.running = True

        yield self.create_connection()
        for x in xrange(self.workers):
            self.worker(x)

        self.start_time = time.time()
        self.periodic_callback.start()

    def start(self):
        self.io_loop.add_callback(self.run)

        try:
            self.io_loop.start()
        except KeyboardInterrupt:
            pass

        self.running = False

        total_time = time.time() - self.start_time

        print(
            ("Total Time: {}\n"
             "Total Requests Sent: {}\n"
             "Total Requests Finished: {}\n"
             "Avg Sent: {} rps\n"
             "Avg Fin: {} rps\n"
             "Data Recvd: {} Bytes\n"
             "Bandwidth: {} Bps\n"
             "Response Code Counts: {}").format(
                total_time,
                self.requests_made,
                self.requests_finish,
                self.requests_made / total_time,
                self.requests_finish / total_time,
                self.get_data_size(),
                self.get_data_size() / total_time,
                self.status_codes
            )
        )

        self.clean_up()

    def stop(self):
        self.periodic_callback.stop()
        self.io_loop.stop()

    @gen.coroutine
    def worker(self, id):
        self.running_workers += 1
        logger.debug("Worker {} starting".format(id))
        while True:
            if not self.running:
                # Skip running the consumer if the attack is no longer running
                return
            targets = []
            try:
                # Timeout periodically to check if this droplet is still running
                first_target = yield self.queue.get(timeout=datetime.timedelta(milliseconds=4))
            except gen.TimeoutError:
                continue
            else:
                targets.append(first_target)

                for _ in xrange(self.concurrency):
                    try:
                        other_target = self.queue.get_nowait()
                    except queues.QueueEmpty:
                        pass
                    else:
                        targets.append(other_target)

            logger.info("Requesting: {}".format(targets))
            self.requests_made += len(targets)
            try:
                results = yield [self.make_request(target) for target in targets]
            except Exception as e:
                logger.exception("Exception while requesting {}: {}".format(targets, e))
            else:
                self.requests_finish += len(targets)
                for result in results:
                    self.handle_result(result)

                try:
                    self.queue.task_done()
                except queues.QueueEmpty:
                    pass
                except ValueError:
                    print("A worker got a ValueError while marking a task as done")

        logger.debug("Worker {} ending".format(id))
        self.running_workers -= 1


class RequestHTTP11TestRunner(RequestHTTP2TestRunner):

    def __init__(self, *args, **kwargs):
        super(RequestHTTP11TestRunner, self).__init__(*args, **kwargs)

        self.ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

        self.data_received = 0

    @gen.coroutine
    def create_connection(self):
        return

    def create_connection_object(self):
        self.connection = SimpleAsyncHTTPClient(io_loop=self.io_loop, max_clients=10000, force_instance=True)

    def clean_up(self):
        self.connection.close()

    def get_data_size(self):
        return self.data_received

    def handle_result(self, result):
        logger.info("Result was {}".format(result.code))
        self.data_received += sum(len(key) + len(value) + 2 for key, value in result.headers.get_all()) + len(result.body)

        try:
            self.status_codes[result.code] += 1
        except KeyError:
            self.status_codes[result.code] = 1

    def make_target_list_iterator(self, target_list):
        return itertools.cycle("https://{}:{}{}".format(self.host, self.port, target) for target in target_list)

    def make_request(self, target):
        return self.connection.fetch(target, ssl_options=self.ssl_context, raise_error=False)


def build_target_list(file_path):
    with open(file_path) as target_file:
        target_list = list(target.strip() for target in target_file)

    return target_list


def handle_terminate(test_runner, signalnum, frame):
    test_runner.io_loop.add_callback_from_signal(test_runner.stop)


def run_tests(port, rate, concurrency, workers, file_path, host, http_version):
    target_list = [
        '/'
    ]
    target_list.extend(build_target_list(file_path=file_path))

    runner = None
    if http_version == 1:
        runner = RequestHTTP11TestRunner(target_list, rate, concurrency, workers, host, port)
    elif http_version == 2:
        runner = RequestHTTP2TestRunner(target_list, rate, concurrency, workers, host, port)

    handler = functools.partial(handle_terminate, runner)
    signal.signal(signal.SIGINT, handler)

    runner.start()


if __name__ == "__main__":

    logging.basicConfig(
        format='%(asctime)s--%(name)s--%(levelname)s: %(message)s',
        datefmt='%m/%d/%Y %I:%M:%S %p',
        level=logging.INFO
    )

    parser = argparse.ArgumentParser()

    parser.add_argument("-r", dest="rate", type=int, help="The rate at which requests will attempted to be sent at")
    parser.add_argument("-p", dest="port", type=int, help="Target Port to connect to")
    parser.add_argument("-w", dest="workers", type=int, default=100, help="The number of concurrent workers to use")
    parser.add_argument("-c", dest="concurrency", type=int, default=1, help="The number of concurrent requests that each worker will try to make")
    parser.add_argument("-f", dest="file_path", help="Path to a line-separated file of assets to request")
    parser.add_argument("-v", dest="http_version", type=int, choices=[1, 2], help="Which version of HTTP to use")
    parser.add_argument("-l", dest="disable_logging", action="store_true", default=False, help="Disable logging each asset being requested and the responses")
    parser.add_argument("-t", dest="host", help="The target host to connect to")

    args = parser.parse_args()

    if args.disable_logging:
        logging.disable(logging.ERROR)

    run_tests(
        port=args.port,
        rate=args.rate,
        concurrency=args.concurrency,
        workers=args.workers,
        file_path=args.file_path,
        host=args.host,
        http_version=args.http_version
    )
