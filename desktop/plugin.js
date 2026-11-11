/**
 * On Air: the studio page for Hermes Desktop.
 *
 * A live view of OBS inside Hermes: program preview, scenes, sources, transport buttons, the
 * overlay, the scene designer and the timeline of marks. Everything goes through the plugin's
 * own RPC methods (plugin.onair.*); this file holds no OBS logic and no credentials.
 *
 * Plain ESM, loaded uncompiled: createElement calls, no JSX. SDK imports only.
 */

import { Button, Codicon, ConfirmDialog, Input, PALETTE_AREA, KEYBINDS_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS, atom, cn, host, useValue } from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useRef, useState } from 'react'

const ID = 'onair'
const ROUTE = '/onair'
const STATE_POLL_MS = 4000      // fallback only: changes also arrive as plugin.onair.changed events
const PREVIEW_POLL_MS = 1200

const $state = atom(null)       // last answer of plugin.onair.state, or null before the first one
const $busy = atom('')          // name of the action in flight, to disable its button

async function rpc(name, params) {
  try {
    const result = await host.request(`plugin.${ID}.${name}`, params || {})
    if (result && result.ok === false) {
      host.notify({ kind: 'error', message: result.error || 'On Air could not do that', detail: result.hint })
    }
    return result || { ok: false }
  } catch (error) {
    // The Python half is not loaded (plugin disabled for this profile, or gateway restarting).
    return { ok: false, offline: true, error: String(error && error.message ? error.message : error) }
  }
}

let refreshing = null
function refresh() {
  if (!refreshing) {
    refreshing = rpc('state').then(result => { $state.set(result) }).finally(() => { refreshing = null })
  }
  return refreshing
}

async function act(label, name, params) {
  $busy.set(label)
  try {
    const result = await rpc(name, params)
    await refresh()
    return result
  } finally {
    $busy.set('')
  }
}

const markNow = note => act('mark', 'mark', { note: note || '' }).then(result => {
  if (result.ok) host.notify({ kind: 'info', message: result.clip ? 'Marked, clip saved' : 'Marked' })
  return result
})

// ---------------------------------------------------------------------------- small pieces

const muted = 'text-(--ui-text-tertiary)'
const panel = 'rounded-lg border border-(--ui-stroke-secondary) p-3'
const fieldClass = 'h-7 rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 text-xs text-(--ui-text-primary)'

function Section({ title, action, children }) {
  return h('section', { className: cn(panel, 'flex flex-col gap-2') },
    h('div', { className: 'flex items-center justify-between' },
      h('h3', { className: cn('text-[0.6875rem] font-semibold uppercase tracking-wider', muted) }, title), action),
    children)
}

function Pill({ on, tone, children }) {
  const color = !on ? cn('border-(--ui-stroke-secondary)', muted)
    : tone === 'danger' ? 'border-destructive bg-destructive text-white' : 'border-(--ui-accent) text-(--ui-accent)'
  return h('span', { className: cn('rounded-full border px-2 py-0.5 text-[0.6875rem] font-semibold', color) }, children)
}

function Picker({ value, onChange, options, label }) {
  return h('select', { 'aria-label': label, className: fieldClass, value, onChange: event => onChange(event.target.value) },
    options.map(option => h('option', { key: option, value: option }, option)))
}

// ---------------------------------------------------------------------------- preview

function Preview({ live, scene }) {
  const [image, setImage] = useState('')
  useEffect(() => {
    if (!live) { setImage(''); return undefined }
    let stopped = false
    const tick = async () => {
      if (document.hidden) return
      try {
        const result = await host.request(`plugin.${ID}.preview`, {})
        if (!stopped && result && result.ok) setImage(result.image)
      } catch (_) { /* the state poll reports a lost connection */ }
    }
    tick()
    const timer = setInterval(tick, PREVIEW_POLL_MS)
    return () => { stopped = true; clearInterval(timer) }
  }, [live, scene])

  return h('div', { className: 'relative aspect-video w-full overflow-hidden rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary)' },
    image
      ? h('img', { src: image, alt: `OBS program output: ${scene}`, className: 'h-full w-full object-contain' })
      : h('div', { className: cn('flex h-full items-center justify-center text-xs', muted) }, live ? 'Waiting for OBS…' : 'OBS is not connected'),
    scene ? h('div', { className: 'absolute left-2 top-2 rounded bg-(--ui-bg-quaternary) px-2 py-0.5 text-[0.6875rem] font-medium' }, scene) : null)
}

// ---------------------------------------------------------------------------- not connected

