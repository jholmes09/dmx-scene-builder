# DMX Scene Builder — build plan (v1, 2026-09-27)

A Mac-hosted control surface, used from an iPad in Safari, for setting static
looks on Anolis Calumma fixtures that hang off Anolis E-Box Remote boxes, one
parade float at a time, then saving each look into the fixtures so every float
runs stand-alone.

Deadline: working and tested by **2026-09-28 ~16:00 ET** (Jeff travels to the
fabrication shop after that).

## 1. What we know (from the E-Box Remote manual v2.5 and the Calumma DMX chart v1.3)

- The E-Box Remote has **Ethernet IN/OUT** and accepts **Art-Net, sACN, MA-Net,
  DMX, RDM**. Menu: `Personality > DMX Input = Ethernet`, `Ethernet mode = Artnet`,
  Art-Net Net/Sub-Net/Universe settable.
- Default IP is **2.x.x.x /8, derived from the MAC**. A custom IP can be set from
  the box's menu.
- The box has a built-in web page, **REAP**, at `http://<box-ip>` (factory login
  `robe` / `2479`, published in the manual). REAP shows the box status, can
  **discover connected modules, change each module's DMX address and DMX preset,
  identify (flash) a module**, and toggle "Output Data".
- Calumma XS modules usually run in **Pass-Through mode** (parallel). In that
  mode each module is addressed individually (via RDM / REAP).
- Calumma DMX modes (chart v1.3):
  | Mode | Ch | Layout |
  |---|---|---|
  | 1 | 4 | R G B W (8-bit) |
  | 2 | 3 | R G B |
  | 3 | 12 | R Rf G Gf B Bf W Wf GreenCorr CTC Dim Dimf |
  | 4 | 3 | GreenCorr CTC Dim (white only) |
  | 5 | 6 | R G B W Dim Dimf |
  | 6 | 8 | R G B W GreenCorr CTC Dim Dimf |
  | 7 | 15 | Special R Rf G Gf B Bf W Wf GreenCorr CTC VCW Shutter Dim Dimf |
  | 11 | 3 | TW: White 2700–6500K, Dim, Dimf |
  | 12 | 4 | TW: WW, CW, Dim, Dimf |
  | 13 | 2 | PW: Dim, Dimf |
  Factory default: Mode 1 for RGBW/RGBA, Mode 11 for TW/PW.
- **Saving a look into the fixtures (stand-alone):** put the module in **Mode 7**,
  send the look, hold channel 1 (Special functions) at **1–2 for at least 3 s**
  ("Save current DMX values to fixture as initial DMX values"), then set the
  box's **Output Data = Disabled** (menu or REAP, then "Reset now") and power
  cycle. The modules then wake up in the saved look.
- ⚠️ **Gap:** Mode 7 and the "save" channel are only documented for the
  RGBW/RGBA variants. The TW modes (11/12) have no Special-functions channel.
  Whether a TW module accepts Mode 7, or saves its look some other way, is
  **unknown until tested on real hardware**. Fallback for TW floats:
  `DMX Hold` on the box (keeps the last look while data drops, not across a
  power cycle), or a small DMX/Art-Net playback device on the float.
- ⚠️ **Gap:** the manual never says whether the box answers **RDM over Art-Net
  (ArtRdm)**. REAP definitely does discovery and addressing. So the app tries
  ArtRdm, and if the box doesn't answer, the addressing step uses REAP, which
  the app links to.

Jeff's answers: Ethernet straight into the box (or Mac in between); **no RDM
USB adapter available**; **the box/fixtures store the look for the parade**; a
**Mac will be on-site**.

## 2. Decision: Mac-hosted web app, iPad as the remote

- **Why not a native iPad app:** Jeff has never shipped one, side-loaded apps
  expire after 7 days without a paid developer account, and nothing is gained:
  the Mac must be on-site anyway and all the hardware talk is plain network
  traffic. A native app stays an option for next season.
- **Why not an off-the-shelf app:** REAP (free, built in) already handles
  addressing and identifying, and general Art-Net apps (e.g. Luminair) can
  send colour. None of them know the floats, the fixture schedule, the
  Calumma modes, or the save-into-fixture routine. The custom app adds those,
  and REAP stays as the backup for addressing.
- **Stack:** Python 3 standard library only (the Mac already has Python 3.14),
  so there's nothing to install. A single process runs:
  - an HTTP server (static web app + JSON API + a Server-Sent Events stream),
  - an **Art-Net engine** (ArtPoll discovery, ArtDmx output at 30 fps per
    universe, ArtTodRequest / ArtTodData / ArtRdm for RDM),
  - a **simulator**: a fake E-Box Remote with virtual Calumma modules, so
    everything can be tested with no hardware.
- **Front end:** one HTML page, vanilla JS, no build step. It can be added to the
  iPad Home Screen so it opens full-screen like an app. JHP branding: near-black
  `#070605`, antique gold `#D4A84A`, champagne `#F6E3AE`, showbill serif.
