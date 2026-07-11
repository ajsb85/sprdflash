"""Native BSL connect that hits the RDA8910 download agent's brief beacon window.

The 0525:a4a7 soft-download agent (reached via AT*DOWNLOAD) only beacons for a
short window right after entering download mode. This script minimises latency:
it sends AT*DOWNLOAD, then the instant the download COM port appears it opens it
with no sleeps and immediately hammers the checkbaud/CONNECT, reading the reply.

Run with the module in NORMAL mode (freshly power-cycled is best):
    python tools/tight_native_connect.py
"""
import sys
import time

import serial

sys.path.insert(0, r'C:\Users\ajsb8\dev\sprdflash\src')
sys.path.insert(0, r'C:\Users\ajsb8\dev\pacflash\src')

from pacflash import device  # noqa: E402
from sprdflash import protocol as p  # noqa: E402


def open_immediately(dev_name, attempts=200):
    for _ in range(attempts):
        try:
            s = serial.Serial(dev_name, 115200, timeout=0.01, write_timeout=0.5)
            s.dtr = True
            s.rts = True
            return s
        except Exception:
            pass  # no sleep - spin as fast as possible
    return None


def main():
    mp = device.find_module_ports()
    at = device.probe_at_port(mp) if mp else None
    if not device.find_download_port():
        if not at:
            print('module not in normal mode and no download port; power-cycle first')
            return 1
        print(f'sending AT*DOWNLOAD=1 to {at.device}')
        device.enter_download_mode(at)

    # spin-wait for the port, then open with ZERO delay
    port = None
    t0 = time.time()
    while time.time() - t0 < 20:
        dl = device.find_download_port()
        if dl:
            port = dl.device
            break
    if not port:
        print('download port never appeared')
        return 1
    s = open_immediately(port)
    if not s:
        print('could not open', port)
        return 1

    # immediately blast checkbaud and read; look for VER
    ver = None
    buf = bytearray()
    t0 = time.time()
    tries = 0
    while time.time() - t0 < 3.0:
        s.write(b'\x7e')
        tries += 1
        d = s.read(256)
        if d:
            buf += d
            if buf.count(0x7e) >= 2:
                seg = bytes(buf[buf.index(0x7e):])
                try:
                    body = p.hdlc_unescape(seg.strip(b'\x7e'))
                    cmd, data = p.parse_message(body)
                    if cmd == p.BSL_REP_VER:
                        ver = data
                        break
                except Exception:
                    buf = bytearray()  # resync
    print(f'{tries} checkbaud tries in {time.time()-t0:.2f}s')
    if ver:
        print('*** GOT VER:', ver.decode("latin-1", "replace").strip())
        io = p.SpdIO(s, timeout=1.0)
        io.checksum = p.detect_checksum(
            body[:-2] + body[-2:]) if 'body' in dir() else 'sprd'
        io.checksum = 'sprd'
        try:
            io.connect()
            print('*** CONNECT ACK - native BSL handshake WORKS ***')
        except Exception as e:
            print('connect after VER failed:', e)
    else:
        print('no VER; got', bytes(buf)[:48].hex() if buf else 'silence',
              '(beacon window likely missed - power-cycle and retry)')
    s.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
