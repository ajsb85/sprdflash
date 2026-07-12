"""Tests for the native cross-SDK format path (NV checksums + marker handling).

The constants here are ground truth, verified byte-for-byte against a Frida
trace of the vendor download tool doing a cross-SDK flash of the Air724UG.
"""
import struct

from sprdflash import native


class TestNvChecksums:
    def test_sum32_is_additive_byte_sum(self):
        assert native._sum32(b'') == 0
        assert native._sum32(b'\x01\x02\x03') == 6
        assert native._sum32(b'\xff' * 4) == 0x3FC
        # wraps at 32 bits
        assert native._sum32(b'\xff' * (0x1000000 + 3)) == (255 * (0x1000000 + 3)) & 0xFFFFFFFF

    def test_nv_crc16_is_crc16_arc(self):
        # CRC-16/ARC standard check value for "123456789"
        assert native._nv_crc16(b'123456789') == 0xBB3D
        assert native._nv_crc16(b'') == 0x0000

    def test_nv_fix_crc_patches_leading_be_slot(self):
        blob = b'\x00\x00' + b'hello world padding' * 4
        fixed = native._nv_fix_crc(blob)
        # bytes 2.. are unchanged; only the 2-byte CRC slot is written
        assert fixed[2:] == blob[2:]
        stored = (fixed[0] << 8) | fixed[1]
        assert stored == native._nv_crc16(blob[2:])

    def test_nv_fix_crc_is_idempotent(self):
        blob = bytes(range(256)) * 8
        once = native._nv_fix_crc(blob)
        twice = native._nv_fix_crc(once)
        assert once == twice


class _Entry:
    """Minimal PAC-entry stand-in for the classify/marker helpers."""
    def __init__(self, file_id, address, size):
        self.file_id = file_id
        self.address = address
        self.size = size
        self.is_marker = size == 0


class _Info:
    def __init__(self, entries):
        self.entries = entries


def _v4035_markers():
    # the logical markers of the V4035 PAC, in file order
    return _Info([
        _Entry('PhaseCheck', 0xFE000002, 0),
        _Entry('FMT_FSSYS', 0xFE000006, 0),
        _Entry('FMT_FSEXT', 0xFE000006, 0),
        _Entry('FLASH', 0xFE000001, 0),
        _Entry('NV', 0xFE000003, 131072),
        _Entry('PREPACK', 0xFE000004, 92),
    ])


class TestMarkerHandling:
    def test_erase_ops_match_vendor(self):
        # vendor issues exactly two erases: FMT_FSSYS "SYSF" and FLASH 0,
        # deduped by address (FMT_FSEXT shares 0xFE000006 and is skipped)
        ops = native._erase_ops(_v4035_markers())
        assert ops == [
            ('FMT_FSSYS', 0xFE000006, b'SYSF'),
            ('FLASH', 0xFE000001, b'\x00\x00\x00\x00'),
        ]

    def test_data_markers_are_nv_then_prepack(self):
        dm = native._data_markers(_v4035_markers())
        assert [(e.file_id, e.address, e.size) for e in dm] == [
            ('NV', 0xFE000003, 131072),
            ('PREPACK', 0xFE000004, 92),
        ]

    def test_erase_ops_skip_unknown_and_phasecheck(self):
        info = _Info([_Entry('PhaseCheck', 0xFE000002, 0),
                      _Entry('WHATEVER', 0xFE000009, 0)])
        assert native._erase_ops(info) == []


class TestNvStartPayload:
    def test_start_payload_shape(self):
        # NV START_DATA = addr(4 BE) | size(4 BE) | sum32(4 BE)
        data = native._nv_fix_crc(b'\x00\x00' + bytes(1022))
        payload = struct.pack('>III', 0xFE000003, len(data), native._sum32(data))
        assert len(payload) == 12
        assert payload[:8] == bytes.fromhex('fe00000300000400')
