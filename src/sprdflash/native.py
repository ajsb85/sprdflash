"""Complete native flash: PDL (FDL1) -> BSL (FDL2 + partitions) -> reset.

Ties together the two link layers reverse-engineered from the vendor tool:
  Phase 1 (pdl.py)      : load + exec the first-stage loader (HOST_FDL/"PDL1")
  Phase 2 (protocol.py) : BSL handshake, load + exec FDL2, write each partition

Both phases were validated on a real Air724UG (RDA8910).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from . import pdl
from . import protocol as p
from .flasher import classify, send_stage
from .pac import parse_pac

log = logging.getLogger('sprdflash')

Progress = Callable[[str, int, int], None]


def _open_serial(com: str, timeout: float = 0.3):
    import serial
    s = None
    for _ in range(200):
        try:
            s = serial.Serial(com, 115200, timeout=timeout, write_timeout=2.0)
            break
        except Exception:
            time.sleep(0.03)
    if s is None:
        raise pdl.PdlError(f'could not open {com}')
    try:
        s.dtr = True
        s.rts = True
    except Exception:
        pass
    time.sleep(0.05)
    try:
        s.reset_input_buffer()
    except Exception:
        pass
    return s


def _read_entry(f, entry):
    f.seek(entry.offset)
    return f.read(entry.size)


def native_flash(com: str, pac_path: str | Path, *,
                 progress: Progress | None = None,
                 fdl1_end_checksum: int = 0,
                 do_reset: bool = True) -> None:
    """Flash *pac_path* to the module on serial port *com*, entirely natively.

    The module must already be in download mode (0525:a4a7 / COM port). The
    PDL END checksum for FDL1 is not verified by the agent, so it defaults to 0.
    """
    pac_path = Path(pac_path)
    info = parse_pac(pac_path, verify_payload=True)
    if not info.crc_ok:
        raise pdl.PdlError('PAC checksum mismatch - refusing to flash')

    fdl1 = next((e for e in info.entries if classify(e) == 'fdl1'), None)
    fdl2 = next((e for e in info.entries if classify(e) == 'fdl2'), None)
    if not fdl1:
        raise pdl.PdlError('no FDL1 (HOST_FDL) stage in the PAC')
    partitions = [e for e in info.entries if classify(e) == 'flash']

    with open(pac_path, 'rb') as f:
        fdl1_data = _read_entry(f, fdl1)
        fdl2_data = _read_entry(f, fdl2) if fdl2 else b''
        part_data = {e.file_id: _read_entry(f, e) for e in partitions}

    s = _open_serial(com)
    try:
        # ---- Phase 1: PDL loads and execs FDL1 -----------------------------
        pio = pdl.PdlIO(s, timeout=2.0)
        log.info('PDL connect')
        pio.connect()
        log.info('PDL load FDL1 (%d bytes @ %#x)', len(fdl1_data), fdl1.address)
        pio.send_image(fdl1.address, fdl1_data, checksum=fdl1_end_checksum,
                       progress=(lambda d, t: progress('FDL1', d, t)) if progress else None)
        ver = pio.exec_and_get_ver(timeout=5.0)
        body = p.hdlc_unescape(ver.strip(b'\x7e'))
        _, vdata = p.parse_message(body)
        log.info('FDL1 running: %s', vdata.decode('latin-1', 'replace').strip())

        # ---- Phase 2: BSL loads FDL2, then writes partitions ---------------
        io = p.SpdIO(s, timeout=2.0)
        io.checksum = 'sprd'
        io.connect()
        if fdl2:
            log.info('BSL load FDL2 (%d bytes @ %#x)', len(fdl2_data), fdl2.address)
            send_stage(io, fdl2.address, fdl2_data,
                       progress=(lambda d, t: progress('FDL2', d, t)) if progress else None)
            io.command(p.BSL_CMD_EXEC_DATA, timeout=15.0, what='EXEC FDL2')
            io.connect()   # re-handshake under FDL2 (vendor "Connect2")

        for e in partitions:
            data = part_data[e.file_id]
            log.info('flash %s (%d bytes @ %#x)', e.file_id, len(data), e.address)
            send_stage(io, e.address, data,
                       progress=(lambda d, t, _e=e: progress(_e.file_id, d, t)) if progress else None)

        if do_reset:
            io.send(p.BSL_CMD_NORMAL_RESET)
            log.info('reset; module reboots into the new firmware')
    finally:
        try:
            s.close()
        except Exception:
            pass
