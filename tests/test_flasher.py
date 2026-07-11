"""Flasher logic tests that don't touch hardware."""
import struct

from sprdflash import protocol as p
from sprdflash.flasher import classify, flashable_entries, send_stage
from sprdflash.pac import PacEntry, PacInfo


def _entry(fid, size, address, offset=0):
    return PacEntry(file_id=fid, file_name=fid.lower() + '.img', size=size,
                    offset=offset, address=address, flag=0, omit=0)


def _info(entries):
    return PacInfo(version='BP', product_name='X', product_version='1',
                   size=0, file_count=len(entries), mode=0, flash_type=0,
                   magic=0, header_crc_ok=True, payload_crc_ok=True, entries=entries)


class TestClassify:
    def test_roles(self):
        assert classify(_entry('HOST_FDL', 13216, 0x00838000)) == 'fdl1'
        assert classify(_entry('FDL2', 58112, 0x00810000)) == 'fdl2'
        assert classify(_entry('PhaseCheck', 0, 0xFE000002)) == 'marker'
        assert classify(_entry('AP', 3322880, 0x60010000)) == 'flash'
        assert classify(_entry('NV', 131072, 0xFE000003)) == 'marker'
        # trailing XML manifest: address 0 -> not flashable
        assert classify(_entry('', 4308, 0)) == 'marker'


class TestFlashableEntries:
    def test_excludes_fdls_markers_and_manifest(self):
        info = _info([
            _entry('HOST_FDL', 13216, 0x00838000),
            _entry('FDL2', 58112, 0x00810000),
            _entry('PhaseCheck', 0, 0xFE000002),
            _entry('BOOTLOADER', 42496, 0x60000000),
            _entry('AP', 3322880, 0x60010000),
            _entry('FLASH', 0, 0xFE000001),
            _entry('NV', 131072, 0xFE000003),   # logical address -> excluded
            _entry('', 4308, 0),                # XML manifest -> excluded
        ])
        got = [e.file_id for e in flashable_entries(info)]
        assert got == ['BOOTLOADER', 'AP']


class RecordingIO:
    """Captures the command sequence send_stage issues, auto-ACKing."""

    def __init__(self):
        self.calls = []

    def command(self, cmd, data=b'', expect=p.BSL_REP_ACK, timeout=None, what=None):
        self.calls.append((cmd, len(data)))
        return b''


class TestSendStage:
    def test_start_midst_end_sequence(self):
        io = RecordingIO()
        payload = bytes(1000)
        send_stage(io, 0x60000000, payload, chunk=400)
        cmds = [c for c, _ in io.calls]
        assert cmds[0] == p.BSL_CMD_START_DATA
        assert cmds[-1] == p.BSL_CMD_END_DATA
        midst = [n for c, n in io.calls if c == p.BSL_CMD_MIDST_DATA]
        assert midst == [400, 400, 200]   # 1000 bytes in 400-byte chunks

    def test_start_data_carries_addr_and_size(self):
        captured = {}

        class IO2(RecordingIO):
            def command(self, cmd, data=b'', expect=p.BSL_REP_ACK, timeout=None, what=None):
                if cmd == p.BSL_CMD_START_DATA:
                    captured['data'] = data
                return super().command(cmd, data, expect, timeout, what)

        io = IO2()
        send_stage(io, 0x12345678, bytes(10), chunk=64)
        addr, size = struct.unpack('>II', captured['data'])
        assert addr == 0x12345678 and size == 10
