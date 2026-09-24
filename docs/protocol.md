# MXW01 BLE protocol: verified notes

Everything here was checked against our unit (firmware `1.9.3.1.2`) unless marked *(community)* or *(unverified)*.
Community sources: [jeremy46231/MXW01-catprinter](https://github.com/jeremy46231/MXW01-catprinter),
[MaikelChan/CatPrinterBLE](https://github.com/MaikelChan/CatPrinterBLE),
[kamome-run/jarful](https://github.com/kamome-run/jarful).

## Advertising
- Name `MXW01`, service UUID `0xAF30`, manufacturer data (company `0x0F48`) `57 40 18 b2`.
- Advertises **intermittently**. A 5s scan can miss it, so scan for 10–15s.
- Advertises only while nothing is connected. **Auto-powers off after a few idle minutes unless a client is
  connected.** A held connection keeps it on (LED goes solid green), which is what `open-print serve` does.
- One central at a time. The phone app must be closed for us to connect.

## GATT (service `AE30` unless noted)
| Char | Props | Use |
|---|---|---|
| AE01 | write-no-rsp | control commands |
| AE02 | notify | command replies (incl. AA print-complete) |
| AE03 | write-no-rsp | raw bitmap data |
| AE04 | notify | nothing observed, even during prints |
| AE05 | indicate | nothing observed |
| AE10 | read/write | reads `00 00 00 00` |
| AE3A: AE3B / AE3C | write-no-rsp / notify | untouched (probably OTA firmware update, so don't poke it) |

MTU on macOS: 185, so data chunks are 182 bytes.

## Framing
Host → AE01: `22 21 CMD 00 LEN_LO LEN_HI PAYLOAD… CRC8 FF`. CRC-8 (poly 0x07, init 0) over the payload only.
AE02 → host: `22 21 CMD xx LEN_LO LEN_HI PAYLOAD…` (no CRC or trailer; `xx` is usually 00, 03 on status).

## Commands
| Cmd | Payload sent | Reply payload | Notes |
|---|---|---|---|
| A1 status | `00` | 10 bytes, see below | verified |
| A2 heat/intensity | `[level]` | none | verified; saturates at ~0x5D (see Printing) |
| A3 feed | u16 LE distance | `00` | verified: **~1 mm per unit** (80 → 81 mm) |
| A4 retract | u16 LE distance | `00` | verified: **~1 mm per unit** (40 → ~38.5 mm) |
| A7 "query count" | `00` | `5e e3 4c 36 dd 5e` | 6 bytes, constant; looks like a device ID/MAC, not a count |
| A9 print request | `LINES(u16 LE) 30 MODE` | `00` accepted, `02` no paper | verified for modes 0 and 2; any line count up to 65535 is accepted (no size pre-check) |
| AA print complete | (printer → host) | `?` | arrives on AE02 after the job physically finishes |
| AB battery | `00` | `[percent]` | 0x56 = 86% |
| AC cancel | `00` | `00` | verified: stops a running print within ~1 s (a 480-row job stopped at ~row 160). Sends an early AA. **Skips the tear-off feed** |
| AD flush / go | `00` | none | starts printing buffered data |
| AE ? | `00` | `00` | harmless, unknown |
| B0 print type | `00` | `01` | |
| B1 version | `00` | ASCII `1.9.3.1.2` + `00` | |
| B2 ? | `00` | `00` | harmless, unknown |
| B3 ? | `00` | no reply | |

Unknown opcodes (A5, A6, A8, AF, B4+) were **deliberately not sent**. They could be settings writes.

### Status payload (A1)
```
idx: 0     1  2  3     4     5  6        7         8-9
     state 00 00 batt  temp  00 errflag  errcode   sensor (u16 LE)
idle:     00 00 00 56 14 00 00 00 09 01
no paper: 00 00 00 54 1d 00 01 01 a6 08
```
- state: 0 standby, 1 printing, 2 moving paper (seen during **both** feed and retract).
- temp in °C. Went 20 → 44 °C over ~15 short test prints.
- errflag ≠ 0 means error. errcode **1 = no paper (verified with the lid open *and* with the lid closed and
  empty; the printer can't tell these apart)**. 9 = no paper, 4 = overheated, 8 = low battery *(community,
  unverified)*.
- sensor: probably the raw paper sensor. ~0x00FD–0x0116 with paper, 0x08A6–0x0C44 without; it wobbles while paper moves.
- The printer gives **no beep or LED change** on paper out. Status is the only signal.

## Printing
1. `A2 level`, then `A1` (check errflag == 0).
2. `A9 lines 30 mode` and wait for the A9 reply `00`.
3. Stream rows to AE03 in 182-byte chunks **with ≥30 ms between chunks** (see pacing).
4. `AD 00`, then wait for `AA`.
5. The printer then **auto-feeds ~13.5 mm** so the print clears the tear bar. Every job costs this, so batch
   content into one job instead of many small ones.

**Busy state:** motion commands (A3/A4, probably A9) sent while state ≠ 0 are acked with `00` but
**silently ignored**. Poll A1 until state == 0 first. The driver's `wait_idle()` does this.

### Bitmap formats
| Mode | A9 mode byte | Bytes/row | Packing |
|---|---|---|---|
| 1-bit mono | `0x00` | 48 | bit `x&7` of byte `x>>3` (LSB = leftmost dot); 1 = black |
| 4-bit gray | `0x02` | 192 | high nibble = left dot, low nibble = right dot; 0 = white, 0xF = black |
| `0x01` | *(community: also 1bpp; untested)* | | |

Width is always 384 dots (48mm printable, 8 dots/mm ≈ 203 dpi). The first row sent prints first, so to read
the output with the printer sitting upright, rotate the image 180° before sending.

### Pacing (important)
The printer has no flow control that we can see (nothing on AE04/AE05), and it silently drops data it can't
keep up with. Symptom: rows become misaligned or garbled partway through the job.
- 10 ms between chunks (~13 KB/s): clean up to ~19 KB, then garbage. Tested with a 240-row 4-bit job that
  broke at exactly row 100 (19,200 bytes).
- 15 ms between chunks: broke at ~23 KB (row 120 of 192-byte rows).
- 20 ms and 30 ms between chunks: a 46 KB 4-bit job printed clean. A 23 KB mono ruler at 25 ms was clean.
- **Default 25 ms** (~7 KB/s) for margin.
Mono jobs are 4× smaller, so they usually stay under the ~19 KB cliff, but long mono prints would hit it too.

### Job length and streaming
One A9 job can be arbitrarily long. The printer **starts printing on its own once ~48 KB is buffered**
(without waiting for AD) and keeps consuming data as it arrives, like a leaky bucket.
- Verified: a single **1400-row / 67 KB mono** job printed continuously, with no seams.
  The vendor app prints ~1.5 m images the same way.
- **Open issue, 4-bit:** a 1000-row / 187 KB 4-bit job stopped cleanly at row ~280 (~54 KB) and sent AA
  while data was still being sent. Likely cause: once printing starts the BLE link slows, and 4-bit needs ~4× more data per mm
  (~3.8 KB/s at print speed), so the buffer runs dry and the firmware ends the job. Mono only needs ~2.1 KB/s.
  4-bit jobs ≤ ~48 KB (~250 rows, ~31 mm) are safe.
- **Rejected approach:** splitting into ≤48 KB jobs joined by a 14 mm retract. Joins were aligned when tuned,
  but a longer run left gaps and crinkled the paper. Don't do this.
- AA payload is 3 bytes of unknown meaning (e.g. `00 e8 e8`, `fd ed a0`).

### Minimum length
Docs claim 90 rows. A 30-row unpadded mono job printed fine, so we don't pad.

### Heat (A2)
Mono heat ladder: darkness rises from 0x10 up to **0x5D (93)**. 0x64 (100), 0x80, 0xB0, 0xE0 and 0xFF are
no darker. Print time also stops increasing at ~0x5D. The scale is probably 0–100 with a firmware cap near 93.
At 0x5D, 1-dot checkerboards and 1px lines stay crisp (no heat bleed), so **0x5D is the best default**.
0x30–0x5D gives visible gradations for a lighter-print option.
In 4-bit mode, levels **5–F are distinguishable** at 0x5D; 0–4 are near-white.

### Scale
Print resolution verified with a printed ruler: **8 rows per mm** (203 dpi) along the paper.

### Timing
| Job | Print time (flush → AA) |
|---|---|
| 90 rows mono | 2.9 s (heat 0x10) … 3.9 s (≥0x5D) |
| 266 rows mono | 7.8 s |
| 170 rows 4-bit | 10.1 s |
| 266 rows 4-bit | 11.1–11.9 s |
Plus transfer time at ~5 KB/s.
