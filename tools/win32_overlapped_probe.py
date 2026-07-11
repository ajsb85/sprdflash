"""Native BSL checkbaud over RAW Win32 overlapped serial I/O.

pyserial writes then reads, so the device's microsecond-fast checkbaud reply is
gone before the read runs. CmdDloader instead keeps an overlapped ReadFile
posted continuously (SetCommMask/overlapped), so the reply is captured the
instant it arrives. This script replicates that: post an overlapped read, fire
the 0x7e checkbaud, and wait on the read event - matching the vendor exactly.
"""
import ctypes as C
import sys
import time
from ctypes import wintypes

sys.path.insert(0, r'C:\Users\ajsb8\dev\sprdflash\src')
sys.path.insert(0, r'C:\Users\ajsb8\dev\pacflash\src')
from pacflash import device  # noqa: E402
from sprdflash import protocol as p  # noqa: E402

k32 = C.WinDLL('kernel32', use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID = wintypes.HANDLE(-1).value
ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 0x102


class DCB(C.Structure):
    _fields_ = [('DCBlength', wintypes.DWORD), ('BaudRate', wintypes.DWORD),
                ('fBits', wintypes.DWORD), ('wReserved', wintypes.WORD),
                ('XonLim', wintypes.WORD), ('XoffLim', wintypes.WORD),
                ('ByteSize', wintypes.BYTE), ('Parity', wintypes.BYTE),
                ('StopBits', wintypes.BYTE), ('XonChar', C.c_char),
                ('XoffChar', C.c_char), ('ErrorChar', C.c_char),
                ('EofChar', C.c_char), ('EvtChar', C.c_char),
                ('wReserved1', wintypes.WORD)]


class COMMTIMEOUTS(C.Structure):
    _fields_ = [('ReadIntervalTimeout', wintypes.DWORD),
                ('ReadTotalTimeoutMultiplier', wintypes.DWORD),
                ('ReadTotalTimeoutConstant', wintypes.DWORD),
                ('WriteTotalTimeoutMultiplier', wintypes.DWORD),
                ('WriteTotalTimeoutConstant', wintypes.DWORD)]


class OVERLAPPED(C.Structure):
    _fields_ = [('Internal', C.c_void_p), ('InternalHigh', C.c_void_p),
                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE)]


def open_overlapped(com):
    path = r'\\.\%s' % com
    h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                        OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if h == INVALID:
        raise OSError('CreateFile failed %d' % C.get_last_error())
    dcb = DCB(); dcb.DCBlength = C.sizeof(DCB)
    k32.GetCommState(h, C.byref(dcb))
    dcb.BaudRate = 115200; dcb.ByteSize = 8; dcb.Parity = 0; dcb.StopBits = 0
    # fBinary=1, fDtrControl=1 (enable), fRtsControl=1 (enable)
    dcb.fBits = 0x1 | (0x1 << 4) | (0x1 << 12)
    if not k32.SetCommState(h, C.byref(dcb)):
        raise OSError('SetCommState failed %d' % C.get_last_error())
    to = COMMTIMEOUTS(0xFFFFFFFF, 0, 0, 0, 0)  # return immediately with available bytes
    k32.SetCommTimeouts(h, C.byref(to))
    k32.PurgeComm(h, 0xF)
    return h


def main():
    if not device.find_download_port():
        mp = device.find_module_ports(); at = device.probe_at_port(mp)
        if not at:
            print('not in normal or download mode; power-cycle'); return 1
        print('AT*DOWNLOAD ->', at.device)
        device.enter_download_mode(at)
    port = None; t0 = time.time()
    while time.time() - t0 < 20:
        dl = device.find_download_port()
        if dl: port = dl.device; break
    if not port:
        print('no download port'); return 1
    # open ASAP with retry (enumeration race)
    h = None
    t1 = time.time()
    while time.time() - t1 < 8:
        try:
            h = open_overlapped(port); break
        except OSError:
            time.sleep(0.02)
    if not h:
        print('open failed'); return 1
    print('opened', port, 'overlapped')

    rd_evt = k32.CreateEventW(None, True, False, None)
    wr_evt = k32.CreateEventW(None, True, False, None)
    rbuf = (C.c_char * 512)()
    ver = None
    acc = bytearray()
    total_written = 0
    t0 = time.time()
    tries = 0
    while time.time() - t0 < 6 and ver is None:
        tries += 1
        # 1) POST the overlapped read first
        ov_r = OVERLAPPED(); ov_r.hEvent = rd_evt
        k32.ResetEvent(rd_evt)
        nread = wintypes.DWORD(0)
        ok = k32.ReadFile(h, rbuf, 512, C.byref(nread), C.byref(ov_r))
        # 2) write the checkbaud 0x7e (overlapped)
        ov_w = OVERLAPPED(); ov_w.hEvent = wr_evt
        k32.ResetEvent(wr_evt)
        wb = C.c_char(0x7e)
        nw = wintypes.DWORD(0)
        k32.WriteFile(h, C.byref(wb), 1, C.byref(nw), C.byref(ov_w))
        k32.WaitForSingleObject(wr_evt, 100)
        wdone = wintypes.DWORD(0)
        k32.GetOverlappedResult(h, C.byref(ov_w), C.byref(wdone), True)
        total_written += wdone.value
        # 3) wait briefly for the read to complete
        if not ok and C.get_last_error() == ERROR_IO_PENDING:
            r = k32.WaitForSingleObject(rd_evt, 30)
            if r == WAIT_TIMEOUT:
                k32.CancelIo(h)
                k32.GetOverlappedResult(h, C.byref(ov_r), C.byref(nread), True)
        got = bytes(rbuf[:nread.value]) if nread.value else b''
        if got:
            acc += got
            if acc.count(0x7e) >= 2:
                seg = bytes(acc[acc.index(0x7e):])
                try:
                    body = p.hdlc_unescape(seg.strip(b'\x7e'))
                    cmd, data = p.parse_message(body)
                    print(f'FRAME after {tries}: {p.rep_name(cmd)} {data[:40]!r}')
                    if cmd == p.BSL_REP_VER: ver = data; break
                    acc = bytearray()
                except Exception:
                    acc = bytearray()
    print(f'{tries} tries in {time.time()-t0:.2f}s; bytes_written={total_written}; '
          f'acc={bytes(acc)[:32].hex() if acc else "none"}')
    if ver:
        print('*** VER:', ver.decode('latin-1', 'replace').strip(), '— NATIVE HANDSHAKE WORKS ***')
    else:
        print('no VER')
    k32.CloseHandle(h); k32.CloseHandle(rd_evt); k32.CloseHandle(wr_evt)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
