# On Air

**Hermes inside your OBS.** Ask for a scene and it exists. Ask what the error on screen is and the
answer appears on your stream. Mark the good moments and get the highlight reel, the YouTube
chapters and a title when you stop.

For anyone who records or streams with [OBS Studio](https://obsproject.com): demos, tutorials,
coding streams, gameplay, talks.

```
You:     make me a synthwave "starting soon" scene with a 5 minute countdown
Hermes:  Scene "Starting Soon" is ready. Want it on air?

You:     what's this error?
Hermes:  (card on stream)  Missing import: `requests` is not installed in this venv.

You:     /live stop
Hermes:  Session ended with 6 marks.
         Highlight reel (draft, 6 clips): …/reel-20261111-142210.mp4
         Chapters (paste into YouTube):
         0:00 Intro
         2:14 Null pointer in the upload handler
         5:40 Fixed
```

## What it does

| | |
|---|---|
| **Control OBS by asking** | Scenes, sources, recording, streaming, replay buffer, virtual camera. "Add my webcam bottom right", "hide the chat", "switch to the camera scene". |
| **Design scenes** | One request makes a finished animated scene: *starting soon* (with countdown), *be right back*, *ending*, *intermission*, or any title, in six animated styles (aurora, synthwave, matrix, starfield, confetti, bounce). For anything else Hermes writes the HTML and CSS itself. |
| **GIFs and effects** | Pop a GIF on screen for a few seconds, rain confetti, emoji or fireworks. |
| **Overlay** | One transparent browser source. Hermes shows short answer cards, toasts and chapter titles on it. Everything hides itself on a timer, so nothing can get stuck on screen. |
| **Ask about the screen** | "What's this error?" takes a screenshot through OBS, reads it with your own model, and shows a two-line answer on the overlay. |
| **Marks** | `/mark`, a button, ⌘⇧M, your phone (Telegram), or OBS's own *Save Replay* hotkey. Each mark saves a clip, a thumbnail, a note and the timestamp. A build or test that fails and then passes is marked automatically, as a pair. |
| **Wrap-up** | When you stop: a highlight reel of the marked clips with chapter cards, chapter timestamps for the full recording, and a draft title, description and show notes. Always a draft; the recording is never modified. |
| **Studio in Hermes Desktop** | A live preview of OBS, scene switcher, source toggles, transport buttons, the scene designer and the timeline of marks. Untick the marks you do not want and rebuild the reel. |

## Install

Requires Hermes 0.23.1 or newer and OBS Studio 28 or newer (30+ recommended).

```bash
hermes plugins install onair      # from the catalog
hermes plugins enable onair
```

Or from this repository:

```bash
hermes plugins install https://github.com/perfect7613/hermes-onair
hermes plugins enable onair
```

No extra Python packages are installed: the plugin uses `websockets` and `Pillow`, which ship
with Hermes.

## Set up (about a minute)

1. In OBS: **Tools → WebSocket Server Settings → Enable WebSocket server**.
2. If authentication is on, click **Show Connect Info**, copy the password, and paste it into
   Hermes **Settings → Plugins → On Air → OBS password**.
3. In Hermes: `/live setup`

`/live setup` adds the overlay to your scenes and switches on the replay buffer for you.
`/live doctor` checks everything and tells you how to fix each gap:

```
✓ OBS connection: OBS 33.0.1, obs-websocket 5.6.3
✓ Browser sources: available
✓ Overlay: in scene 'Main'
✗ Replay buffer: switched off in OBS
    Fix: Run /live setup to switch it on.
✓ Recording folder: /Users/you/Movies
✓ FFmpeg: found, highlight reels can be rendered
```

## Use it

Talk to Hermes normally. Some things to try:

- "What scenes do I have?" · "Switch to the camera scene"
- "Make a be-right-back scene in the matrix style"
- "Make a scene with a slowly rotating neon grid and my channel name in the middle" (Hermes writes it)
- "Add `~/Pictures/logo.png` to the top-left corner"
- "Put this GIF on screen: https://…/party.gif"
- "Start a session called *Fixing the upload bug* and record"
- "What's this error?" · "Mark that" · "Confetti!"
- "Stop the session"

Or type commands. They work from the terminal, Hermes Desktop and messaging apps:

| Command | |
|---|---|
| `/live` | What OBS and the session are doing |
| `/live setup` · `/live doctor` | Set up · check the setup |
| `/live start [title] [--record]` · `/live stop` | Start a session · end it and get the wrap-up |
| `/mark [note]` | Save this moment |
| `/live scene [name]` | List scenes, or switch (partial names work) |
| `/live record start\|stop\|pause\|resume` | Recording |
| `/live stream start\|stop` | Go live · end the stream |
| `/live replay start\|stop\|save` | Replay buffer |
| `/live design <kind> [style] [title]` | Create an animated scene |
| `/live card <text>` · `/live chapter <title>` · `/live hide` | Overlay |
| `/live celebrate [fireworks\|<emoji>]` · `/live gif <link or path>` | Effects and stickers |
| `/live ask <question>` · `/live shot` | Ask about the screen · save a screenshot |
| `/live reel` | Rebuild the highlight reel |
| `/live pause` · `/live resume` | Stop / allow Hermes looking at the screen |

In Hermes Desktop, open **On Air** in the sidebar (or ⌘K → *On Air: Open studio*).

### Marking with an OBS hotkey

In OBS **Settings → Hotkeys**, bind **Save Replay**. While a session is running, every press
becomes a mark, even when Hermes is not in focus.

## Safety

- **Hermes asks before anything your audience would see or that cannot be resumed:** going live,
  ending a stream, stopping a recording, deleting a scene or source, and any scene or source
  change while you are streaming. This uses Hermes's own approval prompt. Commands you type
  yourself act immediately. You can turn the prompts off in the plugin settings.
- **`/live stream` alone never goes live.** It needs `start`.
- **Screen capture is yours to control.** `/live pause` (or the checkbox in the studio page)
  stops Hermes taking screenshots. *Sources Hermes may look at* in the settings restricts
  capture to the scenes or sources you name.
- **Marks are saved first.** If OBS is closed, the replay buffer is off or FFmpeg is missing, the
  mark is still recorded and can be cut from the recording later.

## What this plugin does on your machine

Disclosure, as the catalog requires:

- **Network:** connects to OBS's WebSocket server on your machine (`127.0.0.1:4455` by default,
  configurable). Nothing connects until you first use the plugin. No other network calls, no
  telemetry, no update checks. Web links you give it (a GIF, a page) are loaded by OBS's browser
  source, not by the plugin.
