"""Library to mux several communication on a single interface

MCOM sends frames of at most 265 bytes.
Each frame is acknownledge by the other side using channel 0.
Acknowledge can validate only part of the packet, this is called a "partial ack".
In case of partial ack:
- The sender discards only the amount of data which was accepted by the receiver.
- The sender waits for a 'resume' frame on channel 0.

ack frame data fields:
- byte 0 = 0x00 (identifier for ACK frame)
- Bytes 1 and 2:
    - 6 lsb: channel to ACK
    - 10 msb: buf_level (10 bit signed int)
        - if positive: max additional bytes that can be accepted by this channel (depends on current state of its buffer)
        - if negative, this is a partial ack, only -buf_level bytes have been accepted

Note: the channel to ACK could be implicit as the other side always sends them "in order".

resume frame data fields:
- byte 0 = 0x01 (identifier fro RESUME frame)
- Bytes 1 and 2:
    - 6 lsb: channel number used to send the packet
    - 10 msb: max bytes that can be accepted by this channel (depends on current state of its buffer)

"""

__version__ = '0.0.1'
__title__ = 'mcom'
__description__ = 'Library to mux several communication on a single interface'
__long_description__ = """
Library to mux several communication on a single interface
"""
__uri__ = 'https://github.com/sebastien-riou/mcom'
__doc__ = __description__ + ' <' + __uri__ + '>'
__author__ = 'Sebastien Riou'
# For all support requests, please open a new issue on GitHub
__email__ = 'matic@nimp.co.uk'
__license__ = 'Apache 2.0'
__copyright__ = ''


import io
from queue import SimpleQueue, Empty
import select
import threading
import mcom.mqueue
from mcom.mqueue import MQueuePool, MQueue

