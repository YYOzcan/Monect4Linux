# Monect PC Remote — LAN protocol notes (receiver 7.9.3)

Notes reconstructed by static analysis of `MonectServerService.exe` and
`channel.dll` (Ghidra 12.2 via REA 6.10.1) for interoperability. Byte orders:
"BE" = network order. All offsets are into the UDP payload / data-channel
message.

## Transport overview

| Port / channel        | Purpose                                                    |
|-----------------------|------------------------------------------------------------|
| UDP 28451             | discovery, connect handshake, WebRTC signaling, ping       |
| UDP 28458             | PC → phone notifications (e.g. gamepad rumble), optional   |
| WebRTC DataChannel(s) | everything after the handshake (input, capabilities, …)    |
| UDP 27679             | internet relay path (AES‑GCM, cloud account) — not used    |
| Bluetooth RFCOMM      | alternative transport — not implemented                    |

The phone sends from any port; the PC answers either to `ip:28451`
or to the exact source address depending on the request's mode byte.

## UDP 28451

### Ping
A datagram whose first byte is `0x7F` is answered with the single byte `0x7F`;
the byte is then stripped and the remainder (if any) is processed normally.

### 0x10 — discovery request (≥10 bytes)
`[0x10, mode, clientId(8) …]`

Reply:
```
0x05
secMode              u8   0 = allow anyone, 3 = blocked, 4 = allow-list …
nameLen              u8   byte length of the name
name                 UTF‑16LE host name
version              BE u32 = 0x2B
hwid                 32 ASCII chars (uppercase hex GUID, persistent)
```
mode 1 → reply to `src_ip:28451`; mode 4 → reply to the source address.

### 0x12 — connect request
```
0x12, mode(4 over Wi‑Fi), clientId(8),
n1, deviceName (UTF‑16LE, n1 bytes),
n2, appVersion (ASCII, e.g. "7.x.y"),
n3, extra (ASCII)
```
Reply `[0x06]` = accepted, `[0x00]` = rejected (sent to the source address).
Clients with version ≥ 7.3.3 are flagged as "new protocol".

### 0x35 — connect via relay id (not needed on LAN)

### 0x33 — WebRTC signaling tunnel
Payload after the `0x33` byte is a binary signaling message:

| Type | Layout                                                                    |
|------|---------------------------------------------------------------------------|
| 0x02 | `msgId(4) sdpType(1: 0 offer,1 pranswer,2 answer) len(BE u32) sdp`        |
| 0x03 | `msgId(4) mLineIndex(BE u32) midLen(BE u32) mid candLen(BE u32) cand`     |
| 0x04 | `msgId(4)` — acknowledgement of a 0x02/0x03 with that id                  |
| 0x05 | close / bye                                                               |

Every 0x02/0x03 is acknowledged with 0x04 and de-duplicated by msgId;
senders retransmit until acknowledged. On the LAN the phone is the
offerer; the PC answers.

## DataChannel messages
Messages are raw; byte 0 is the category.

### Category 0x00 — input (byte 1 = sub-command)
| Sub  | Payload                                                                 |
|------|-------------------------------------------------------------------------|
| 0x00 | mouse boot report `buttons, dx(i8), dy(i8), wheel(i8)`; wheel×60 = WHEEL_DELTA units |
| 0x01 | keyboard boot report `mods, 0, k1..k6` (HID usage ids)                  |
| 0x03 | Xbox 360 report: `pad, 0, XUSB_REPORT(12)` (buttons u16, LT, RT, LX, LY, RX, RY) |
| 0x04 | DirectInput report: `pad, 0, pov, buttons u32, 8× i16 axes`             |
| 0x05 | gamepad management; `0x00 count (type,index)*` plug (2 dinput, 3 x360, 4 dsu) → reply `[0x04] + 256` status bytes (all zero = ok) |
| 0x06 | paste text: `BE u32 totalLen`, UTF‑16LE text (may be fragmented)        |
| 0x09 | consumer keys bitmask (2 bytes): b0 mute, b1 vol+, b2 vol−, b3 play/pause, b4 stop, b5 prev, b6 next, … |
| 0x0D | single key by set‑1 scancode: `scancode, flags` (bit0 = E0 extended, bit1 = pressed) |
| 0x02/0x07/0x0B/0x0C | remote-desktop pointer (forwarded to the capture process) |

### Category 0x0A — capability query
Answer: `[0x14, 0x01] + JSON` such as
`{"touchpad":{"relativeMove":true,…},"keyboard":{"hidBootReport":true,…},…}`.