function Disconnected({ obs, offline }) {
  const [checks, setChecks] = useState(null)
  const runChecks = async () => { const result = await rpc('doctor'); if (result.ok) setChecks(result.checks) }
  if (offline) {
    return h('div', { className: cn(panel, 'max-w-xl text-sm') },
      h('div', { className: 'mb-1 font-medium' }, 'The On Air plugin is not running'),
      h('div', { className: muted }, 'Enable it for this profile: run "hermes plugins enable onair" in a terminal, then restart Hermes.'))
  }
  return h('div', { className: cn(panel, 'flex max-w-xl flex-col gap-3 text-sm') },
    h('div', { className: 'font-medium' }, obs.error || 'OBS is not connected'),
    obs.hint ? h('div', { className: muted }, obs.hint) : null,
    h('div', { className: 'flex gap-2' },
      h(Button, { size: 'sm', onClick: () => refresh() }, 'Try again'),
      h(Button, { size: 'sm', variant: 'outline', onClick: runChecks }, 'Check my setup')),
    checks ? h('ul', { className: 'flex flex-col gap-1 text-xs' }, checks.map(check =>
      h('li', { key: check.name },
        h('span', { className: check.ok ? 'text-(--ui-accent)' : 'text-destructive' }, check.ok ? '✓ ' : '✗ '),
        h('span', { className: 'font-medium' }, check.name), `: ${check.detail}`,
        !check.ok && check.fix ? h('div', { className: cn('pl-4', muted) }, check.fix) : null))) : null)
}

// ---------------------------------------------------------------------------- transport

function Transport({ obs, session }) {
  const busy = useValue($busy)
  const [confirm, setConfirm] = useState(null)   // 'start' | 'stop' | null, for the stream
  const [title, setTitle] = useState('')
  const rec = obs.recording.active, live = obs.streaming.active, replay = obs.replay_buffer.active
  const output = (action, name) => act(`${name}:${action}`, 'output', { action, output: name })

  return h('div', { className: 'flex flex-col gap-2' },
    h('div', { className: 'flex flex-wrap items-center gap-2' },
      h(Button, { disabled: busy === 'mark', onClick: () => markNow('') }, h(Codicon, { name: 'star-full' }), ' Mark'),
      h(Button, { variant: rec ? 'destructive' : 'secondary', onClick: () => output(rec ? 'stop' : 'start', 'record') },
        h(Codicon, { name: rec ? 'debug-stop' : 'record' }), rec ? ` Stop recording ${obs.recording.timecode.slice(0, 8)}` : ' Record'),
      h(Button, { variant: 'secondary', onClick: () => output(replay ? 'stop' : 'start', 'replay_buffer') },
        h(Codicon, { name: 'history' }), replay ? ' Replay buffer on' : ' Start replay buffer'),
      h(Button, { variant: live ? 'destructive' : 'outline', onClick: () => setConfirm(live ? 'stop' : 'start') },
        h(Codicon, { name: 'broadcast' }), live ? ' End stream' : ' Go live')),
    h('div', { className: 'flex flex-wrap items-center gap-2' },
      session && session.active
        ? [h('span', { key: 'label', className: 'text-xs' }, `Session: ${session.title || 'untitled'} · ${session.mark_count} marks`),
           h(Button, { key: 'stop', size: 'sm', variant: 'outline', disabled: busy === 'session', onClick: async () => {
             const result = await act('session', 'session.stop')
             if (result.ok) { host.notify({ kind: 'info', message: 'Session ended. Building the reel…' }); rpc('reel') }
           } }, 'End session & build reel')]
        : [h(Input, { key: 'title', className: 'h-7 w-56 text-xs', placeholder: 'Session title (optional)', value: title,
             onChange: event => setTitle(event.target.value) }),
           h(Button, { key: 'start', size: 'sm', variant: 'outline', disabled: busy === 'session',
             onClick: () => act('session', 'session.start', { title }).then(() => setTitle('')) }, 'Start session')]),
    h(ConfirmDialog, {
      open: confirm !== null, onClose: () => setConfirm(null), dismissOnConfirm: true, destructive: confirm === 'stop',
      title: confirm === 'stop' ? 'End the stream?' : 'Go live now?',
      description: confirm === 'stop' ? 'Viewers will be disconnected.' : 'This starts streaming to the service set up in OBS.',
      confirmLabel: confirm === 'stop' ? 'End stream' : 'Go live',
      onConfirm: () => { output(confirm, 'stream') } }))
}

// ---------------------------------------------------------------------------- scenes and sources