class MCom(object):
    """ MCom main class

    Generic MCom implementation. It interface to actual hardware via a
    "communication driver" which shall implement few functions.
    See :class:`SocketComDriver` and :class:`StreamComDriver` for example.

    Args:
        is_host (bool): Set to ``True`` to be host, ``False`` to be device
        com_driver (object): A MCom communication driver

    """

    @property
    def com(self):
        """object: Communication hardware driver"""
        return self._com

    @property
    def is_host(self):
        """bool: `True` if host, `False` if device"""
        return self._is_host

    @property
    def spy_frame_tx(self):
        """functions called to spy on each tx frame, without padding"""
        return self._spy_frame_tx

    @spy_frame_tx.setter
    def spy_frame_tx(self, value):
        self._spy_frame_tx = value

    @property
    def spy_frame_rx(self):
        """functions called to spy on each rx frame, without padding"""
        return self._spy_frame_rx

    @spy_frame_rx.setter
    def spy_frame_rx(self, value):
        self._spy_frame_rx = value

    def __init__(self,*,is_host,com_driver):
        self._com = com_driver
        self._is_host = is_host
        self._spy_frame_tx = None
        self._spy_frame_rx = None
        self._channels = {}
        self._last_tx_chan = 1
        self._last_resume_chan = 1
        self._tx_pool = mcom.mqueue.MQueuePool()
        self._rx_pool = mcom.mqueue.MQueuePool()
        self.open_channel(name="ctrl",num=0,rx_buf_size=4, tx_buf_size=4, description="link control channel"  )
        self._rx_thread = threading.Thread(target=self.rx_worker, args=[])
        self._rx_thread.start()
        self._tx_thread = threading.Thread(target=self.tx_worker, args=[])
        self._tx_thread.start()

    def open_channel(self,*,name: str,num: int,rx_buf_size: int,tx_buf_size:int,description: str=""):
        assert(num not in self._channels)
        args = locals()
        del args["self"]
        chan = Channel(**args)
        self._channels[chan.num] = chan
        chan.tx_queue_pool = self._tx_pool
        chan.rx_queue_pool = self._rx_pool

    def close_channel(self,num: int):
        self._channels.pop(num)

    class Frame(object):

        @staticmethod
        def DATA_UNIT_SIZE():
            """int: size of data units excanched with hardware driver"""
            return 4

        @staticmethod
        def SMALL_FRAME_HEADER_SIZE():
            """int: size of header within data unit of small frame"""
            return 1

        @staticmethod
        def LARGE_FRAME_HEADER_SIZE():
            """int: size of header within first data unit of large frame"""
            return 2

        @staticmethod
        def LARGE_FRAME_FIRST_DATA_UNIT_SIZE():
            """int: size of data within first data unit of large frame"""
            return 2

        @staticmethod
        def LARGE_FRAME_MIN_DATA_SIZE():
            """int: minimum data size sent in one large frame"""
            return 4

        @staticmethod
        def MAX_DATA_SIZE():
            """int: max size of data sent in one frame"""
            return 255 + MCom.Frame.LARGE_FRAME_MIN_DATA_SIZE()

        @staticmethod
        def FAST_DATA_SIZE():
            """int: size of data sent in one frame to achieve most efficient transfers"""
            max_extra_data_bytes = Frame.MAX_DATA_SIZE() - Frame.LARGE_FRAME_FIRST_DATA_UNIT_SIZE()
            max_full_data_units = max_extra_data_bytes // Frame.DATA_UNIT_SIZE()
            return max_full_data_units * Frame.DATA_UNIT_SIZE() + Frame.LARGE_FRAME_FIRST_DATA_UNIT_SIZE()

        def __init__(self,*,chan: int, data):
            assert(chan < 64)
            self._chan = chan
            self._bytes = bytearray()
            self._data_size = len(data)
            assert(self._data_size < self.MAX_DATA_SIZE())
            large_frame_size_field = self._data_size - self.LARGE_FRAME_MIN_DATA_SIZE()
            if large_frame_size_field >= 0:
                self._small_frame_size = 0
            else:
                self._small_frame_size = self._data_size
            byte0 = (self._small_frame_size << 6) | chan
            self._bytes += byte0.to_bytes(1,byteorder='little')
            if large_frame_size_field >= 0:
                self._bytes += large_frame_size_field.to_bytes(1,byteorder='little')
                self._bytes += data
            self._padlen = len(self._bytes) % MCom.Frame.DATA_UNIT_SIZE()
            if self._padlen:
                self._padlen = MCom.Frame.DATA_UNIT_SIZE() - self._padlen
                self._bytes += bytes(self._padlen)
            self._ndu = len(self._bytes) // MCom.Frame.DATA_UNIT_SIZE()
            assert(0 == (len(self._bytes) % MCom.Frame.DATA_UNIT_SIZE()))

        @staticmethod
        def from_bytes(frame):
            byte0 = int.from_bytes(frame[0:1],byteorder='little')
            small_frame_size = byte0 >> 6
            chan = byte0 & 0x3F
            is_large = 0 == small_frame_size
            if is_large:
                #data_size = len(frame) - Frame.LARGE_FRAME_HEADER_SIZE
                data = frame[MCom.Frame.LARGE_FRAME_HEADER_SIZE():]
            else:
                #data_size = len(frame) - Frame.SMALL_FRAME_HEADER_SIZE
                data = frame[MCom.Frame.SMALL_FRAME_HEADER_SIZE():]
            if 0 == chan:
                assert(not is_large)
                if data[0] == MCom.AckFrame.INS:
                    return MCom.AckFrame.from_bytes(frame)
                if data[0] == MCom.ResumeFrame.INS:
                    return MCom.ResumeFrame.from_bytes(frame)
                raise Exception("unknown frame type received on channel 0:",frame)
            print("data=",data)
            fo = MCom.Frame(chan=chan,data=data)
            print("fo.bytes",fo.bytes)
            print("fo.data",fo.data)
            return fo

        @property
        def bytes(self):
            return self._bytes

        @property
        def data_size(self):
            return self._data_size

        @property
        def size(self):
            return len(self._bytes)

        @property
        def size_data_unit(self):
            return self._ndu

        @property
        def is_large(self):
            return 0 == self._small_frame_size

        @property
        def data(self):
            if self.is_large:
                return self._bytes[self.LARGE_FRAME_HEADER_SIZE():]
            return self._bytes[self.SMALL_FRAME_HEADER_SIZE():]

        @property
        def channel(self):
            return self._chan

    class AckFrame(Frame):
        INS = 0x00
        """byte: identifier of ACK frames"""

        @property
        def ackchan(self):
            return self._ackchan

        @property
        def buf_level(self):
            return self._buf_level

        def __init__(self,*,ackchan: int, buf_level: int):
            self._ackchan = ackchan
            self._buf_level = buf_level
            buf_level = (buf_level<<6) | ackchan
            data = buf_level.to_bytes(2,byteorder='little',signed=True)
            super().__init__(chan=0,data=data)

        @classmethod
        def from_bytes(cls,frame):
            assert(len(frame)==MCom.Frame.DATA_UNIT_SIZE())
            assert(frame[0]==0x80)
            assert(frame[1]==cls.INS)
            buf_level = int.from_bytes(frame[2:],byteorder='little',signed=True)
            ackchan = buf_level & 0x3F
            buf_level = buf_level >> 6
            return cls(ackchan=ackchan,buf_level=buf_level)

    class ResumeFrame(AckFrame):
        INS = 0x01
        """byte: identifier of RESUME frames"""

    def rx_worker(self):
        while(True):
            rx_frame = self.__frame_rx()
            if 0 == rx_frame.channel:
                ackchannum = rx_frame.ackchan
                chan = self._channels[ackchannum]
                if rx_frame.INS == MCom.AckFrame.INS:
                    chan._ack_tx(rx_frame.buf_level)
                elif rx_frame.INS == MCom.ResumeFrame.INS:
                    chan._resume_tx(rx_frame.buf_level)
                else:
                    raise Exception("Unexpected frame type on channel 0:",rx_frame.INS)
            else:
                chan = self._channels[rx_frame.channel]
                buf_level = chan._add_to_rx_buf(rx_frame.data)
                ack_frame = MCom.AckFrame(ackchan=chan.num,buf_level=buf_level)
                self._channels[0].tx(ack_frame.bytes)

    def tx_worker(self):
        #loop until get_queue returns "None" so this loop can be exited using self._tx_pool.put(None)
        for q in iter(self._tx_pool.get_queue, None):
            chan = q.id
            print("tx_worker: chan.num=%d"%chan.num)
            if 0 == chan.num:
                #here the data is fully formated frames
                framebytes = chan._get_from_tx_buf()
                self.__frame_tx(framebytes)
            else:
                if chan.rx_stalled:
                    free_size = chan._rx_free_size()
                    if free_size > 0:
                        resume_frame = MCom.ResumeFrame(ackchan=chan.num,buf_level=free_size)
                        self.__frame_tx(resume_frame)
                if chan._has_tx():
                    frame = MCom.Frame(chan=chan.num,data=chan._get_from_tx_buf())
                    self.__frame_tx(frame)

    def tx(self,*,channel: int, data: bytes):
        """Transmit

        Args:
            channel: :class:`Channel` number.
            data: bytes to send
        """
        self._channels[channel].tx(data)

    def rx(self,*,channel: int=None, length: int=1, block=True):
        """Receive at most `length` bytes on `channel`
        Returns:
             Data bytes, at most `length`
             if channel is None, second return value is the channel number
        """
        assert(channel != 0)
        def core(channel,length,block):
            if channel is None:
                #rx from any channel, blocking or not
                q = self._rx_pool.get_queue(block=block)
                assert(q.id.num != 0)
                channel = q.id.num
                return self._channels[channel].rx(length), channel
            elif block:
                #blocking rx on specific channel
                match = False
                while not match:
                    q = self._rx_pool.get_queue(block=block)
                    assert(q.id.num != 0)
                    match = channel == q.id.num
            return self._channels[channel].rx(length), channel
        retchan = channel is None
        if block:
            out = bytearray()
            while len(out) < length:
                dat, channel = core(channel,length,block)
                out += dat
        else:
            out, channel = core(channel,length,block)
        if retchan:
            return out,channel
        return out

    def __frame_tx(self,frame):
        """send a complete frame"""

        try:
            dat = frame.bytes
        except:
            dat = frame

        if self._spy_frame_tx is not None:
            self._spy_frame_tx(dat)

        self._com.tx(dat)
        #print("__frame_tx done",flush=True)

    def __frame_rx(self):
        """receive a complete frame"""
        frame = self.__rx()
        length = frame[0] >> 6
        if 0==length:
            length = frame[1] + MCom.Frame.LARGE_FRAME_MIN_DATA_SIZE()
            remaining = length - MCom.Frame.LARGE_FRAME_FIRST_DATA_UNIT_SIZE()
            ndu = Utils.ceildiv(remaining, MCom.Frame.DATA_UNIT_SIZE())
            frame += self.__rx(ndu)

        if self._spy_frame_rx is not None:
            self._spy_frame_rx(frame)

        return MCom.Frame.from_bytes(frame)

    def __rx(self,ndu=1):
        """receive data units (blocking)"""
        dat = self._com.rx(ndu)
        assert(0 == (len(dat) % MCom.Frame.DATA_UNIT_SIZE()))
        return dat

    def __tx(self, data: bytes):
        """send a data unit (blocking)"""
        assert(len(dat) == MCom.Frame.DATA_UNIT_SIZE())
        self._com.tx(data)

