# sprdflash

**Native** flasher for SPRD/UNISOC `.pac` firmware — Air724UG, Air722UG and
other RDA8910 / UIS8910 modules — that speaks the Spreadtrum **BootROM/FDL
download protocol directly over the serial port. No vendor download tool
(CmdDloader.exe / ResearchDownload.exe) is required.**

> Sibling project to [pacflash](https://github.com/ajsb85/pacflash), which
> drives the vendor tool. `sprdflash` reimplements the wire protocol instead.

```
sprdflash info     firmware.pac    # parse + CRC-validate, classify FDL/flash/marker entries
sprdflash identify                 # connect to the BootROM and read its version (safe, read-only)
sprdflash flash    firmware.pac    # load FDL1/FDL2 and write every partition natively
```

## How it works

The Spreadtrum BootROM speaks a framed protocol (BSL) over HDLC:

```
0x7e | escape( type[2] size[2] data[size] checksum[2] ) | 0x7e
```

- BootROM mode uses a **CRC16** checksum (poly `0x11021`); once FDL1 runs the
  device switches to a **ones-complement sum** checksum.
- Flash flow: autobaud (`0x7e` -> `VER`) -> `CONNECT` -> send **FDL1** and
  `EXEC` -> re-handshake in sum-checksum mode -> send **FDL2** and `EXEC` ->
  `START_DATA`/`MIDST_DATA`/`END_DATA` for each real partition -> `NORMAL_RESET`.

The FDL stages and partition load addresses are read straight from the `.pac`
file table; logical erase/format markers are skipped.

### Transport (important)

On the RDA8910/UIS8910 the BootROM enumerates as a CDC-ACM USB gadget
(`0525:A4A7`, "SPRD U2S Diag"), but the BSL protocol does **not** ride the CDC
serial framing. It uses the raw **bulk endpoints** of the data interface
(OUT `0x02`, IN `0x81`), activated by a vendor control transfer
(`bmRequestType=0x21, bRequest=0, wValue=1`). A plain COM-port open only sends
the standard CDC line-state request, which the BootROM ignores — which is why a
pyserial-only approach stays silent. `sprdflash` therefore talks to the device
through **libusb** (`--transport usb`, the default). A `--transport serial`
mode is provided for targets whose BootROM is exposed on a real UART.

Protocol references (clean-room; no vendor code used): the open-source
[spreadtrum_flash / spd_dump](https://github.com/ilyakurdyukov/spreadtrum_flash)
and [iscle/sprdclient](https://github.com/iscle/sprdclient) projects, plus the
device's own USB descriptor.

## Requirements

- Python ≥ 3.10, `pip install "sprdflash[usb]"` (the `[usb]` extra pulls in
  pyusb + a bundled libusb).
- A module in **download mode**. Two entry paths / USB identities are supported:
  - **`1782:4D00`** — the *raw BootROM*, reached by strapping the **`USB_BOOT`**
    pin high (to VDD_EXT / VDD_1V8, directly or via a pull-up) during reset.
    This is the most reliable native target (plain BSL autobaud).
  - **`0525:A4A7`** ("SPRD U2S Diag") — the soft-download interface exposed
    after `AT*DOWNLOAD=1` on the AT port (see
    [pacflash](https://github.com/ajsb85/pacflash) for the automated switch).

  Endpoints are auto-discovered from the descriptor, so either identity works.
- **A WinUSB/libusbK driver bound to the BootROM device.** On Windows the device
  ships with the CDC/usbser (COM-port) driver, through which libusb cannot do
  I/O. Use [Zadig](https://zadig.akeo.ie) once to replace the driver for USB
  `0525:A4A7` with **WinUSB**. This is a deliberate, reversible choice:
  **while WinUSB is bound, the vendor tool and pacflash cannot use the device**
  (they need the COM driver) — swap back to restore them.

## Status

- **`info` is complete and verified** against real firmware (parses and
  CRC-validates the full partition table).
- **`identify` and `flash`** implement the full BSL handshake and flow over the
  libusb transport, with 16 protocol-level tests. End-to-end flashing was **not**
  run against the live module in this environment because it would require
  swapping the module off the CDC driver (which the working
  [pacflash](https://github.com/ajsb85/pacflash) setup depends on). Once WinUSB
  is bound, `identify` is the safe first check — it performs the read-only
  autobaud + CONNECT handshake.

You cannot brick the BootROM: a failed FDL load just means re-entering download
mode and trying again. If in doubt, use pacflash (drives the vendor tool) — it
needs no driver change.

## Example

```
# one-time: Zadig -> USB 0525:A4A7 -> WinUSB -> Replace Driver

> sprdflash identify
connected via usb
BootROM version: SPRD3

> sprdflash flash C:\firmware\LuatOS-Air_V4035_RDA8910_TTS_NOVOLTE_FLOAT.pac
native-flashing ... via usb
  BOOTLOADER      100%
  AP              100%
  PS              100%
  ...
flash complete
```

## Exit codes

| code | meaning |
|------|---------|
| 0    | success |
| 1    | file not found |
| 2    | PAC validation failed |
| 3    | no device / download port |
| 4    | flash / protocol error |
| 5    | USB transport unavailable (install `[usb]`, or bind WinUSB via Zadig) |

## License

MIT — see [LICENSE](LICENSE). Protocol knowledge is from public open-source
projects; no vendor binaries are included or required.