function Scenes({ obs }) {
  const [pending, setPending] = useState(null)
  const live = obs.streaming.active
  const go = name => act('scene', 'scene', { action: 'switch', name })
  return h(Section, { title: 'Scenes' },
    h('ul', { className: 'flex flex-col gap-0.5' }, obs.scenes.map(name =>
      h('li', { key: name },
        h('button', {
          type: 'button', 'aria-current': name === obs.scene ? 'true' : undefined,
          className: cn('flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs hover:bg-(--chrome-action-hover)',
            name === obs.scene ? 'font-semibold text-(--ui-accent)' : ''),
          onClick: () => (name === obs.scene ? null : live ? setPending(name) : go(name)) },
          h(Codicon, { name: name === obs.scene ? 'circle-filled' : 'circle-outline', size: 10 }), name)))),
    h(ConfirmDialog, { open: pending !== null, onClose: () => setPending(null), dismissOnConfirm: true,
      title: 'You are live', description: `Switch viewers to "${pending}"?`, confirmLabel: 'Switch',
      onConfirm: () => { go(pending) } }))
}

function Sources({ obs }) {
  const toggle = source => act('source', 'source', { action: source.visible ? 'hide' : 'show', name: source.name, scene: obs.scene })
  return h(Section, { title: `Sources in ${obs.scene}` },
    obs.sources.length === 0 ? h('div', { className: cn('text-xs', muted) }, 'This scene is empty. Ask Hermes to add something.')
      : h('ul', { className: 'flex flex-col gap-0.5' }, obs.sources.map(source =>
        h('li', { key: source.id, className: 'flex items-center gap-2 px-2 py-0.5 text-xs' },
          h('button', { type: 'button', 'aria-label': `${source.visible ? 'Hide' : 'Show'} ${source.name}`, 'aria-pressed': source.visible,
            className: cn('rounded p-0.5 hover:bg-(--chrome-action-hover)', source.visible ? '' : muted), onClick: () => toggle(source) },
            h(Codicon, { name: source.visible ? 'eye' : 'eye-closed' })),
          h('span', { className: cn('truncate', source.visible ? '' : muted) }, source.name)))))
}

// ---------------------------------------------------------------------------- overlay and designer

function OverlayBox({ installed }) {
  const [text, setText] = useState('')
  const send = (action, extra) => rpc('overlay', { action, text, ...extra }).then(result => { if (result.ok && action !== 'effect') setText('') })
  return h(Section, { title: 'Overlay',
    action: installed ? null : h(Button, { size: 'xs', variant: 'outline', onClick: () => act('setup', 'setup') }, 'Add to scenes') },
    h(Input, { className: 'h-7 text-xs', placeholder: 'Text to put on screen', value: text, onChange: event => setText(event.target.value),
      onKeyDown: event => { if (event.key === 'Enter' && text.trim()) send('card') } }),
    h('div', { className: 'flex flex-wrap gap-1.5' },
      h(Button, { size: 'xs', variant: 'secondary', disabled: !text.trim(), onClick: () => send('card') }, 'Card'),
      h(Button, { size: 'xs', variant: 'secondary', disabled: !text.trim(), onClick: () => send('chapter') }, 'Chapter title'),
      h(Button, { size: 'xs', variant: 'secondary', onClick: () => send('effect', { effect: 'confetti' }) }, 'Confetti'),
      h(Button, { size: 'xs', variant: 'secondary', onClick: () => send('effect', { effect: 'fireworks' }) }, 'Fireworks'),
      h(Button, { size: 'xs', variant: 'outline', onClick: () => send('hide') }, 'Clear')))
}

function Designer({ design }) {
  const [kind, setKind] = useState('starting-soon')
  const [style, setStyle] = useState('aurora')
  const [title, setTitle] = useState('')
  const create = async () => {
    const name = title.trim() || kind.replace(/-/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase())
    const result = await act('design', 'design', { name, kind, style, title: title.trim() })
    if (result.ok) { host.notify({ kind: 'info', message: `Scene "${result.scene}" is ready. Click it under Scenes to put it on air.` }); setTitle('') }
  }
  return h(Section, { title: 'Design a scene' },
    h('div', { className: 'flex gap-1.5' },
      h(Picker, { label: 'Scene kind', value: kind, onChange: setKind, options: design.kinds }),
      h(Picker, { label: 'Animation style', value: style, onChange: setStyle, options: design.styles })),
    h(Input, { className: 'h-7 text-xs', placeholder: kind === 'title' ? 'Title (required)' : 'Title (optional)', value: title,
      onChange: event => setTitle(event.target.value) }),
    h(Button, { size: 'xs', variant: 'secondary', disabled: kind === 'title' && !title.trim(), onClick: create }, 'Create scene'),
    h('div', { className: cn('text-[0.6875rem]', muted) }, 'For anything custom, ask Hermes: "make me a scene that…"'))
}

