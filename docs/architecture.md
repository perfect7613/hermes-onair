# Architecture

Three skins over one application, over a Hermes-free core.

## Layers

Dependencies point downwards only. Nothing in `core/` imports Hermes.

```mermaid
flowchart TB
    subgraph surfaces["Surfaces: thin skins, no logic"]
        direction LR
        tools["tools.py<br/>12 agent tools"]
        commands["commands.py<br/>/live, /mark"]
        rpc["rpc.py<br/>plugin.onair.* RPC"]
        desktop["desktop/plugin.js<br/>Studio page"]
        hooks["__init__.py<br/>pre/post tool hooks"]
    end

    app["core/app.py<br/><b>App</b>: every operation, once"]

    subgraph domain["Domain"]
        direction LR
        director["director.py<br/>start, mark, stop"]
        stage["stage.py<br/>overlay, designed and media scenes"]
        studio["studio.py<br/>scenes, sources, outputs"]
        reel["reel.py<br/>plan, chapters, render"]
        policy["guard.py, automark.py, doctor.py<br/>pure policy and checks"]
    end

    subgraph infra["Infrastructure"]
        direction LR
        obs["obs.py<br/>obs-websocket v5 client"]
        store["store.py<br/>SQLite: sessions, marks"]
    end

    host["hermes_host.py<br/>the only Hermes import:<br/>settings, secret, storage, model, events"]

    desktop -- "host.request" --> rpc
    tools --> app
    commands --> app
    rpc --> app
    hooks --> app
    app --> director & stage & studio & reel & policy
    director --> stage & studio & store
    stage --> studio
    studio --> obs
    app -. "Host protocol" .-> host

    obs == "WebSocket" ==> OBS[("OBS Studio")]
    reel == "subprocess" ==> FF[("ffmpeg / ffprobe")]
    store == "file" ==> DB[("plugin-data/onair/data.db")]
```

## Processes

Hermes runs plugin code in more than one process. Each has its own lazy OBS connection; the
database is what they share.

```mermaid
flowchart LR
    subgraph agent["Agent process"]
        A1["tools + hooks"] --> A2["App"]
    end
    subgraph gateway["Gateway / plugin host"]
        G1["slash commands + RPC"] --> G2["App"]
    end
    D["Hermes Desktop<br/>Studio page"] -- "plugin.onair.* RPC" --> G1
    G2 -. "plugin.onair.changed / reel events" .-> D
    A2 <--> DB[("SQLite<br/>single source of truth")]
    G2 <--> DB
    A2 <-- "WebSocket" --> OBS[("OBS Studio")]
    G2 <-- "WebSocket" --> OBS
    OBS -- "browser source<br/>emit_event" --> OV["overlay.html"]
```

## A mark, end to end

The mark is durable before OBS is asked for anything. The replay file is paired with its mark
by `claim_clip`, whichever process hears about the file first.

```mermaid
sequenceDiagram
    autonumber
    participant U as User or agent
    participant D as Director
    participant S as Store
    participant O as OBS
    participant V as Overlay

    U->>D: mark, note The fix lands
    D->>O: status, recording position and replay buffer
    D->>S: add_mark, clip_state pending
    Note over D,S: The mark now exists.<br/>Everything below is best effort.
    D->>O: GetSourceScreenshot
    D->>S: update_mark, thumb_path
    D->>O: SaveReplayBuffer
    par poll in the calling process
        D->>O: GetLastReplayBufferReplay
        D->>S: claim_clip, from_hotkey false
    and event in every connected process
        O-->>D: ReplayBufferSaved
        D->>S: claim_clip, from_hotkey true
    end
    Note over S: One IMMEDIATE transaction.<br/>Path already owned: no-op.<br/>Else the oldest pending mark gets it.<br/>Else, for a hotkey save, a new mark.
    D->>O: CreateRecordChapter
    D->>V: toast Marked
    D-->>U: mark with clip, thumbnail, position
```

## Session lifecycle

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Live: /live start, or the first /mark
    Live --> Live: mark, OBS hotkey, fail/fix auto-mark
    Live --> WrapUp: /live stop (recording stopped, stream left alone)
    WrapUp --> Idle: chapters, title, description, and the reel when FFmpeg and footage exist
    Idle --> Idle: /live reel (rebuild from edited marks)
    note right of Live
        OBS connection is held and
        reconnects with backoff
    end note
