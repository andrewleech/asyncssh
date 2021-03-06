# Copyright (c) 2013-2015 by Ron Frederick <ronf@timeheart.net>.
# All rights reserved.
#
# This program and the accompanying materials are made available under
# the terms of the Eclipse Public License v1.0 which accompanies this
# distribution and is available at:
#
#     http://www.eclipse.org/legal/epl-v10.html
#
# Contributors:
#     Ron Frederick - initial implementation, API, and documentation

"""SSH stream handlers"""

import asyncio

from .constants import EXTENDED_DATA_STDERR
from .misc import BreakReceived, SignalReceived, TerminalSizeChanged
from .session import SSHClientSession, SSHServerSession, SSHTCPSession


class SSHReader:
    """SSH read stream handler"""

    def __init__(self, session, chan, datatype=None):
        self._session = session
        self._chan = chan
        self._datatype = datatype

    @property
    def channel(self):
        """The SSH channel associated with this stream"""

        return self._chan

    def get_extra_info(self, name, default=None):
        """Return additional information about this stream

           This method returns extra information about the channel
           associated with this stream. See :meth:`get_extra_info()
           <SSHClientChannel.get_extra_info>` on :class:`SSHClientChannel`
           for additional information.

        """

        return self._chan.get_extra_info(name, default)

    def read(self, n=-1):
        """Read data from the stream

           This method is a coroutine which reads up to ``n`` bytes
           or characters from the stream. If ``n`` is not provided or
           set to ``-1``, it reads until EOF or until a signal is
           received on the stream.

           If EOF was received and the receive buffer is empty, an
           empty ``bytes`` or ``string`` object is returned.

           .. note:: Unlike traditional ``asyncio`` stream readers,
                     the data will be delivered as either bytes or
                     a string depending on whether an encoding was
                     specified when the underlying channel was opened.

        """

        return self._session.read(n, self._datatype, exact=False)

    def readline(self):
        """Read one line from the stream

           This method is a coroutine which reads one line, ending in
           ``'\\n'``.

           If EOF was received before ``'\\n'`` was found, the partial
           line is returned. If EOF was received and the receive buffer
           is empty, an empty ``bytes`` or ``string`` object is returned.

        """

        return self._session.readline(self._datatype)

    def readexactly(self, n):
        """Read an exact amount of data from the stream

           This method is a coroutine which reads exactly n bytes or
           characters from the stream.

           If EOF is received before ``n`` bytes are read, an
           :exc:`IncompleteReadError <asyncio.IncompleteReadError>` is
           raised and its ``partial`` attribute contains the partially
           read data.

        """

        return self._session.read(n, self._datatype, exact=True)

    def at_eof(self):
        """Return whether the stream is at EOF

           This method returns ``True`` when EOF has been received and
           all data in the stream has been read.

        """

        return self._session.at_eof(self._datatype)


class SSHWriter:
    """SSH write stream handler"""

    def __init__(self, session, chan, datatype=None):
        self._session = session
        self._chan = chan
        self._datatype = datatype

    @property
    def channel(self):
        """The SSH channel associated with this stream"""

        return self._chan

    def get_extra_info(self, name, default=None):
        """Return additional information about this stream

           This method returns extra information about the channel
           associated with this stream. See :meth:`get_extra_info()
           <SSHClientChannel.get_extra_info>` on :class:`SSHClientChannel`
           for additional information.

        """

        return self._chan.get_extra_info(name, default)

    def can_write_eof(self):
        """Return whether the stream supports :meth:`write_eof`"""

        return self._chan.can_write_eof()

    def close(self):
        """Close the channel

           .. note:: After this is called, no data can be read or written
                     from any of the streams associated with this channel.

        """

        return self._chan.close()

    @asyncio.coroutine
    def drain(self):
        """Wait until the write buffer on the channel is flushed

           This method is a coroutine which blocks the caller if the
           stream is currently paused for writing, returning when
           enough data has been sent on the channel to allow writing
           to resume. This can be used to avoid buffering an excessive
           amount of data in the channel's send buffer.

        """

        return (yield from self._session.drain())

    def write(self, data):
        """Write data to the stream

           This method writes bytes or characters to the stream.

           .. note:: Unlike traditional ``asyncio`` stream writers,
                     the data must be supplied as either bytes or
                     a string depending on whether an encoding was
                     specified when the underlying channel was opened.

        """

        return self._chan.write(data, self._datatype)

    def writelines(self, list_of_data):
        """Write a collection of data to the stream"""

        return self._chan.writelines(list_of_data, self._datatype)

    def write_eof(self):
        """Write EOF on the channel

           This method sends an end-of-file indication on the channel,
           after which no more data can be written.

           .. note:: On an :class:`SSHServerChannel` where multiple
                     output streams are created, writing EOF on one
                     stream signals EOF for all of them, since it
                     applies to the channel as a whole.

        """

        return self._chan.write_eof()