class Channel(object):
    """MCOM channel

    """

    class Buf(object):
        def __init__(self,size: int):
            self.size = size
            self.cnt = 0
            self.buf = MQueue(size)
            self.tx_buf = bytearray()

        def get(self,length: int=1):
            base = len(self.tx_buf)
            cnt = length
            try:
                while cnt > 0:
                    dat = self.buf.get_nowait()
                    self.tx_buf.append(dat)
                    self.cnt -= 1
                    assert(self.cnt >= 0)
                    cnt -= 1
            except Empty as e:
                pass
            out = self.tx_buf[base:base+length]
            return out

        def full_ack(self):
            self.tx_buf = bytearray()

        def partial_ack(self,length: int):
            self.tx_buf = self.tx_buf[length:]

        def put(self,dat: bytearray):
            cnt = 0
            try:
                for i in dat:
                    self.buf.put_nowait(i)
                    self.cnt += 1
                    assert(self.cnt <= self.size)
                    cnt += 1
            except queue.Full as e:
                pass
            return cnt

        def free_size(self):
            return self.size - self.cnt

        def data_size(self):
            return self.cnt

    @property
    def rx_queue_pool(self):
        return self.rx_buf.buf.pool

    @rx_queue_pool.setter
    def rx_queue_pool(self, val):
        self.rx_buf.buf.pool = val
        self.rx_buf.buf.id = self

    @property
    def tx_queue_pool(self):
        return self.tx_buf.buf.pool

    @tx_queue_pool.setter
    def tx_queue_pool(self, val):
        self.tx_buf.buf.pool = val
        self.tx_buf.buf.id = self

    def __init__(self,*,name: str,num: int,rx_buf_size: int,tx_buf_size:int,description: str=""):
        self.name = name
        self.num = num
        self.description = description
        self.rx_buf = Channel.Buf(rx_buf_size)
        self.tx_buf = Channel.Buf(tx_buf_size)
        self.tx_max_bytes = MCom.Frame.MAX_DATA_SIZE()
        self.rx_stalled = False

    def rx(self,length: int=1):
        return self.rx_buf.get(length)

    def _add_to_rx_buf(self,dat: bytearray):
        cnt = self.rx_buf.put(dat)
        all = len(dat)
        if cnt < all:
            self.rx_stalled = True
            return -cnt # return the number of bytes accepted, inversed
        return self.rx_buf.free_size() # return remaining number of bytes that can be accepted

    def _rx_free_size(self):
        return self.rx_buf.free_size()

    def _tx_dat_size(self):
        if 0 == self.tx_buf.data_size():
            return 0
        dat_size = self.tx_buf.data_size()
        return min(dat_size,self.tx_max_bytes)

    def _has_tx(self):
        return self.tx_buf.data_size() > 0

    def tx(self,dat):
        return self.tx_buf.put(dat)

    def _get_from_tx_buf(self,*,length = None):
        if length is None:
            length = self.tx_max_bytes
        self.tx_max_bytes = None # we gave some data, now wait for a acknowledge
        return self.tx_buf.get(length)

    def _ack_tx(self,length: int):
        if length > 0:
            self.tx_buf.full_ack()
            self.tx_max_bytes = length
        else:
            self.tx_buf.partial_ack(-length)

    def _resume_tx(self,length: int):
        self.tx_max_bytes = length
        self.tx_buf.buf.put_empty() #notify tx_worker that it shall send ResumeFrame

