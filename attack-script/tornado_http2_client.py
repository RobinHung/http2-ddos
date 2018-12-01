# -*- coding: utf-8 -*-
"""
post_request.py
~~~~~~~~~~~~~~~

A short example that demonstrates a client that makes POST requests to certain
websites.

This example is intended to demonstrate how to handle uploading request bodies.
In this instance, a file will be uploaded. In order to handle arbitrary files,
this example also demonstrates how to obey HTTP/2 flow control rules.

Takes one command-line argument: a path to a file in the filesystem to upload.
If none is present, uploads this file.
"""
from __future__ import print_function

import logging
import mimetypes
import os
import socket
import ssl
import sys
import time

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

from h2.connection import H2Connection
from h2.connection import StreamClosedError as H2StreamClosedError
from h2.events import (
    ResponseReceived, DataReceived, StreamEnded, StreamReset, WindowUpdated,
    SettingsAcknowledged, PushedStreamReceived, TrailersReceived, RemoteSettingsChanged
)

from tornado import gen
from tornado.concurrent import Future
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream, SSLIOStream, StreamClosedError
from tornado.netutil import _client_ssl_defaults

logger = logging.getLogger()


__author__ = 'bennettaur'


class H2Client(object):

    ALPN_HTTP2_PROTOCOL = b'h2'
    USER_AGENT = 'Tornado 4.3 hyper-h2/1.0.0'

    def __init__(self, io_loop=None, config=None):
        self.io_loop = io_loop or IOLoop.current()
        self.conn = H2Connection()
        self.known_proto = None

        self.authority = None
        self.io_stream = None

        self.ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        self.ssl_context.set_alpn_protocols([self.ALPN_HTTP2_PROTOCOL])

        self.are_settings_acked = False
        self.pending_requests = []

        #self.settings_acked_future = Future()

        self.responses = {}
        self.pushes = {}

        self.data_received_size = 0
        self.last_time_data_recvd = None

    @gen.coroutine
    def connect(self, host, port):
        self.authority = host
        s = socket.socket()
        self.io_stream = SSLIOStream(s, ssl_options=self.ssl_context)

        yield self.io_stream.connect((host, port), server_hostname=host)
        logger.debug("Connected!")
        self.known_proto = self.io_stream.socket.selected_alpn_protocol()

        assert self.known_proto == self.ALPN_HTTP2_PROTOCOL, "ALPN protocol was not h2, was {} instead".format(
            self.known_proto)

        self.io_stream.set_close_callback(self.connection_lost)

        logger.debug("Talking to a valid HTTP2 server! Sending preamble")
        self.conn.initiate_connection()

        self.io_stream.read_until_close(streaming_callback=self.data_received)
        data = self.conn.data_to_send()
        yield self.io_stream.write(data)
        logger.debug("Preamble Sent! Should be connected now")

    @gen.coroutine
    def close_connection(self):
        self.io_stream.set_close_callback(lambda: None)
        self.conn.close_connection()

        data = self.conn.data_to_send()
        yield self.io_stream.write(data)

