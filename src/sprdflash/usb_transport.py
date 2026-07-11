"""libusb (pyusb) transport for the Spreadtrum BootROM download gadget.

The BootROM enumerates as a CDC-ACM device (USB 0525:A4A7, "SPRD U2S Diag")
but the BSL protocol does **not** ride the CDC serial framing - it uses the
raw bulk endpoints of the data interface, kicked off by a vendor control
transfer. This mirrors the open-source ``iscle/sprdclient`` reference and the
behaviour of the vendor ChannelD.dll.

Wire-up (confirmed against the live device descriptor):

- interface 1, class 0x0a (CDC-Data): bulk OUT ``0x02``, bulk IN ``0x81``
- connect kick: control transfer ``bmRequestType=0x21, bRequest=0,
  wValue=1, wIndex=0`` (asserts the line/activates the endpoints)
- then a lone ``0x7e`` on the bulk OUT starts autobaud; the device answers a
  ``BSL_REP_VER`` frame on bulk IN.

**Windows note:** libusb can read descriptors while the device is on the
CDC/usbser driver, but *I/O* (open/claim/transfer) requires the device to be
bound to a WinUSB/libusbK driver (use Zadig once). That is mutually exclusive
with the vendor tool / pacflash, which need the COM driver.
"""
from __future__ import annotations

import logging

log = logging.getLogger('sprdflash')

DOWNLOAD_VID = 0x0525
DOWNLOAD_PID = 0xA4A7
DATA_INTERFACE = 1
EP_OUT = 0x02
EP_IN = 0x81

# vendor connect kick (see module docstring)
CTRL_REQUEST_TYPE = 0x21
CTRL_REQUEST = 0x00
CTRL_VALUE = 0x01
CTRL_INDEX = 0x00


class UsbUnavailable(RuntimeError):
    """pyusb/libusb backend missing, or the device is not WinUSB-bound."""


def _is_timeout(exc: Exception) -> bool:
    try:
        import usb.core
        if isinstance(exc, usb.core.USBTimeoutError):
            return True
    except Exception:
        pass
    msg = str(exc).lower()
    return 'timed out' in msg or 'timeout' in msg


def _get_backend():
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:
        return None   # pyusb will fall back to a system libusb if present


def find_bootrom():
    """Return the pyusb device for the BootROM gadget, or None."""
    try:
        import usb.core
    except ImportError as e:
        raise UsbUnavailable(
            'pyusb is not installed - install with: pip install "sprdflash[usb]"') from e
    return usb.core.find(idVendor=DOWNLOAD_VID, idProduct=DOWNLOAD_PID,
                         backend=_get_backend())


class UsbPort:
    """A serial-port-like adapter (read/write/flush) over the BootROM's bulk
    endpoints, so the framed SpdIO layer can drive it unchanged."""

    def __init__(self, dev, timeout: float = 2.0):
        self.dev = dev
        self.timeout_ms = int(timeout * 1000)
        self._rx = bytearray()
        self._opened = False

    def open(self) -> None:
        import usb.util
        try:
            try:
                if self.dev.is_kernel_driver_active(DATA_INTERFACE):
                    self.dev.detach_kernel_driver(DATA_INTERFACE)
            except Exception:
                # not supported on Windows (WinUSB has no "kernel driver" concept)
                pass
            usb.util.claim_interface(self.dev, DATA_INTERFACE)
            # kick the endpoints alive (vendor connect request)
            self.dev.ctrl_transfer(CTRL_REQUEST_TYPE, CTRL_REQUEST,
                                   CTRL_VALUE, CTRL_INDEX, None, self.timeout_ms)
            self._opened = True
        except NotImplementedError as e:
            raise UsbUnavailable(
                'the BootROM port is not accessible via libusb - it is bound to '
                'the CDC/usbser driver. Replace that driver with WinUSB using '
                'Zadig (https://zadig.akeo.ie) for USB 0525:A4A7, then retry. '
                'Note: this disables the vendor tool / pacflash for this device '
                'until you restore the CDC driver.') from e
        except Exception as e:
            raise UsbUnavailable(f'could not open the BootROM USB device: {e}') from e

    # -- serial-like API --------------------------------------------------
    def write(self, data: bytes) -> int:
        return self.dev.write(EP_OUT, data, timeout=self.timeout_ms)

    def flush(self) -> None:
        pass

    def read(self, n: int = 1) -> bytes:
        if not self._rx:
            try:
                chunk = self.dev.read(EP_IN, 4096, timeout=self.timeout_ms)
                self._rx += bytes(chunk)
            except Exception as e:
                # a bulk-IN read with no data raises USBTimeoutError; its message
                # is like "[Errno 110] Operation timed out"
                if _is_timeout(e):
                    return b''
                raise
        take = self._rx[:n]
        del self._rx[:n]
        return bytes(take)

    def close(self) -> None:
        if self._opened:
            try:
                import usb.util
                usb.util.release_interface(self.dev, DATA_INTERFACE)
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self._opened = False


def open_usb_port(timeout: float = 2.0) -> UsbPort:
    dev = find_bootrom()
    if dev is None:
        raise UsbUnavailable(
            f'no BootROM device (USB {DOWNLOAD_VID:04x}:{DOWNLOAD_PID:04x}) found. '
            'Put the module in download mode first.')
    port = UsbPort(dev, timeout=timeout)
    port.open()
    return port
