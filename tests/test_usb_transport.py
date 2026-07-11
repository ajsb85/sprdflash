"""Hardware-free tests for the USB transport's framing adapter and error paths."""
import pytest

from sprdflash import protocol as p
from sprdflash.usb_transport import UsbPort

EP_OUT, EP_IN = 0x02, 0x81   # UsbPort defaults when open() is bypassed


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


class TestEndpointDiscovery:
    def test_prefers_cdc_data_bulk_endpoints(self):
        pytest.importorskip('usb.util')
        from sprdflash.usb_transport import _discover_data_interface

        class Ep:
            def __init__(self, addr, attrs):
                self.bEndpointAddress = addr
                self.bmAttributes = attrs

        class Intf:
            def __init__(self, num, cls, eps):
                self.bInterfaceNumber = num
                self.bInterfaceClass = cls
                self._eps = eps

            def __iter__(self):
                return iter(self._eps)

        class Dev:
            def __init__(self, intfs):
                self._intfs = intfs

            def get_active_configuration(self):
                return self._intfs

        # intf 0 = CDC-control (interrupt only), intf 1 = CDC-data (bulk 0x02/0x81)
        comm = Intf(0, 0x02, [Ep(0x83, 0x03)])
        data = Intf(1, 0x0A, [Ep(0x02, 0x02), Ep(0x81, 0x02)])
        assert _discover_data_interface(Dev([comm, data])) == (1, 0x02, 0x81)

    def test_falls_back_to_any_bulk_interface(self):
        pytest.importorskip('usb.util')
        from sprdflash.usb_transport import _discover_data_interface

        class Ep:
            def __init__(self, addr):
                self.bEndpointAddress = addr
                self.bmAttributes = 0x02

        class Intf:
            def __init__(self, num, cls, eps):
                self.bInterfaceNumber = num
                self.bInterfaceClass = cls
                self._eps = eps

            def __iter__(self):
                return iter(self._eps)

        class Dev:
            def get_active_configuration(self):
                return [Intf(3, 0xFF, [Ep(0x04), Ep(0x85)])]   # vendor-specific

        assert _discover_data_interface(Dev()) == (3, 0x04, 0x85)


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