// ---------------------------------------------------------------------------- marks

const KIND_LABEL = { manual: 'Moment', hotkey: 'Hotkey', fail: 'It broke', fix: 'Fixed', ask: 'Question', chapter: 'Chapter' }
const clock = ms => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` }

function MarkCard({ mark }) {
  const [note, setNote] = useState(mark.note)
  useEffect(() => setNote(mark.note), [mark.note])
  const update = fields => act('mark', 'mark.update', { id: mark.id, ...fields })
  return h('li', { className: cn('flex w-44 shrink-0 flex-col gap-1 rounded-lg border border-(--ui-stroke-secondary) p-1.5', mark.included ? '' : 'opacity-50') },
    mark.thumb ? h('img', { src: mark.thumb, alt: '', className: 'aspect-video w-full rounded object-cover' })
      : h('div', { className: cn('flex aspect-video w-full items-center justify-center rounded bg-(--ui-bg-quaternary) text-[0.6875rem]', muted) }, 'no thumbnail'),
    h('div', { className: 'flex items-center justify-between text-[0.6875rem]' },
      h('span', { className: mark.kind === 'fail' ? 'text-destructive' : 'text-(--ui-accent)' }, KIND_LABEL[mark.kind] || mark.kind),
      h('span', { className: muted }, mark.position_ms != null ? clock(mark.position_ms) : '', mark.clip_state === 'saved' ? ' · clip' : '')),
    h('input', { 'aria-label': 'Mark note', className: cn(fieldClass, 'h-6 w-full'), value: note, placeholder: 'Add a note',
      onChange: event => setNote(event.target.value), onBlur: () => { if (note !== mark.note) update({ note }) },
      onKeyDown: event => { if (event.key === 'Enter') event.target.blur() } }),
    h('div', { className: 'flex items-center justify-between text-[0.6875rem]' },
      h('label', { className: 'flex items-center gap-1' },
        h('input', { type: 'checkbox', checked: mark.included, onChange: event => update({ included: event.target.checked }) }), 'In reel'),
      h('button', { type: 'button', 'aria-label': 'Delete mark', className: cn('rounded p-0.5 hover:bg-(--chrome-action-hover)', muted),
        onClick: () => update({ delete: true }) }, h(Codicon, { name: 'trash' }))))
}

function Marks({ state, os }) {
  const [building, setBuilding] = useState(false)
  const session = state.session
  const reel = session && session.wrapup ? session.wrapup.reel : ''
  const chapters = session && session.wrapup ? session.wrapup.chapters : ''
  useEffect(() => host.onEvent(`plugin.${ID}.reel`, ({ payload }) => {
    setBuilding(false)
    if (payload && payload.ok) host.notify({ kind: 'info', message: `Highlight reel ready (${payload.clips} clips)` })
    else host.notify({ kind: 'error', message: (payload && payload.error) || 'The reel could not be built', detail: payload && payload.hint })
    refresh()
  }), [])
  const build = async () => { setBuilding(true); const result = await rpc('reel'); if (!result.ok) setBuilding(false) }
  const included = state.marks.filter(mark => mark.included).length

  return h(Section, { title: session ? `Marks · ${session.title || 'untitled session'}` : 'Marks',
    action: h('div', { className: 'flex items-center gap-1.5' },
      chapters ? h(Button, { size: 'xs', variant: 'outline', onClick: () => os.writeClipboard(chapters).then(() => host.notify({ kind: 'info', message: 'Chapters copied' })) }, 'Copy chapters') : null,
      reel ? h(Button, { size: 'xs', variant: 'outline', onClick: () => os.revealPath(reel) }, 'Show reel') : null,
      h(Button, { size: 'xs', disabled: building || included === 0 || !state.reel_available, onClick: build },
        building ? 'Building…' : reel ? 'Rebuild reel' : 'Build reel')) },
    state.marks.length === 0
      ? h('div', { className: cn('py-3 text-xs', muted) }, 'No marks yet. Press Mark (⌘⇧M), type /mark, or use the Save Replay hotkey in OBS when something good happens.')
      : h('ul', { className: 'flex gap-2 overflow-x-auto pb-1' }, state.marks.map(mark => h(MarkCard, { key: mark.id, mark }))),
    state.reel_available ? null : h('div', { className: cn('text-[0.6875rem]', muted) }, 'Install FFmpeg to build highlight reels. Marks are saved either way.'))
}

// ---------------------------------------------------------------------------- page and chip

function Studio({ os }) {
  const state = useValue($state)
  useEffect(() => {
    refresh()
    const timer = setInterval(() => { if (!document.hidden) refresh() }, STATE_POLL_MS)
    return () => clearInterval(timer)
  }, [])

  if (!state) return h('div', { className: cn('p-6 text-sm', muted) }, 'Connecting to OBS…')
  const obs = state.obs
  const connected = Boolean(state.ok && obs && obs.connected)

  return h('div', { className: 'flex h-full flex-col gap-3 overflow-y-auto p-4 text-sm' },
    h('header', { className: 'flex flex-wrap items-center gap-2' },
      h('h1', { className: 'mr-2 text-base font-semibold' }, 'On Air'),
      connected ? [
        h(Pill, { key: 'rec', on: obs.recording.active, tone: 'danger' }, 'REC'),
        h(Pill, { key: 'live', on: obs.streaming.active, tone: 'danger' }, 'LIVE'),
        h(Pill, { key: 'replay', on: obs.replay_buffer.active }, 'REPLAY'),
        h('span', { key: 'meta', className: cn('text-xs', muted) }, `OBS ${obs.obs.version} · ${obs.canvas.width}×${obs.canvas.height}`)] : null,
      h('div', { className: 'ml-auto flex items-center gap-2' },
        h('label', { className: 'flex items-center gap-1.5 text-xs', title: 'When paused, Hermes cannot take screenshots of your screen.' },
          h('input', { type: 'checkbox', checked: !state.capture_paused, onChange: event => act('capture', 'capture', { paused: !event.target.checked }) }),
          'Hermes can see the screen'))),
    !connected ? h(Disconnected, { obs: obs || {}, offline: !state.ok })
      : h('div', { className: 'grid gap-3 lg:grid-cols-[minmax(0,1fr)_18rem]' },
        h('div', { className: 'flex min-w-0 flex-col gap-3' },
          h(Preview, { live: connected, scene: obs.scene }),
          h(Transport, { obs, session: state.session })),
        h('div', { className: 'flex flex-col gap-3' },
          h(Scenes, { obs }), h(Sources, { obs }), h(OverlayBox, { installed: obs.overlay_installed }), h(Designer, { design: state.design }))),
    state.ok ? h(Marks, { state, os }) : null)
}

function Chip() {
  const state = useValue($state)
  const obs = state && state.ok ? state.obs : null
  const connected = Boolean(obs && obs.connected)
  const label = !connected ? 'On Air' : obs.streaming.active ? '● LIVE' : obs.recording.active ? '● REC' : 'On Air'
  const hot = connected && (obs.streaming.active || obs.recording.active)
  return h('button', { type: 'button', title: connected ? `OBS connected · scene ${obs.scene}` : 'OBS not connected. Click to open the studio.',
    className: cn('inline-flex h-full items-center px-1.5 text-[0.6875rem] hover:bg-(--chrome-action-hover)', hot ? 'font-semibold text-destructive' : muted),
    onClick: () => host.navigate(ROUTE) }, label)
}

export default {
  id: ID,
  name: 'On Air',
  register(ctx) {
    ctx.onEvent(`plugin.${ID}.changed`, () => { refresh() })
    ctx.setInterval(() => { if (!document.hidden) refresh() }, 20000)   // keeps the status-bar chip honest
    ctx.setTimeout(() => { refresh() }, 1500)
    ctx.onDispose(() => { $state.set(null); $busy.set('') })

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => h(Studio, { os: ctx.os }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'On Air', codicon: 'broadcast' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 125, render: () => h(Chip, {}) },
      { id: 'open', area: PALETTE_AREA,
        data: { id: 'onair.open', label: 'On Air: Open studio', keywords: ['obs', 'stream', 'record', 'live'], run: () => host.navigate(ROUTE) } },
      { id: 'mark-command', area: PALETTE_AREA,
        data: { id: 'onair.mark', label: 'On Air: Mark this moment', keywords: ['obs', 'mark', 'clip', 'highlight'], run: () => { markNow('') } } },
      { id: 'mark-key', area: KEYBINDS_AREA,
        data: { id: 'onair.mark', label: 'Mark this moment', category: 'On Air', defaults: ['mod+shift+m'], run: () => { markNow('') } } }
    ])
  }
}