#    @gen.coroutine
    def data_received(self, data):
        """
        Called by Tornado when data is received on the connection.

        We need to check a few things here. Firstly, we want to validate that
        we actually negotiated HTTP/2: if we didn't, we shouldn't proceed!

        Then, we want to pass the data to the protocol stack and check what
        events occurred.
        """

        self.data_received_size += len(data)
        self.last_time_data_recvd = time.time()

        if not self.known_proto:
            self.known_proto = self.io_stream.socket.selected_alpn_protocol()
            assert self.known_proto == b'h2'

        events = self.conn.receive_data(data)

        for event in events:
            #print("Processing event: {}".format(event))
            if isinstance(event, ResponseReceived):
                self.handle_response(event)
            elif isinstance(event, DataReceived):
                self.handle_data(event)
            elif isinstance(event, StreamEnded):
                #self.end_stream(event)
                logger.debug("Got event Stream ended for stream {}".format(event.stream_id))
            elif isinstance(event, SettingsAcknowledged):
                self.settings_acked(event)
            elif isinstance(event, StreamReset):
                logger.debug("A Stream reset!: %d" % event.error_code)
            elif isinstance(event, WindowUpdated):
                self.window_updated(event)
            elif isinstance(event, PushedStreamReceived):
                self.handle_push(event)
            elif isinstance(event, TrailersReceived):
                self.handle_response(event, response_type="trailers")
            else:
                logger.debug("Received an event we don't handle: {}".format(event))

        data = self.conn.data_to_send()
        if data:
            #print("Responding to the server: {}".format(data))
            self.io_stream.write(data)

    def settings_acked(self, event):
        """
        Called when the remote party ACKs our settings. We send a SETTINGS
        frame as part of the preamble, so if we want to be very polite we can
        wait until the ACK for that frame comes before we start sending our
        request.
        """
        self.are_settings_acked = True
        #self.settings_acked_future.set_result(True)

    def _get_stream_response_holder(self, stream_id):
        try:
            return self.responses[stream_id]
        except KeyError:
            return self.pushes[stream_id]

    def handle_response(self, event, response_type="headers"):
        """
        Handle the response by storing the response headers.
        """
        try:
            response = self._get_stream_response_holder(event.stream_id)
        except KeyError:
            logger.exception("Unable to find a response future for stream {} while handling a response".format(event.stream_id))
        else:
            response[response_type] = event.headers
            if event.stream_ended is not None:
                try:
                    future = response.pop("future")
                except KeyError:
                    logger.exception("No future associated with the response for stream {} while handling a response".format(event.stream_id))
                else:
                    future.set_result(response)

                try:
                    self.end_stream(event.stream_ended)
                except H2StreamClosedError as e:
                    logger.exception("Got an exception trying to end a stream after handling a response for stream: {}".format(e.stream_id))

    def handle_data(self, event):
        """
        We handle data that's received
        """

        self.conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
        try:
            response = self._get_stream_response_holder(event.stream_id)
        except KeyError:
            logger.debug("Unable to find a response future for stream {} while handling a data. Adding one now".format(event.stream_id))
            # response = {'future': Future()}
            # self.responses[event.stream_id] = response
        else:
            if "data" not in response:
                response["data"] = event.data
            else:
                response["data"] = response["data"] + event.data
            if event.stream_ended is not None:
                try:
                    future = response.pop("future")
                except KeyError:
                    logger.debug("No future associated with the response for stream {} while handling data".format(event.stream_id))
                else:
                    future.set_result(response)

                try:
                    self.end_stream(event.stream_ended)
                except H2StreamClosedError as e:
                    logger.exception("Got an exception trying to end a stream after handling a response for stream: {}".format(e.stream_id))

    def handle_push(self, event):
        self.pushes[event.pushed_stream_id] = {
            "parent_stream_id": event.parent_stream_id,
            "request_headers": event.headers
        }

