"""Hardware-free tests for the USB transport's framing adapter and error paths."""
import pytest

from sprdflash import protocol as p
from sprdflash.usb_transport import EP_IN, EP_OUT, UsbPort


class FakeUsbDev:
    """Emulates a pyusb device: bulk writes go to a log, reads drain a queue."""

    def __init__(self, in_chunks=()):
        self.written = bytearray()
        self._in = list(in_chunks)

    def is_kernel_driver_active(self, intf):
        return False

    def ctrl_transfer(self, *a, **k):
        return 0

    def write(self, ep, data, timeout=0):
        assert ep == EP_OUT
        self.written += bytes(data)
        return len(data)

    def read(self, ep, size, timeout=0):
        assert ep == EP_IN
        if not self._in:
            raise Exception('Operation timed out')   # mimic USBTimeoutError text
        return list(self._in.pop(0))


class TestUsbPortAdapter:
    def test_write_goes_to_bulk_out(self):
        dev = FakeUsbDev()
        port = UsbPort(dev)
        port.write(b'\x7e\x01\x02')
        assert dev.written == b'\x7e\x01\x02'

    def test_read_buffers_across_calls(self):
        # one bulk-IN transfer delivers a whole frame; read(1) drains it byte by byte
        frame = p.build_message(p.BSL_REP_VER, b'SPRD3', crc_mode=True)
        dev = FakeUsbDev(in_chunks=[frame])
        port = UsbPort(dev)
        out = bytearray()
        for _ in range(len(frame)):
            out += port.read(1)
        assert bytes(out) == frame

    def test_read_timeout_returns_empty(self):
        dev = FakeUsbDev(in_chunks=[])
        port = UsbPort(dev)
        assert port.read(1) == b''

    def test_spdio_over_usb_adapter_handshakes(self):
        # a scripted device that answers VER then ACK, framed over bulk IN
        ver = p.build_message(p.BSL_REP_VER, b'SPRD3', crc_mode=True)
        ack = p.build_message(p.BSL_REP_ACK, b'', crc_mode=True)
        dev = FakeUsbDev(in_chunks=[ver, ack])
        port = UsbPort(dev)
        io = p.SpdIO(port, timeout=1.0)
        assert io.autobaud(attempts=2, timeout=1.0) == b'SPRD3'
        io.connect()
        assert dev.written.startswith(b'\x7e')   # the lone autobaud flag was sent


class TestUsbUnavailable:
    def test_open_maps_notimplemented_to_zadig_hint(self, monkeypatch):
        # on Windows/CDC, libusb raises NotImplementedError when claiming the
        # interface for I/O — verify that becomes the actionable Zadig hint
        usb_util = pytest.importorskip('usb.util')
        monkeypatch.setattr(usb_util, 'claim_interface',
                            lambda *a, **k: (_ for _ in ()).throw(
                                NotImplementedError('Operation not supported')))
        from sprdflash.usb_transport import UsbUnavailable
        port = UsbPort(FakeUsbDev())
        with pytest.raises(UsbUnavailable, match='Zadig'):
            port.open()