class SocketComDriver(object):
    """Parameterized model for a communication peripheral and low level rx/tx functions

    Args:
        sock (socket): `socket` object used for communication

    """
    def __init__(self,sock):
        self._sock = sock
        self._sock.setblocking(True)

    def write(self,data):
        self._sock.send(data)

    def read(self,length):
        return self._sock.recv(length)

    def _set_blocking(self):
        self._sock.settimeout(None)

    def _set_non_blocking(self):
        self._sock.settimeout(0)

    @property
    def sock(self):
        """socket: `socket` object used for communication"""
        return self._sock

    def tx(self,data):
        """Transmit data

        Args:
            data (bytes): bytes to transmit, size shall be a multiple of data units
        """
        assert(0 == (len(data) % MCom.Frame.DATA_UNIT_SIZE()))
        self.write(data)

    def rx(self,ndu=1):
        """Receive data

        Args:
            length (int): length to receivein data units

        Returns:
            bytes: received data, padded with zeroes if necessary to be compatible with :func:`sfr_granularity`
        """
        data = bytearray()
        remaining = ndu * MCom.Frame.DATA_UNIT_SIZE()
        #print("length=",length,flush=True)
        #print("remaining=",remaining,flush=True)
        while(remaining):
            #print("remaining=",remaining)
            #print("receive: ",end="")
            dat = self.read(remaining)
            #print("received: ",Utils.hexstr(dat))
            if 0==len(dat):
                print(self._sock)
                #print(self._stream.timeout)
                raise Exception("Connection broken")
            data += dat
            remaining -= len(dat)
        return data

    def has_rx_dat(self):
        socket_list = [self._sock]
        # Get the list sockets which are readable
        read_sockets, write_sockets, error_sockets = select.select(socket_list , [], [])
        return len(read_sockets) > 0