#    @gen.coroutine
    def end_stream(self, event):
        """
        We call this when the stream is cleanly ended by the remote peer. That
        means that the response is complete.
        """
        self.conn.end_stream(event.stream_id)
        yield self.io_stream.write(self.conn.data_to_send())
        logger.debug("Closed Stream {}".format(event.stream_id))

    def window_updated(self, event):
        """
        I don't think I actually need to do anything with this in Tornado, since sending data uses futures to continue
        """
        pass

    def connection_lost(self):
        """
        Called by Twisted when the connection is gone. Regardless of whether
        it was clean or not, we want to stop the reactor.
        """
        logger.debug("Connection was lost!")

    def get_request(self, path):
        return self.send_bodyless_request(path, "GET")

    fetch = get_request

    def head_request(self, path):
        return self.send_bodyless_request(path, "HEAD")

    @gen.coroutine
    def send_bodyless_request(self, path, method):
        # if not self.settings_acked_future.done():
        #     print("Settings haven't been acked, yield until they are")
        #     yield self.settings_acked_future
        #     print("Settings acked! Let's send this pending request")

        request_headers = [
            (':method', method),
            (':authority', self.authority),
            (':scheme', 'https'),
            (':path', path),
            ('user-agent', self.USER_AGENT),
        ]

        stream_id = self.conn.get_next_available_stream_id()
        logger.debug("Generating HEADER frame to send for stream_id {}".format(stream_id))
        self.conn.send_headers(stream_id, request_headers, end_stream=True)

        response_future = Future()
        self.responses[stream_id] = {'future': response_future}

        logger.debug("Writing out to the stream")
        yield self.io_stream.write(self.conn.data_to_send())
        logger.debug("Request sent! Waiting for response")

        result = yield response_future
        logger.debug("Got a result")
        raise gen.Return(result)

    def post_file_request(self, path, file_path):

        # First, we need to work out how large the file is.
        file_size = os.stat(file_path).st_size

        # Next, we want to guess a content-type and content-encoding.
        content_type, content_encoding = mimetypes.guess_type(file_path)

        # We can now open the file.
        file_obj = open(file_path, 'rb')

        self.post_request(
            path=path,
            file_obj=file_obj,
            file_size=file_size,
            content_type=content_type,
            content_encoding=content_encoding
        )

    def post_request(self, path, file_obj, file_size=None, content_type=None, content_encoding=None):
        """
        Send the POST request.

        A POST request is made up of one headers frame, and then 0+ data
        frames. This method begins by sending the headers, and then starts a
        series of calls to send data.
        """

        # if not self.settings_acked_future.done():
        #     print("Settings haven't been acked, yield until they are")
        #     yield self.settings_acked_future
        #     print("Settings acked! Let's send this pending post request")

        if type(file_obj) is str:
            file_size = len(file_obj)
            file_obj = StringIO(file_obj)

        # Now we can build a header block.
        request_headers = [
            (':method', 'POST'),
            (':authority', self.authority),
            (':scheme', 'https'),
            (':path', path),
            ('user-agent', self.USER_AGENT),
            ('content-length', str(file_size)),
        ]

        if content_type is not None:
            request_headers.append(('content-type', content_type))

            if content_encoding is not None:
                request_headers.append(('content-encoding', content_encoding))

        stream_id = self.conn.get_next_available_stream_id()

        self.conn.send_headers(stream_id, request_headers)

        # We now need to send all the relevant data. We do this by checking
        # what the acceptable amount of data is to send, and sending it. If we
        # find ourselves blocked behind flow control, we then place a deferred
        # and wait until that deferred fires.

        response_future = Future()

        self.responses[stream_id] = {'future': response_future}

        # We now need to send a number of data frames.
        try:
            while file_size > 0:
                # Firstly, check what the flow control window is for the current stream.
                window_size = self.conn.local_flow_control_window(stream_id=stream_id)

                # Next, check what the maximum frame size is.
                max_frame_size = self.conn.max_outbound_frame_size

                # We will send no more than the window size or the remaining file size
                # of data in this call, whichever is smaller.
                bytes_to_send = min(window_size, file_size)

                while bytes_to_send > 0:
                    chunk_size = min(bytes_to_send, max_frame_size)
                    data_chunk = file_obj.read(chunk_size)
                    self.conn.send_data(stream_id=stream_id, data=data_chunk)

                    yield self.io_stream.write(self.conn.data_to_send())

                    bytes_to_send -= chunk_size
                    file_size -= chunk_size
        except StreamClosedError:
            logger.warning("Connection was lost while sending stream {}".format(stream_id))
        else:
            self.conn.end_stream(stream_id=stream_id)
        finally:
            file_obj.close()

        result = yield response_future
        raise gen.Return(result)

    @gen.coroutine
    def update_settings(self, new_settings):
        self.conn.update_settings(new_settings=new_settings)
        data = self.conn.data_to_send()
        if data:
            self.io_stream.write(data)
