from __future__ import print_function

import ssl
import sys
import time

from hyperframe.frame import SettingsFrame

from tornado_http2_client import H2Client

from tornado import gen
from tornado.ioloop import IOLoop
from tornado.simple_httpclient import SimpleAsyncHTTPClient

__author__ = 'bennettaur'

@gen.coroutine
def test_http_11(io_loop, host, port):
    ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    c = SimpleAsyncHTTPClient(io_loop=io_loop, max_clients=10000, force_instance=True)

    print("Sending HTTP/1.1 request")
    now = time.time()
    response = yield c.fetch("https://{}:{}/".format(host, port), ssl_options=ssl_context, raise_error=False)
    total_time = time.time() - now

    data_received = sum(len(key) + len(value) + 2 for key, value in response.headers.get_all()) + len(response.body)

    print(
        "Total data received: {} bytes\nDuration: {}s\nRate: {} Bps\n".format(
            data_received,
            total_time,
            data_received/total_time
        )
    )
    c.close()

@gen.coroutine
def test_no_server_push(io_loop, host, port):
    c = H2Client(io_loop=io_loop)

    yield c.connect(host, port)
    print("Disabling Server Push")
    yield c.update_settings({SettingsFrame.ENABLE_PUSH: 0})

    print("Sending HTTP/2 request")
    now = time.time()
    response = yield c.get_request("/")
    total_time = c.last_time_data_recvd - now

    print(
        "Pushes Received: {}\nTotal data received: {} bytes\nDuration: {}s\nRate: {} Bps\n".format(
            len(c.pushes),
            c.data_received_size,
            total_time,
            c.data_received_size/total_time
        )
    )
    c.close_connection()

@gen.coroutine
def test_server_push(io_loop, host, port):
    c = H2Client(io_loop=io_loop)

    yield c.connect(host, port)

    print("Sending HTTP/2 request with Server Push")
    now = time.time()
    response = yield c.get_request("/")
    yield gen.sleep(1)

    total_time = c.last_time_data_recvd - now

    print(
        "Pushes Received: {}\nTotal data received: {} bytes\nDuration: {}s\nRate: {} Bps\n".format(
            len(c.pushes),
            c.data_received_size,
            total_time,
            c.data_received_size/total_time
        )
    )
    #print response
    c.close_connection()


@gen.coroutine
def run_tests(io_loop, host, port):
    yield test_http_11(io_loop=io_loop, host=host, port=port)
    yield test_no_server_push(io_loop=io_loop, host=host, port=port)
    yield test_server_push(io_loop=io_loop, host=host, port=port)
    io_loop.stop()


host = sys.argv[1]
port = int(sys.argv[2])
io_loop = IOLoop.current()

io_loop.add_callback(run_tests, io_loop=io_loop, host=host, port=port)

io_loop.start()