- **Screenshots go to your model provider:** `live_ask`, `/live ask` and the wrap-up title use
  the model you configured in Hermes, through Hermes. Screenshots are only taken for those when
  you or the agent ask, and never while capture is paused. Mark thumbnails and the studio
  preview stay on your machine.
- **Credentials:** the OBS password is stored by Hermes as a secret (`ONAIR_OBS_PASSWORD`) and
  read through Hermes's secret store. The plugin does not read OBS's configuration files.
- **Changes to OBS:** creates scenes and sources you ask for, an `On Air Overlay` browser
  source, and, on `/live setup`, enables the replay buffer in your current OBS profile.
- **Files written:** only under `<HERMES_HOME>/plugin-data/onair/`: a SQLite database of
  sessions and marks, thumbnails, screenshots, overlay and scene HTML, highlight reels and
  `wrapup.md`. Replay clips and recordings are written by OBS to its own folder; the plugin
  reads them and never modifies or deletes them.
- **Subprocesses:** runs `ffmpeg` and `ffprobe` from your `PATH` with fixed argument lists (no
  shell) to render highlight reels.
- **Background work:** while a session is running, one connection to OBS stays open and
  reconnects if OBS restarts. It closes when the session stops or the plugin unloads.
- **Hooks:** `post_tool_call` reads the exit code of terminal commands during a session, for
  automatic marks. `pre_tool_call` asks for approval on the plugin's own public-facing tools.

## Settings

Hermes Desktop → **Settings → Plugins → On Air**, or `hermes config`:

| Setting | Default | |
|---|---|---|
| OBS password | | From OBS → Tools → WebSocket Server Settings |
| OBS host / port | `127.0.0.1` / `4455` | |
| Ask before public actions | on | The approval prompts described above |
| Automatic marks | on | Mark failing and then passing build/test commands |
| Clip length | 20 s | How much of each mark goes into the reel |
| Replay buffer length | 30 s | Used when setup switches the buffer on |
| Sources Hermes may look at | scene on air | Restrict screenshots to named scenes or sources |
| Accent colour | `#ffb000` | Overlay and chapter cards |

## Tools

Twelve tools, in the `onair` toolset: `live_status`, `live_scene`, `live_source`,
`live_design_scene`, `live_output`, `live_overlay`, `live_screenshot`, `live_ask`, `live_mark`,
`live_session`, `live_reel`, and `live_obs_request` (any obs-websocket request, for filters,
transitions, audio and whatever else you need). A bundled skill, `onair:director`, teaches the
agent when to mark and how to write cards.

## Not included, on purpose

- **Viewer chat and co-hosting.** Reading and answering live chat is a different product with
  different risks.
- **A video editor.** The reel is a first cut: clips and chapter cards, joined. For real editing,
  take the clips into an editor; the catalog has plugins for that.
- **Starting OBS for you**, and changing your stream key or service.

## Limitations

- The overlay is updated through obs-browser's event channel, which needs OBS's bundled browser
  source (included in the official builds for macOS, Windows and Linux).
- Chapter markers inside the recording file need OBS 30.2+ and the Hybrid MP4/MOV format. The
  chapter list in the wrap-up works with any format.
- Enabling the replay buffer from `/live setup` is done through the OBS profile and may need
  outputs to be idle; if OBS refuses, the doctor says where to tick the box.
- Developed and tested on macOS with OBS 33. Windows and Linux use the same protocol and the
  plugin picks platform-specific source types at run time, but they are not yet tested by hand.

## Development

```bash
git clone https://github.com/perfect7613/hermes-onair && cd hermes-onair
ln -s "$PWD" ~/.hermes/plugins/onair          # live-linked: Desktop hot-reloads desktop/plugin.js
python -m unittest discover -s tests -t .      # needs websockets and Pillow; ffmpeg for the render test
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

The tests run the whole plugin (application, tools, commands, Desktop RPC) against an in-memory
OBS that speaks the real protocol, and render a real reel with FFmpeg. See
[docs/architecture.md](docs/architecture.md) for how the code is laid out.

## Licence

MIT