### Category 0x01 — remote desktop
Start: `01 00 monitor fps W(BE u32) H(BE u32) … codec@0x25 … flag@0x30` →
`01 01 codec flag hasAudio` (or `01 02` on failure). Stop `01 01` → `01 03|04`;
`01 02`/`01 03` pause/resume; `01 04` → `01 05 count {idx, primary, l, t, r, b}*` (BE i32);
`01 05 idx` switch monitor → `01 06|07 idx`.
Video: prefix **0x05** + seq framing (see `packetizer.py`): an encoded Annex‑B
access unit is sent whole as `seq 00 AU` when ≤0x3FFC bytes, otherwise as
FU-style chunks of 0x3FFB bytes with header `(AU[0]&0xFC)|0x1C, (AU[0]&0x1F)|S|E`.
Cursor (when not embedded): prefix 0x07 (v1) / 0x0D (v2):
`BE flags, f32 x, f32 y [, BE stdCursorId] [, BE hotX, BE hotY, PNG]`.
Pointer from phone: input sub 0x02 `x y` (BE u16, 0..65535 on the streamed monitor),
sub 0x0C `x y` (BE i32, 0..65535 on the virtual desktop), sub 0x07 multitouch
(count + 16-byte points), sub 0x0B pen (36 bytes).

### Category 0x02 — audio
`02 00` → `02 00` + BE {0xFFFE, ch=2, rate, rate*bits/4, bits/4, bits=32, 22, latencyMs}.
`02 01|05|06` start PCM|ADPCM|Opus → `02 03` ok / `02 04` busy / `02 01` error.
Packets: prefix 0x06 PCM (s16le stereo), 0x0C ADPCM, 0x0E Opus, all seq-framed.
`02 02` stop → `02 02|05`; `02 03` → `02 06 vol`; `02 04 BE u32` set volume.
`02 07` PC-mic format, `02 08` start PC mic (Opus, prefix 0x0F) → `02 0B`, `02 09` stop.

### Category 0x03 — cameras
`03 00 … W H` PC webcam → phone (Media Foundation) → `03 00|01|02|04`; `03 01` stop.
`03 02 {json}` create phone→PC virtual camera → `03 05 BE u64 handle` / `03 07`;
`03 03`/`03 04` → `03 08 [json]`; `03 05 handle` close. Frames arrive as a WebRTC video track.

### Category 0x04 — auth
`04 02 role BE len name(UTF‑16)` hello → `09 00 01 BE u32 0x2B` accepted,
`09 01 00` password required, `09 00 00` waiting for confirmation, `09 00 02` rejected.
`04 01 BE 0x100 hash[256]` password → accepted or `09 00 05` (5 tries, then 30 s lock).

### Category 0x06 — file explorer (replies start with 0x0B)
`06 00 xx xx path` list → `0B 00 BE count` + 548-byte entries
`{type, attrs, ctime, atime, mtime, sizeHi, sizeLo, name[260] UTF‑16}` (BE u32s);
root `\` lists drives (type 1) and special folders.
`06 01 xx xx path` download → `0B 04 flag id(8) 16B size(8) nameLen name len data`,
then `0B 04 01 id len data`; cancel `06 04 id` → `0B 05 id`; resume `06 06 id offset path@0x13`.
`06 02` delete → `0B 03 ok path`; `06 05 from 00 00 xx to` rename → `0B 02 ok from`;
`06 03 path` open on PC; `06 07 BE count ids` special folders → `0B 06 n (id len path)*`.
Upload `06 08 00 md5[16] … size(8)@0x13 … name@0x1D, dir` → `0B 07 00 id …`;
data `06 08 01 id len data`; done → `0B 07 01|02 id`; cancel `06 08 02 id`.

### Category 0x0B — task manager (replies `15 kind flags json-chunk`)
kind 0 stats {cpu, cores, gpu, memTotal, memUsed, diskPercent, netRecvBps, netSendBps, uptimeSec};
1 processes {cores, processes:[{pid, name, desc, cpu, mem}]}; 2 kill {pid, op}; 3 details.

## Corrections from the Android client 9.1.4 (authoritative)
* Connect: app 9.x uses `0x35 clientId(8) peerId(8) n1 name(UTF-16 with BOM) n2 version n3 platform` → `06`.
* Input also arrives on an unordered `input` channel as `0D seq(BE u32) <category-0 message without the 0x00>`.
* Remote desktop start reply is `01 00 codec(0 H.264,1 HEVC) flag av1?`; stop `01 03`; `01 06` = keyframe request.
  Start request: [2] screen inches, [4:12] phone W/H, [19] fps, [21:37] decoder limits, [37] codec, [40:48] target W/H, [49] audio.
* Capabilities: task manager is `taskManager.webRtc`, game center `gameCenter.listGames/launchGame`.
* Gamepad mgmt: op0 `(type, maxIndex)`, op1 `(type, count)`; status `04 02 03 nX 02 01 nD 04 nDSU`; flags `04 03 a b c`.
* DSU motion: input sub 0x08, 68 bytes; bytes 4..67 == cemuhook pad data from the button bytes on.
* Virtual camera frames: `03 05 handle(8) H.264` on mbus (not a WebRTC track).
* Projector (phone → PC): `05 00` / `05 02` / `05 01 func(LE16) len(LE32) data` (1 video H.264, 5 audio fmt, 6 audio);
  PC answers `0A 00|02|03|05`.
* Game list: `10 00` + seq-framed {count, (01, id, title, n, games...)}; launch `07 01 id game` → `10 02 status`.
* Open/ShellExecute: `08 00 "open" file params` → `11 00 BE int` (>32 = success).
* Power widget: `0F action` over UDP 28451.