- **Launch:** double-click `DMX Scene Builder.command`. It starts the server, prints
  the iPad address, and opens the page on the Mac.

## 3. Network setup in the field (recommended)

```
 iPad ──Wi-Fi──► travel router ◄──Wi-Fi── Mac ──Ethernet (USB-C adapter)──► E-Box Remote
```
- Mac **Ethernet** port: manual IP `2.0.0.10`, mask `255.0.0.0` (matches the box's
  factory 2.x.x.x address, per the manual). The app shows a one-tap checklist.
- Mac **Wi-Fi** joins the travel router. The iPad joins the same router and opens
  `http://<mac-wifi-ip>:8080`.
- Alternative (Jeff's first option): box → router by cable. Then either give the
  box a custom IP in the router's range (box menu), or give the router a
  2.x.x.x LAN. The app works either way; it only needs the box's IP.
- Sidecar also works: the iPad becomes a Mac screen, with no networking at all.

## 4. Features (v1 scope, in priority order)

1. **Floats.** A list of floats (imported from the show's fixture schedule, kept local)
   → tap one to open its profile: box IP/universe, fixtures with ID, label,
   type (TW / RGBW / PW), DMX mode, and address.
2. **Live control.** Per-fixture and group (multi-select / "all TW" / "all RGB")
   intensity, plus:
   - TW: colour temperature 2700–6500K (Mode 11) or warm/cool mix (Mode 12).
   - RGBW: colour picker (hue/saturation) + white level, plus a "white only"
     CCT slider (modes 3/4/6/7 via the CTC channel).
   - Quick swatches, a "flash / identify" button, blackout, and "home" (all 100% at 3000K).
3. **Discover (RDM).** "Scan box": ArtPoll → ArtTodRequest → per-UID DEVICE_INFO,
   label, address, mode. Shows what's actually plugged in, merged against the
   schedule ("expected 6, found 5", new/unknown modules flagged). Set address,
   mode and label per module, and identify. If the box doesn't answer RDM over
   Art-Net: a clear message plus an "Open REAP" button.
4. **Looks.** Save / recall / rename named looks per float. The current look is
   autosaved.
5. **Save to fixtures (stand-alone).** A guided wizard: checks that the modules
   are in Mode 7 (offers to switch them by RDM or tells you to do it in REAP),
   re-sends the look in Mode 7, holds the save value for 4 s, then walks you
   through setting Output Data → Disabled and the power-cycle check. TW modules
   get flagged with the gap described in §1.
6. **Patch sheet export.** Per-float CSV/printable page of fixture → address → mode.
7. **Simulator mode.** A toggle to run against a virtual box, with a live
   "virtual float" view showing the colour each fixture would output. Used for
   testing and training.

Out of scope for v1: cueing/timing, effects, sACN RDM (not a thing), a native app,
multi-box synchronisation.

## 5. Safety and robustness

- The app **only sends data to the box IP(s) you choose** (unicast), never
  broadcast ArtDmx, so it can't fight another console on the network.
- A big **"Release"** button stops all output (the box then falls back to DMX Hold
  or its saved look).
- All data is saved as JSON in `~/Library/Application Support/DMXSceneBuilder/`, with
  a timestamped backup on every change. Profiles can be exported/imported.
- The server binds to all interfaces on port 8080 with no login (field LAN
  only). That's documented.

## 6. Test plan

- **Unit tests** (`python3 -m unittest`): Art-Net packet encode/decode (ArtPoll,
  ArtPollReply, ArtDmx, ArtTodRequest, ArtTodData, ArtRdm), RDM message
  checksum and parameter encoding, and the fixture mode → DMX channel mapping
  for every Calumma mode, including 16-bit fine channels and CTC value mapping.
- **Integration tests:** start the server and the simulator on localhost,
  run a scan over the HTTP API, set address/mode, send a look, and verify the
  simulator received the right DMX bytes. Run the save wizard and verify the
  simulator saw ch1 = 1–2 held ≥ 3 s.
- **UI check** in the browser pane at iPad size (1024×768 and 820×1180),
  light/dark not needed (the brand is dark).
- **On-site smoke test** (Jeff, 10 minutes, printed checklist in README):
  ping the box → Scan → Identify one fixture → change colour → save look.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Box doesn't do RDM over Art-Net | REAP button; manual fixture entry; schedule pre-loaded |
| Art-Net universe numbering off by one (box menu says 1–12) | Universe picker shows both conventions; "find my universe" sweep sends a flash to each universe in turn |
| TW modules can't save a stand-alone look | Flagged in the UI; test on day one; fallbacks listed |
| iPad can't reach the Mac | Sidecar fallback; the launcher prints every Mac IP |
| Python version quirks | stdlib only; tested on the Mac's Python 3.14 plus /usr/bin/python3 (3.9) |
