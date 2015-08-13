from __future__ import absolute_import

import os
import urllib
import urlparse

from .. import idiokit, socket, ssl
from ..dns import host_lookup
from .server import write_headers, read_headers, normalized_headers, get_header_single, get_header_list, get_content_length, _RawWriter, _LimitedWriter, _ChunkedWriter, _Buffered, _Limited, _Chunked, ConnectionLost


@idiokit.stream
def write_request_line(socket, method, uri, http_version):
    yield socket.sendall("{0} {1} {2}\r\n".format(method, uri, http_version))


@idiokit.stream
def read_status_line(buffered):
    line = yield buffered.read_line()
    if not line:
        raise ConnectionLost()

    pieces = line.rstrip().split(" ", 2)
    if len(pieces) < 3:
        raise RuntimeError("could not parse request line")

    http_version, code, reason = pieces
    idiokit.stop(http_version, code, reason)


class ClientResponse(object):
    def __init__(self, http_version, status_code, status_reason, headers, buffered):
        self._http_version = http_version
        self._status_code = status_code
        self._status_reason = status_reason
        self._headers = headers
        self._reader = self._resolve_reader(http_version, headers, buffered)

    @property
    def http_version(self):
        return self._http_version

    @property
    def status_code(self):
        return self._status_code

    @property
    def status_reason(self):
        return self._status_reason

    @property
    def headers(self):
        return self._headers

    def _resolve_reader(self, http_version, headers, buffered):
        if http_version == "HTTP/1.0":
            return self._resolve_reader_http10(headers, buffered)
        if http_version == "HTTP/1.1":
            return self._resolve_reader_http11(headers, buffered)
        raise RuntimeError()

    def _resolve_reader_http10(self, headers, buffered):
        content_length = get_content_length(headers, None)
        if content_length is None:
            return buffered
        return _Limited(buffered, content_length)

    def _resolve_reader_http11(self, headers, buffered):
        transfer_encoding = get_header_list(headers, "transfer-encoding", None)
        content_length = get_content_length(headers, None)

        if transfer_encoding is not None:
            transfer_encoding = transfer_encoding.lower()

        if transfer_encoding == "chunked":
            return _Chunked(buffered)
        if transfer_encoding in (None, "identity") and content_length is not None:
            return _Limited(buffered, content_length)
        if transfer_encoding in (None, "identity"):
            return buffered
        raise ValueError("either content-length or transfer-encoding: chunked must be used")

    def read(self, amount):
        return self._reader.read(amount)


class ClientRequest(object):
    def __init__(self, method, uri, headers, writer, buffered):
        self._writer = writer
        self._buffered = buffered

    @property
    def method(self):
        return self._method

    @property
    def uri(self):
        return self._uri

    @property
    def headers(self):
        return self._headers

    def write(self, data):
        return self._writer.write(data)

    @idiokit.stream
    def finish(self):
        yield self._writer.finish()
        http_version, code, reason = yield read_status_line(self._buffered)
        headers = yield read_headers(self._buffered)
        idiokit.stop(ClientResponse(http_version, code, reason, headers, self._buffered))


class Client(object):
    def __init__(self, resolver=None, require_cert=True, ca_certs=None, timeout=60.0):
        self._resolver = resolver
        self._require_cert = require_cert
        self._ca_certs = ca_certs
        self._timeout = timeout

    @idiokit.stream
    def _unix_connect(self, socket_path):
        sock = socket.Socket(socket.AF_UNIX)
        yield sock.connect(socket_path, timeout=self._timeout)
        idiokit.stop(sock)

    def _tcp_connect(self, host, port):
        @idiokit.stream
        def _connect(port):
            family, ip = yield idiokit.next()
            sock = socket.Socket(family)
            yield sock.connect((ip, port), timeout=self._timeout)
            idiokit.stop(sock)
        return host_lookup(host, self._resolver) | _connect(port)

    @idiokit.stream
    def _init_ssl(self, sock, hostname):
        sock = yield ssl.wrap_socket(
            sock,
            require_cert=self._require_cert,
            ca_certs=self._ca_certs,
            timeout=self._timeout)
        if self._require_cert:
            cert = yield sock.getpeercert()
            ssl.match_hostname(cert, hostname)
        idiokit.stop(sock)

    @idiokit.stream
    def request(self, method, url, headers={}):
        parsed = urlparse.urlparse(url)
        if parsed.scheme == "http":
            sock = yield self._tcp_connect(parsed.hostname, 80 if parsed.port is None else parsed.port)
        elif parsed.scheme == "https":
            sock = yield self._tcp_connect(parsed.hostname, 443 if parsed.port is None else parsed.port)
            sock = yield self._init_ssl(sock, parsed.hostname)
        elif parsed.scheme == "http+unix":
            sock = yield self._unix_connect(os.path.join("/", urllib.unquote(parsed.hostname)))
        else:
            raise ValueError("unknown URI scheme '{0}'".format(parsed.scheme))

        writer, headers = self._resolve_headers(method, parsed.hostname, headers, sock)
        yield write_request_line(sock, method, "/" if parsed.path == "" else parsed.path, "HTTP/1.1")
        yield write_headers(sock, headers)
        idiokit.stop(ClientRequest(method, url, headers, writer, _Buffered(sock)))

    def _resolve_headers(self, method, host, headers, socket):
        headers = normalized_headers(headers)
        if headers.get("host", None) is None:
            headers["host"] = host

        connection = get_header_single(headers, "connection", "close")
        if connection.lower() != "close":
            raise ValueError("unknown connection value '{0}'".format(connection))
        headers["connection"] = connection

        transfer_encoding = get_header_list(headers, "transfer-encoding", None)
        content_length = get_content_length(headers, None)

        if transfer_encoding is not None:
            if transfer_encoding.lower() not in ("identity", "chunked"):
                raise ValueError("unknown transfer encoding '{0}'".format(transfer_encoding))
            transfer_encoding = transfer_encoding.lower()

        if method == "HEAD":
            if content_length not in (None, 0):
                raise ValueError("no content-length != 0 allowed for HEAD requests")
            writer = _LimitedWriter(socket, 0, "no response body allowed for HEAD requests")
            headers["content-length"] = 0
        elif transfer_encoding == "chunked":
            writer = _ChunkedWriter(socket)
        elif content_length is not None:
            writer = _LimitedWriter(socket, content_length, "content length set to {0} bytes".format(content_length))
            headers["content-length"] = content_length
        else:
            writer = _RawWriter(socket)

        return writer, headers


_default_client = None


def _get_client():
    global _default_client
    if _default_client is None:
        _default_client = Client()
    return _default_client


def request(method, url, headers={}):
    client = _get_client()
    return client.request(method, url, headers)