class SSHStreamSession:
    """SSH stream session handler"""

    def __init__(self):
        self._chan = None
        self._loop = None
        self._limit = None
        self._exception = None
        self._eof_received = False
        self._connection_lost = False
        self._recv_buf = {None: []}
        self._recv_buf_len = 0
        self._read_waiter = {None: None}
        self._write_paused = False
        self._drain_waiters = []

    @asyncio.coroutine
    def _block_read(self, datatype):
        """Wait for more data to arrive on the stream"""

        if self._read_waiter[datatype]:
            raise RuntimeError('read called while another coroutine is '
                               'already waiting to read')

        waiter = asyncio.Future(loop=self._loop)
        self._read_waiter[datatype] = waiter
        yield from waiter

    def _unblock_read(self, datatype):
        """Signal that more data has arrived on the stream"""

        waiter = self._read_waiter[datatype]
        if waiter:
            if not waiter.cancelled():
                waiter.set_result(None)
            self._read_waiter[datatype] = None

    def _unblock_drain(self):
        """Signal that more data can be written on the stream"""

        for waiter in self._drain_waiters:
            if not waiter.cancelled():
                waiter.set_result(None)

        self._drain_waiters = []

    def connection_made(self, chan):
        """Handle a newly opened channel"""

        self._chan = chan
        self._loop = chan.get_loop()
        self._limit = self._chan.get_recv_window()

        for datatype in chan.get_read_datatypes():
            self._recv_buf[datatype] = []
            self._read_waiter[datatype] = None

    def connection_lost(self, exc):
        """Handle an incoming channel close"""

        self._connection_lost = True
        self._exception = exc

        if not self._eof_received:
            if exc:
                for datatype in self._read_waiter.keys():
                    self._recv_buf[datatype].append(exc)

            self.eof_received()

        if self._write_paused:
            self._unblock_drain()

    def data_received(self, data, datatype):
        """Handle incoming data on the channel"""

        self._recv_buf[datatype].append(data)
        self._recv_buf_len += len(data)
        self._unblock_read(datatype)

        if self._recv_buf_len >= self._limit:
            self._chan.pause_reading()

    def eof_received(self):
        """Handle an incoming end of file on the channel"""

        self._eof_received = True
        for datatype in self._read_waiter.keys():
            self._unblock_read(datatype)

        return True

    def at_eof(self, datatype):
        """Return whether end of file has been received on the channel"""

        return self._eof_received and not self._recv_buf[datatype]

    def pause_writing(self):
        """Handle a request to pause writing on the channel"""

        self._write_paused = True

    def resume_writing(self):
        """Handle a request to resume writing on the channel"""

        self._write_paused = False
        self._unblock_drain()

    @asyncio.coroutine
    def read(self, n, datatype, exact):
        """Read data from the channel"""

        recv_buf = self._recv_buf[datatype]
        buf = '' if self._chan.get_encoding() else b''
        data = []

        while True:
            while recv_buf and n != 0:
                if isinstance(recv_buf[0], Exception):
                    if data:
                        break
                    else:
                        raise recv_buf.pop(0)

                l = len(recv_buf[0])
                if n > 0 and l > n:
                    data.append(recv_buf[0][:n])
                    recv_buf[0] = recv_buf[0][n:]
                    self._recv_buf_len -= n
                    n = 0
                    break

                data.append(recv_buf.pop(0))
                self._recv_buf_len -= l
                n -= l

            if self._recv_buf_len < self._limit:
                self._chan.resume_reading()

            if n == 0 or (n > 0 and data and not exact) or self._eof_received:
                break

            yield from self._block_read(datatype)

        buf = buf.join(data)
        if n > 0 and exact:
            raise asyncio.IncompleteReadError(buf, len(buf) + n)

        return buf

    @asyncio.coroutine
    def readline(self, datatype):
        """Read a line from the channel"""

        recv_buf = self._recv_buf[datatype]
        buf, sep = ('', '\n') if self._chan.get_encoding() else (b'', b'\n')
        data = []

        while True:
            while recv_buf:
                if isinstance(recv_buf[0], Exception):
                    if data:
                        return buf.join(data)
                    else:
                        raise recv_buf.pop(0)

                idx = recv_buf[0].find(sep) + 1
                if idx > 0:
                    data.append(recv_buf[0][:idx])
                    recv_buf[0] = recv_buf[0][idx:]
                    self._recv_buf_len -= idx

                    if self._recv_buf_len < self._limit:
                        self._chan.resume_reading()

                    return buf.join(data)

                l = len(recv_buf[0])
                data.append(recv_buf.pop(0))
                self._recv_buf_len -= l

            if self._recv_buf_len < self._limit:
                self._chan.resume_reading()

            if self._eof_received:
                return buf.join(data)

            yield from self._block_read(datatype)

    @asyncio.coroutine
    def drain(self):
        """Wait for data written to the channel to drain"""

        if self._write_paused and not self._connection_lost:
            waiter = asyncio.Future(loop=self._loop)
            self._drain_waiters.append(waiter)
            yield from waiter

        if self._connection_lost:
            exc = self._exception

            if not exc and self._write_paused:
                exc = BrokenPipeError()

            if exc:
                raise exc   # pylint: disable=raising-bad-type


