"""Connections of the emulated display to other programs: a pty (a serial port), a TCP socket, a unix socket."""

from __future__ import annotations

import errno
import fcntl
import os
import queue
import select
import socket
import threading
import tty


class Link:
    """Base: ``attach(device)`` then ``start()``; ``send(data)`` is the device's output (never blocks)."""

    name = "link"

    def attach(self, device):
        self.device = device

    def send(self, data):
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def stop(self):
        pass


class PtyLink(Link):
    """A pseudo terminal; the program under test opens the slave side like ``/dev/ttyS1``."""

    def __init__(self, link_path=None):
        self.link_path = link_path
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        # the slave stays open here: the program may open and close its side and the master never sees a hangup
        fcntl.fcntl(self.master, fcntl.F_SETFL, fcntl.fcntl(self.master, fcntl.F_GETFL) | os.O_NONBLOCK)
        self.path = os.ttyname(self.slave)
        self.name = "pty " + self.path
        self._out = queue.Queue()
        self._running = False
        self._made_link = False
        if link_path:
            if os.path.lexists(link_path):
                if not os.path.islink(link_path):
                    raise OSError("%s exists and is not a symlink" % link_path)
                os.unlink(link_path)
            os.symlink(self.path, link_path)
            self._made_link = True

    def send(self, data):
        self._out.put(bytes(data))

    def start(self):
        self._running = True
        threading.Thread(target=self._read_loop, daemon=True, name="pty-rx").start()
        threading.Thread(target=self._write_loop, daemon=True, name="pty-tx").start()

    def _read_loop(self):
        while self._running:
            try:
                r, _, _ = select.select([self.master], [], [], 0.2)
                if not r:
                    continue
                data = os.read(self.master, 65536)
            except BlockingIOError:
                continue
            except OSError:
                break
            if data:
                self.device.feed(data)

    def _write_loop(self):
        while self._running:
            try:
                data = self._out.get(timeout=0.2)
            except queue.Empty:
                continue
            while data and self._running:
                try:
                    n = os.write(self.master, data)
                    data = data[n:]
                except BlockingIOError:
                    select.select([], [self.master], [], 0.05)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        return
                    break

    def stop(self):
        self._running = False
        if self._made_link:
            try:
                os.unlink(self.link_path)
            except OSError:
                pass
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class SocketLink(Link):
    """A listening TCP (``host:port``) or unix (path) socket; every client sees the display's output."""

    def __init__(self, address):
        if isinstance(address, tuple):
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(address)
            self.name = "tcp %s:%d" % self.sock.getsockname()[:2]
            self.port = self.sock.getsockname()[1]
        else:
            if os.path.lexists(address):
                os.unlink(address)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(address)
            self.name = "unix " + address
        self.address = address
        self.sock.listen(4)
        self.clients = []
        self._lock = threading.Lock()
        self._running = False

    def send(self, data):
        with self._lock:
            for c in list(self.clients):
                try:
                    c.sendall(data)
                except OSError:
                    self.clients.remove(c)

    def start(self):
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True, name="sock-accept").start()

    def _accept_loop(self):
        self.sock.settimeout(0.2)
        while self._running:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) if conn.family == socket.AF_INET else None
            with self._lock:
                self.clients.append(conn)
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True, name="sock-rx").start()

    def _client_loop(self, conn):
        while self._running:
            try:
                data = conn.recv(65536)
            except OSError:
                break
            if not data:
                break
            self.device.feed(data)
        with self._lock:
            if conn in self.clients:
                self.clients.remove(conn)
        try:
            conn.close()
        except OSError:
            pass

    def stop(self):
        self._running = False
        with self._lock:
            for c in self.clients:
                try:
                    c.close()
                except OSError:
                    pass
        try:
            self.sock.close()
        except OSError:
            pass
        if not isinstance(self.address, tuple):
            try:
                os.unlink(self.address)
            except OSError:
                pass