class StreamComDriver(object):
    """Parameterized model for a communication peripheral and low level rx/tx functions"""
    def __init__(self,stream):
        self._stream = stream
        self._cache = bytearray()

    @property
    def stream(self):
        """stream: `stream` object used for communication"""
        return self._stream

    def tx(self,data):
        """Transmit data

        Args:
            data (bytes): bytes to transmit, shall be compatible with :func:`sfr_granularity` and :func:`granularity`
        """
        #print("tx ",data,flush=True)
        assert(0 == (len(data) % Frame.DATA_UNIT_SIZE))
        self._stream.write(data)

    def rx(self,ndu=1):
        """Receive data

        Args:
            length (int): length to receive, shall be compatible with :func:`granularity` and smaller or equal to :func:`bufferlen`

        Returns:
            bytes: received data, padded with zeroes if necessary to be compatible with :func:`sfr_granularity`
        """
        #print("rx %d "%length,end="",flush=True)
        data = bytearray()
        remaining = ndu*Frame.DATA_UNIT_SIZE
        #print("length=",length,flush=True)
        #print("remaining=",remaining,flush=True)
        cachelen = len(self._cache)
        if remaining and cachelen > 0:
            toread = min(remining,cachelen)
            dat += self._cache[0:toread]
            self._cache = self._cache[toread:]
            remaining -= toread
        while(remaining):
            #print("remaining=",remaining)
            #print("receive: ",end="")
            dat = self._stream.read(remaining)
            #print("received: ",Utils.hexstr(dat))
            if 0==len(dat):
                print(self._stream)
                #print(self._stream.timeout)
                raise Exception("Connection broken")
            data += dat
            remaining -= len(dat)

        return data

    def has_rx_dat(self):
        if len(self._cache):
            return True
        self._cache += self._stream.read(Frame.DATA_UNIT_SIZE)
        return len(self._cache) > 0


