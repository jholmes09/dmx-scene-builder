# DMX Scene Builder

DMX Scene Builder is a small app that runs on a Mac and controls the lights on a
parade float, one float at a time. You open it from an iPad in Safari. It
talks to Anolis Calumma light fixtures through Anolis E-Box Remote boxes over
a plain network cable, so you can pick colors and brightness for each float,
save the look, and (for most fixtures) store that look inside the fixtures
themselves so the float can run with nothing connected at the parade.

## Quick start

1. Double-click **DMX Scene Builder.command** in this folder.
2. A window opens and prints an address, something like
   `http://jeffs-macbook.local:8080`.
3. On the iPad, open that address in Safari.

See `web/guide.html` (the field guide, below) for the full setup, wiring, and
on-site checklist.

## Try it without hardware

No box or fixtures on hand? Run the app in simulator mode instead:

```
python3 -m scenebuilder --sim
```

Then, in the app, open **Setup**. It shows the simulator running. To connect
a float to it, go to that float's Patch tab and set a box's IP address to
`127.0.0.1`, then press **Find boxes on network**.

## Run the tests

```
python3 -m unittest discover -s tests -t .
```

## Layout of the code

- `scenebuilder/artnet.py`: builds and reads the Art-Net network packets (the
  protocol the E-Box speaks).
- `scenebuilder/rdm.py`: RDM messages, used to discover and address fixtures
  remotely.
- `scenebuilder/fixtures.py`: knows the Calumma DMX modes and turns a look
  (brightness, color, color temperature) into DMX channel values.
- `scenebuilder/node.py`: finds E-Box units on the network and talks to them.
- `scenebuilder/engine.py`: sends the live look to the boxes, at a steady rate.
- `scenebuilder/simulator.py`: a fake E-Box and fixtures, for testing without
  hardware.
- `scenebuilder/server.py`: the web server: serves the app and answers its
  requests.
- `scenebuilder/store.py`: saves and loads project data on the Mac.
- `scenebuilder/patchsheet.py`: builds the printable patch sheet.
- `web/`: the iPad-facing app itself (HTML, CSS, JavaScript).
- `data/demo_project.json`: made-up demo floats loaded on first run, built by
  `tools/make_demo.py`. Real show data is never committed: load it with
  Setup > Import project, or `python3 -m scenebuilder --seed path/to/project.json`.
- `docs/PLAN.md`: the build plan this app was written from, with more detail
  on the hardware and the reasoning behind each decision.

For the field guide (what to bring, wiring, the box menu, and the daily
workflow), see **`web/guide.html`**. Open it in a browser, or from inside the
app under Setup.

## Features

- Save a stand-alone look two ways: by RDM (works in any mode, including
  tunable white), or with Mode 7 (the original method, output held at zero
  while fixtures re-address).
- Saved colors: save a color (and optionally brightness) from the Look tab,
  then apply it to any selected lights on any float.
- Multi-select lights in the Look tab, so several lights can be set together.
- A "Parade" tab for floats that run on live DMX from show control
  instead of stand-alone.
- A per-fixture Settings button in Scan results, for the maker's own
  settings (like Terminator active).

## Known unknowns

- Whether the E-Box actually answers RDM requests sent over Art-Net has not
  been tested on real hardware yet. If it doesn't, addressing falls back to
  REAP (the box's own web page) or Robe Toolkit.
- The RDM save-by-RDM path relies on a manufacturer setting named "Init
  position LEDs" (documented in the E-Box manual v1.6). Its availability on
  current firmware is unverified.
- Art-Net universe numbering (Net / Sub-Net / Universe) is assumed to match
  what the box's own menu shows. Confirm this on-site with a real box.

## Show data stays local

This repository holds only the app and made-up demo floats. Real floats, fixture
schedules and client names live on the show Mac and never go into git.
