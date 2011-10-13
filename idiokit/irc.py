import re
import util
import threado
import sockets

class IRCError(Exception):
    pass

class IRCParser(object):
    def __init__(self):
        self.line_buffer = util.LineBuffer()

    def feed(self, data=""):
        if "\x00" in data:
            raise IRCError("NUL not allowed in messages")

        for line in self.line_buffer.feed(data):
            if len(line) > 510:
                raise IRCError("too long message (over 512 bytes)")
            yield self.process_line(line)

    def process_line(self, line):
        if line.startswith(":"):
            bites = line.split(" ", 2)
            prefix = bites.pop(0)[1:]
        else:
            bites = line.split(" ", 1)
            prefix = None

        if len(bites) != 2:
            raise IRCError("invalid message")

        command, trailing = bites
        params = list()

        while trailing:
            if trailing.startswith(":"):
                params.append(trailing[1:])
                break

            bites = trailing.split(" ", 1)
            params.append(bites.pop(0))
            trailing = "".join(bites)

        return prefix, command, params

def format_message(command, *params):
    message = [command] + list(params[:-1]) + [":" + "".join(params[-1:])]
    return " ".join(message) + "\r\n"

ERROR_REX = re.compile("^(4|5)\d\d$")

class NickAlreadyInUse(IRCError):
    pass

def mutations(*nicks):
    for nick in nicks:
        yield nick

    for suffix in ["_", "-", "~", "^"]:
        for nick in nicks:
            yield nick[:9-len(suffix)] + suffix

    for nick in nicks:
        for i in range(1000000):
            suffix = str(i)
            yield nick[:9-len(suffix)] + suffix

class IRC(threado.GeneratorStream):
    def __init__(self, server, port,
                 ssl=False, ssl_verify_cert=True, ssl_ca_certs=None):
        threado.GeneratorStream.__init__(self)

        self.server = server
        self.port = port

        self.ssl = ssl
        self.ssl_verify_cert = ssl_verify_cert
        self.ssl_ca_certs = ssl_ca_certs

        self.parser = None
        self.socket = None

    @threado.stream
    def connect(inner, self, nick, password=None):
        self.parser = IRCParser()
        self.socket = sockets.Socket()

        yield inner.sub(self.socket.connect((self.server, self.port)))

        if self.ssl:
            yield inner.sub(self.socket.ssl(verify_cert=self.ssl_verify_cert,
                                            ca_certs=self.ssl_ca_certs))

        nicks = mutations(nick)
        if password is not None:
            self.socket.send(format_message("PASS", password))
        self.socket.send(format_message("NICK", nicks.next()))
        self.socket.send(format_message("USER", nick, nick, "-", nick))

        while True:
            source, data = yield threado.any(inner, self.socket)
            if inner is source:
                continue

            for prefix, command, params in self.parser.feed(data):
                if command == "PING":
                    self.socket.send(format_message("PONG", *params))
                    continue

                if command == "001":
                    self.start()
                    inner.finish(nick)

                if command == "433":
                    for nick in nicks:
                        self.socket.send(format_message("NICK", nick))
                        break
                    else:
                        raise NickAlreadyInUse("".join(params[-1:]))
                    continue

                    if ERROR_REX.match(command):
                        raise IRCError("".join(params[-1:]))

    def nick(self, nick):
        self.send("NICK", nick)

    def quit(self, message=None):
        if message is None:
            self.send("QUIT")
        else:
            self.send("QUIT", message)

    def join(self, channel, key=None):
        if key is None:
            self.send("JOIN", channel)
        else:
            self.send("JOIN", channel, key)

    def run(self):
        for prefix, command, params in self.parser.feed():
            if command == "PING":
                self.send("PONG", *params)
            self.inner.send(prefix, command, params)

        while True:
            source, data = yield threado.any(self.inner, self.socket)

            if self.inner is source:
                self.socket.send(format_message(*data))
                continue

            for prefix, command, params in self.parser.feed(data):
                if command == "PING":
                    self.send("PONG", *params)
                self.inner.send(prefix, command, params)