class SSHClientStreamSession(SSHStreamSession, SSHClientSession):
    """SSH client stream session handler"""


class SSHServerStreamSession(SSHStreamSession, SSHServerSession):
    """SSH server stream session handler"""

    def __init__(self, allow_pty, session_factory, sftp_factory):
        super().__init__()

        self._allow_pty = allow_pty
        self._session_factory = session_factory
        self._sftp_factory = sftp_factory

    def pty_requested(self, term_type, term_size, term_modes):
        """Return whether a pseudo-tty can be requested"""

        return self._allow_pty

    def shell_requested(self):
        """Return whether a shell can be requested"""

        return bool(self._session_factory)

    def exec_requested(self, command):
        """Return whether execution of a command can be requested"""

        return bool(self._session_factory)

    def subsystem_requested(self, subsystem):
        """Return whether starting a subsystem can be requested"""

        if subsystem == 'sftp':
            return bool(self._sftp_factory)
        else:
            return bool(self._session_factory)

    def session_started(self):
        """Start a session for this newly opened server channel"""

        if self._chan.get_subsystem() == 'sftp':
            self._chan.start_sftp_server(self._sftp_factory)
        else:
            handler = self._session_factory(SSHReader(self, self._chan),
                                            SSHWriter(self, self._chan),
                                            SSHWriter(self, self._chan,
                                                      EXTENDED_DATA_STDERR))

            if asyncio.iscoroutine(handler):
                asyncio.async(handler)

    def break_received(self, msec):
        """Handle an incoming break on the channel"""

        self._recv_buf[None].append(BreakReceived(msec))
        self._unblock_read(None)
        return True

    def signal_received(self, signal):
        """Handle an incoming signal on the channel"""

        self._recv_buf[None].append(SignalReceived(signal))
        self._unblock_read(None)

    def terminal_size_changed(self, *args):
        """Handle an incoming terminal size change on the channel"""

        self._recv_buf[None].append(TerminalSizeChanged(*args))
        self._unblock_read(None)


class SSHTCPStreamSession(SSHStreamSession, SSHTCPSession):
    """SSH TCP stream session handler"""

    def __init__(self, handler_factory=None):
        super().__init__()

        self._handler_factory = handler_factory

    def session_started(self):
        """Start a session for this newly opened TCP channel"""

        if self._handler_factory:
            handler = self._handler_factory(SSHReader(self, self._chan),
                                            SSHWriter(self, self._chan))

            if asyncio.iscoroutine(handler):
                asyncio.async(handler)
