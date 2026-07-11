"""Protocol-layer tests: checksums, HDLC framing, message build/parse, and a
scripted SpdIO handshake against a fake serial port."""
import struct

import pytest

from sprdflash import protocol as p


class TestChecksums:
    def test_crc16_matches_reference_loop(self):
        # independent re-implementation of spd_crc16 as an oracle
        def oracle(data, crc=0):
            crc &= 0xFFFF
            for b in data:
                crc ^= b << 8
                for _ in range(8):
                    crc = ((crc << 1) ^ (0x11021 if crc & 0x8000 else 0)) & 0xFFFF
            return crc
        for sample in (b'', b'\x00', b'123456789', bytes(range(64)),
                       b'\x00\x00\x00\x00'):
            assert p.crc16(sample) == oracle(sample)

    def test_crc16_known_vector(self):
        # CONNECT message body: type=0x0000 size=0x0000 -> 4 zero bytes
        assert p.crc16(b'\x00\x00\x00\x00') == 0x0000  # 4 zero bytes -> crc stays 0

    def test_sprd_checksum_matches_real_vendor_packets(self):
        # Ground truth captured from the vendor ResearchDownload wire log
        # flashing an RDA8910 (Air724UG). The device uses the SPRD sum
        # checksum for every packet, including the BootROM-level CONNECT.
        vectors = [
            (bytes.fromhex('00000000'), 0xFFFF),                       # CONNECT
            (bytes.fromhex('00800000'), 0xFF7F),                       # ACK
            (bytes.fromhex('000900040001c200'), 0x3DF1),               # CHANGE_BAUD 115200
            (b'\x00\x81\x00\x22' + b'Spreadtrum Boot Block version 1.2\x00', 0xA9E9),  # VER
        ]
        for body, want in vectors:
            assert p.sprd_checksum(body) == want, f'{body.hex()} -> {p.sprd_checksum(body):#06x}'

    def test_connect_frame_matches_vendor_bytes(self):
        # the full on-the-wire CONNECT frame the vendor sends
        assert p.build_message(p.BSL_CMD_CONNECT, b'', checksum='sprd').hex() == '7e00000000ffff7e'

    def test_detect_checksum(self):
        ver_body = b'\x00\x81\x00\x22' + b'Spreadtrum Boot Block version 1.2\x00'
        assert p.detect_checksum(ver_body + p.sprd_checksum(ver_body).to_bytes(2, 'big')) == 'sprd'
        crc_body = b'\x00\x81\x00\x05SPRD3'
        assert p.detect_checksum(crc_body + p.crc16(crc_body).to_bytes(2, 'big')) == 'crc'


class TestHdlc:
    def test_escape_roundtrip(self):
        raw = bytes([0x7e, 0x7d, 0x00, 0x41, 0x7e, 0x20])
        esc = p.hdlc_escape(raw)
        assert 0x7e not in esc[:]  # flags must not appear escaped-region
        assert p.hdlc_unescape(esc) == raw

    def test_escape_expands_special_bytes(self):
        assert p.hdlc_escape(b'\x7e') == b'\x7d\x5e'
        assert p.hdlc_escape(b'\x7d') == b'\x7d\x5d'
        assert p.hdlc_escape(b'\x41') == b'\x41'


class TestMessage:
    def test_build_connect_bootrom(self):
        msg = p.build_message(p.BSL_CMD_CONNECT, b'', checksum='crc')
        assert msg[0] == p.HDLC_FLAG and msg[-1] == p.HDLC_FLAG
        # unescape the middle and check type/size/crc
        body = p.hdlc_unescape(msg[1:-1])
        cmd, size = struct.unpack('>HH', body[:4])
        assert cmd == p.BSL_CMD_CONNECT and size == 0
        assert struct.unpack('>H', body[4:6])[0] == p.crc16(body[:4])

    def test_build_parse_roundtrip_with_escapes(self):
        payload = bytes([0x7e, 0x7d, 0xaa, 0x55])
        msg = p.build_message(p.BSL_CMD_MIDST_DATA, payload, checksum='sprd')
        body = p.hdlc_unescape(msg[1:-1])
        cmd, data = p.parse_message(body)
        assert cmd == p.BSL_CMD_MIDST_DATA
        assert data == payload

    def test_parse_rejects_short(self):
        with pytest.raises(ValueError):
            p.parse_message(b'\x00\x00')

    def test_parse_rejects_truncated_payload(self):
        body = struct.pack('>HH', p.BSL_CMD_READ_FLASH, 8) + b'\x01\x02'
        with pytest.raises(ValueError, match='truncated'):
            p.parse_message(body)


class FakePort:
    """Serial-like port replaying host<->device frames from a script."""

    def __init__(self, responses):
        # responses: list of (type, data) the device will emit, in order
        self._responses = list(responses)
        self.written = bytearray()
        self._rx = bytearray()

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        # every host write pops the next scripted device reply into the rx buffer
        if self._responses:
            cmd, data = self._responses.pop(0)
            if cmd is not None:
                self._rx += p.build_message(cmd, data, checksum='crc')

    def read(self, n=1):
        if not self._rx:
            return b''
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk


class TestSpdIOHandshake:
    def test_autobaud_then_connect(self):
        port = FakePort([
            (p.BSL_REP_VER, b'SPRD3'),   # answer to the autobaud 0x7e
            (p.BSL_REP_ACK, b''),        # answer to CONNECT
        ])
        io = p.SpdIO(port, timeout=1.0)
        ver = io.autobaud(attempts=3, timeout=1.0)
        assert ver == b'SPRD3'
        io.connect()   # would raise ProtocolError on a non-ACK

    def test_command_unexpected_reply_raises(self):
        port = FakePort([(p.BSL_REP_OPERATION_FAILED, b'')])
        io = p.SpdIO(port, timeout=1.0)
        with pytest.raises(p.ProtocolError, match='OPERATION_FAILED'):
            io.command(p.BSL_CMD_START_DATA, b'\x00' * 8, what='START_DATA')