class Utils(object):
    """Helper class"""

    @staticmethod
    def pad(buf,granularity):
        """pad the buffer if necessary (with zeroes)"""
        l = len(buf)
        if 0 != (l % granularity):
            v=0
            buf += v.to_bytes(Utils.padlen(l,granularity),'little')
        return buf

    @staticmethod
    def padlen(l,granularity):
        """compute the length of the pad for data of length l to get the requested granularity"""
        nunits = (l+granularity-1) // granularity
        return granularity * nunits - l

    @staticmethod
    def hexstr(bytes, head="", separator=" ", tail="", *,skip_long_data=False):
        """Returns an hex string representing bytes

        Args:
            bytes: a list of bytes to stringify, e.g. [59, 22] or a bytearray
            head: the string you want in front of each bytes. Empty by default.
            separator: the string you want between each bytes. One space by default.
            tail: the string you want after each bytes. Empty by default.
        """
        if bytes is not bytearray:
            bytes = bytearray(bytes)
        if (bytes is None) or bytes == []:
            return ""
        else:
            pformat = head+"%-0.2X"+tail
            l=len(bytes)
            if skip_long_data and l>16:
                first = pformat % ((bytes[ 0] + 256) % 256)
                last  = pformat % ((bytes[-1] + 256) % 256)
                return (separator.join([first,"...%d bytes..."%(l-2),last])).rstrip()
            return (separator.join(map(lambda a: pformat % ((a + 256) % 256), bytes))).rstrip()

    @staticmethod
    def int_to_bytes(x, width=-1, byteorder='little'):
        if width<0:
            width = (x.bit_length() + 7) // 8
        b = x.to_bytes(width, byteorder)
        return b

    @staticmethod
    def int_to_ba(x, width=-1, byteorder='little'):
        if width<0:
            width = (x.bit_length() + 7) // 8
        b = x.to_bytes(width, byteorder)
        return bytearray(b)

    @staticmethod
    def to_int(ba, byteorder='little'):
        b = bytes(ba)
        return int.from_bytes(b, byteorder)

    @staticmethod
    def ba(hexstr_or_int):
        """Extract hex numbers from a string and returns them as a bytearray
        It also handles int and list of int as argument
        If it cannot convert, it raises ValueError
        """
        try:
            t1 = hexstr_or_int.lower()
            t2 = "".join([c if c.isalnum() else " " for c in t1])
            t3 = t2.split(" ")
            out = bytearray()
            for bstr in t3:
                if bstr[0:2] == "0x":
                    bstr = bstr[2:]
                if bstr != "":
                    l = len(bstr)
                    if(l % 2):
                        bstr = "0"+bstr
                        l+=1
                    out += bytearray.fromhex(bstr)

        except:
            #seems arg is not a string, assume it is a int
            try:
                out = Utils.int_to_ba(hexstr_or_int)
            except:
                # seems arg is not an int, assume it is a list
                try:
                    out = bytearray(hexstr_or_int)
                except:
                    raise ValueError()
        return out

    @staticmethod
    def ceildiv(a, b):
        return -(-a // b)