```

## OBS connection

```mermaid
stateDiagram-v2
    [*] --> Disconnected
    Disconnected --> Connecting: first call, never at plugin load
    Connecting --> Connected: Hello, Identify, Identified
    Connecting --> Disconnected: ObsUnavailable or ObsAuthError
    Connected --> Disconnected: socket lost, waiting callers fail at once
    Disconnected --> Connecting: next call, or backoff 1s to 30s while held
    Connected --> Closed: close on unload
    Disconnected --> Closed: close on unload
    Closed --> [*]
```

## Modules

| Module | Interface | What it hides |
|---|---|---|
| `core/obs.py` | `call`, `call_batch`, `subscribe`, `hold`, `close` | Lazy connect, auth, request/response correlation, reader and dispatcher threads, one retry when a request never left, reconnect with backoff |
| `core/studio.py` | scenes, sources, outputs, `screenshot`, `status` | obs-websocket request names, per-platform input kinds, bounds-based transforms, idempotency (add twice = update) |
| `core/stage.py` | overlay verbs, `design_scene`, `play` | HTML assets, publishing them and imported media to the data directory, which OBS source plays which file type, the obs-browser event channel |
| `core/store.py` | sessions, marks, `claim_clip` | Schema, transactions, pairing a saved replay with its mark exactly once across processes |
| `core/director.py` | `start`, `mark`, `stop` | Mark-first ordering, best-effort decoration, OBS event handling |
| `core/automark.py` | `observe` | Fail-then-fix pairing, bounded memory |
| `core/reel.py` | `plan_reel`, `youtube_chapters`, `draft_wrapup`, `ReelBuilder.build` | FFmpeg argument lists, normalising clips, chapter cards |
| `core/guard.py` | `review`, `safety` | The approval policy: audience prompts (can be switched off) and safety prompts (always asked) |
| `core/weburl.py` | `public_web_url` | Refuses links that are not public http(s) addresses before OBS's browser gets them |
| `core/app.py` | one method per user-visible operation | Composition, capture privacy, wrap-up, background work |

## Seams

Each has two real adapters, production and test:

- **Transport** (`obs.Connector`): `websockets` in production, `tests/fake_obs.FakeObs` in tests.
  The fake keeps real OBS state, so tests assert on outcomes, not on which requests were sent.
- **Host** (`app.Host`): `hermes_host.HermesHost` in production, `FakeHost` in tests.
- **FFmpeg runner** (`reel.Runner`): `subprocess` in production (and in `test_reel_render.py`),
  a recorder in the application tests.

## Decisions

- **SQLite is the only source of truth.** Tools run in the agent process, slash commands and the
  Desktop RPC in the gateway or plugin host. Each process has its own OBS connection; all share
  the database. `claim_clip` runs in one `BEGIN IMMEDIATE` transaction so a replay file reported
  by several processes still produces one mark.
- **A mark is written before anything else is attempted.** Clip, thumbnail, recording chapter
  and toast are decorations; any of them may fail.
- **Tools are nouns with an `action`**, not one tool per OBS request. Twelve tools, plus
  `live_obs_request` as the escape hatch, keeps the model's choice small without capping what it
  can do.
- **The overlay has no server and no polling.** Updates arrive as obs-browser `emit_event`
  custom events. Every element hides itself on a timer, so a lost update cannot strand anything
  on screen.
- **Assets are copied to the plugin data directory**, because `hermes plugins update` replaces
  the install directory and OBS remembers file paths.
- **Media from other tools comes in as a file path, nothing more.** The plugin does not call
  Hermes's image, video or speech tools; the agent does, and hands over the file. That keeps the
  plugin working with any tool that writes a file, including ones that do not exist yet. Files
  are copied in, because generators write to caches that get cleaned.
- **One-shot sounds and clips need no timer.** They are non-looping media sources that clear
  themselves when the file ends, so nothing has to come back later to remove them.
- **Chapter cards are drawn with Pillow**, not FFmpeg's `drawtext`, which many FFmpeg builds
  (Homebrew's included) are compiled without.
- **The Desktop half talks to the plugin over `ctx.register_rpc`**, not a separate
  `dashboard/plugin_api.py`. That file is imported outside the plugin package, which would mean
  a second copy of the application and its OBS connection.
