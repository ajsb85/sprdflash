"""Tests for the PDL protocol layer (frame format from the vendor capture)."""
import struct

from sprdflash import pdl


class TestFrame:
    def test_header_format(self):
        # header = ae | len(u16 LE) | 00 00 | ff | 00 00
        header, body = pdl.build(b'\x00' * 12)
        assert header == bytes.fromhex('ae0c000000ff0000')
        assert body == b'\x00' * 12

    def test_connect_matches_capture(self):
        header, body = pdl.build(pdl.params(pdl.PDL_CONNECT))
        assert header.hex() == 'ae0c000000ff0000'
        assert body.hex() == '000000000000000000000000'

    def test_start_matches_capture(self):
        # START: cmd=4, addr=0x00838000, size=0x33a0, name "PDL1\0"
        payload = pdl.params(pdl.PDL_START_DATA, 0x00838000, 0x33A0, b'PDL1\x00')
        header, body = pdl.build(payload)
        assert header.hex() == 'ae11000000ff0000'   # len 0x11 = 17
        assert body.hex() == '0400000000808300a033000050444c3100'

    def test_midst_matches_capture(self):
        # DATA: cmd=5, block=0, len=0x800, + 2048 bytes
        chunk = bytes(2048)
        payload = pdl.params(pdl.PDL_MIDST_DATA, 0, len(chunk), chunk)
        header, body = pdl.build(payload)
        # len = 12 + 2048 = 2060 = 0x080c
        assert header.hex() == 'ae0c080000ff0000'
        assert body[:12].hex() == '050000000000000000080000'


class FakePdlPort:
    """Replays PDL ae-framed ACKs; records writes (header+payload pairs)."""

    def __init__(self):
        self.writes = []
        self._rx = bytearray()

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        # after a full message (header + payload), queue an ACK
        if len(self.writes) >= 2 and len(self.writes) % 2 == 0:
            ack_payload = struct.pack('<I', 0)  # status 0
            h, b = pdl.build(ack_payload)
            self._rx += h + b

    def read(self, n=1):
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk


class TestPdlIO:
    def test_connect_sends_two_writes_and_parses_ack(self):
        port = FakePdlPort()
        io = pdl.PdlIO(port, timeout=1.0)
        io.connect()
        # header + payload = two separate writes (the critical detail)
        assert len(port.writes) == 2
        assert port.writes[0] == bytes.fromhex('ae0c000000ff0000')

    def test_send_image_sequences_start_midst_end(self):
        port = FakePdlPort()
        io = pdl.PdlIO(port, timeout=1.0)
        data = bytes(pdl.CHUNK + 100)   # one full chunk + a partial
        io.send_image(0x838000, data, checksum=0)
        # writes come in header/payload pairs; extract the payloads
        payloads = [port.writes[i] for i in range(1, len(port.writes), 2)]
        cmds = [struct.unpack('<I', pl[:4])[0] for pl in payloads]
        assert cmds[0] == pdl.PDL_START_DATA
        assert cmds[1] == pdl.PDL_MIDST_DATA
        assert cmds[2] == pdl.PDL_MIDST_DATA
        assert cmds[-1] == pdl.PDL_END_DATA
