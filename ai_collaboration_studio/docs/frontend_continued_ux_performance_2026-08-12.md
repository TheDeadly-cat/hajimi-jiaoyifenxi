# Continued frontend UX and performance evidence — 2026-08-12

This slice improves the host UI without changing room creation, formal-round
authorization, domain capability, Provider, market, wallet, or execution
semantics. All runtime checks used a system-temporary runtime and SQLite file,
`AI_STUDIO_SKIP_LOCAL_ENV=1`, unconfigured Provider keys, and Futu fixed to the
offline sentinel `127.0.0.1:1`.

## Changes

- The mobile Create Room flow now uses a dedicated `100dvh` surface with a
  sticky header and safe-area-aware sticky action bar. It preserves every
  template, capability-pack, lifecycle, and explicit stock-pool field, and
  keeps `stockRoomFormSubmission(form)` as the only submit path.
- The Create Room action bar exposes a neutral live status. Invalid explicit
  stock scope and unverifiable capability lifecycle continue to block creation;
  the UI does not auto-discover or expand instruments.
- Action Overview drawer CSS now follows its lazy component. The eager sidebar
  entry style remains in the host stylesheet, so the cold shell is styled
  without loading the drawer CSS.
- Formal Round Launch now uses the shared modal focus contract. Its stable close
  button receives initial focus, Tab and Shift+Tab stay inside the topmost
  dialog, Escape closes only that dialog, and cancellation restores the exact
  Composer or Inspector trigger. While confirmation is being submitted, the
  dialog consumes Escape, moves focus to its stable container, and preserves
  all existing busy-state close guards.
- When a compact Room Inspector is below Formal Round Launch, its keyboard
  handlers pause until the launch dialog closes. This prevents a background
  drawer from stealing focus or closing in response to the top dialog.
- Member editing, Workflow Policy, and Member Version History now share the
  same modal focus contract: stable initial focus, bidirectional Tab trapping,
  top-surface-only Escape, exact trigger restoration, and fail-closed busy
  dismissal for the two editable surfaces.
- Workflow Policy and Member Version History styles now follow their lazy
  modules. Shared inspector/member rules and the eager history trigger remain
  in the host stylesheet.
- The first-load Formal Round Suspense surface is now a real labelled modal with
  a visible cancel action that only aborts read-only planning. A successfully
  started round restores focus to the visible conversation status rather than
  attempting to focus a now-disabled start button.
- Compact Room Inspector restoration now records the actual header or rail
  trigger and falls back only to a connected, visible, enabled control. This
  also handles a desktop-opened inspector that is resized to mobile before
  closing.

## Browser acceptance

An isolated HTTP server was bound only to `127.0.0.1:18776`; the formal `8770`
and Futu/OpenD `11111` ports were not started or contacted. No launch
confirmation was submitted and no Provider or market call was made.

- Create Room at `390x844` and `320x568`: exact viewport-width surface, sticky
  header/action bar, visible focused stock-pool field, no horizontal overflow,
  and disabled creation with the explicit stock-pool blocker.
- Action Overview: its CSS was absent from the cold page resource set, appeared
  on first open, and rendered correctly at desktop and `390x844`; close restored
  the exact trigger.
- Formal Round Launch from the desktop Composer: close received initial focus,
  Shift+Tab wrapped to Cancel, Tab wrapped back to close, and Escape restored
  the exact enabled `开始一轮` button.
- Formal Round Launch from the `390x844` compact Inspector: the top dialog kept
  keyboard ownership; Escape closed only it, left the Inspector open, and
  restored focus to the Inspector's original `开始一轮` button.
- The isolated server's stdout and stderr logs remained empty during this QA.

A second isolated browser pass used `127.0.0.1:18777` and a separate explicit
system-temporary SQLite file. It did not contact the formal database, Provider,
Futu/OpenD, or the formal `8770` service.

- Member editing, Workflow Policy, and Member Version History each opened with
  the close control focused; Shift+Tab reached the final enabled action, Tab
  wrapped back to close, and Escape restored the exact originating control.
- Workflow Policy and Member Version History CSS were absent before their first
  open and appeared as their own hashed stylesheets afterwards.
- At `390x844`, Workflow Policy rendered without horizontal overflow. Escape
  closed only the nested policy dialog, kept Room Inspector open, and restored
  its internal `设置` trigger.
- Compact Inspector close restored either the exact mobile `房间信息` trigger or,
  after a desktop-to-mobile resize, the visible mobile fallback trigger.
- Formal Round Launch loaded too quickly to visually hold the Suspense surface;
  the resulting confirmation dialog still received initial focus and restored
  the exact Composer trigger. No confirmation was submitted.
- Browser console errors and warnings: `0`.

A third isolated browser pass crossed the 2026-08-12/13 local midnight and used
`127.0.0.1:18778` with another explicit system-temporary runtime and SQLite
file. It added no Provider, market, wallet, or formal-service access.

- Material editing and official-attestation preview now share one focus owner
  across their surface transition. Material editing at desktop and `390x844`
  opened on the stable close control, trapped both Tab directions, and restored
  the exact `添加` trigger. Busy dismiss and submit routes remain fail closed.
- Observation and Reflection use the same initialized-surface focus contract.
  Observation at desktop and `390x844` opened on its close control, trapped
  focus, and restored the exact `新建观察` trigger. A nested Escape closed only
  the top surface.
- Round Execution Trace and Discussion Audit CSS was absent before the first
  trace open and appeared as one hashed lazy stylesheet on demand. The trace
  rendered at `390x844` with no horizontal overflow and no console warning.
- This runtime check exposed and fixed an HTTP guard bug: execution-shaped API
  routes remain forbidden, while hashed static filenames containing
  `Execution` are now served normally instead of being rejected with `403`.
  A backend regression test locks both sides of this boundary.
- The isolated server's final stdout and stderr logs remained empty, all
  browser tabs were closed, and the temporary port was released.

A fourth isolated browser pass used `127.0.0.1:18780`, an explicit
system-temporary runtime, and an explicit temporary SQLite file. The formal
database, formal `8770`, Provider routes, and Futu/OpenD were not contacted.

- Artifact editing and Paper Portfolio editing now use the shared modal focus
  contract. On desktop and at `390x844`, the stable close action received
  initial focus, reverse Tab wrapped to the final action, Escape closed only
  the top surface, and focus returned to the exact artifact row or portfolio
  edit button that opened it.
- The mobile Room Sidebar is now a labelled navigation dialog rather than a
  visually positioned sidebar. It owns focus while open, makes the application
  content inert, wraps both Tab directions, restores the exact menu trigger,
  and remains an ordinary eager sidebar on desktop.
- Selecting a room from the mobile drawer restores focus to that room's
  conversation status rather than reopening the drawer. Transitioning from the
  drawer into Create Room transfers focus ownership to the new dialog without
  a stale sidebar restoration race.
- A closed compact Room Inspector is now inert and absent from the modal
  accessibility tree; it exposes `dialog` semantics only while actually open.
- Artifact, portfolio, and sidebar surfaces had no horizontal overflow at
  `390x844`. Browser console errors and warnings remained `0`.

A fifth isolated browser pass used `127.0.0.1:18781` with the same strict
system-temporary runtime and explicit disposable SQLite boundary. Provider
credentials and proxy variables were blank, Futu pointed to loopback port `1`,
and no formal service or database was opened.

- A mounted-DOM regression first reproduced that two nested focus hooks both
  handled the same Tab and Escape event. The shared hook now keeps one ordered
  active-surface registry, and only the top connected modal may consume those
  keys. Real browser checks with a compact Inspector under Artifact and Paper
  Portfolio confirmed that Escape closes only the top dialog and restores its
  exact internal launch control.
- Closing and immediately reopening the same Artifact no longer reuses an
  abandoned unsaved draft during the two-frame focus-restoration interval.
  Each open transition creates a new editor session before paint; browser QA
  changed the title locally, dismissed the editor, reopened it, and observed
  the original persisted title.
- Paper Portfolio styles now load with its Inspector/lazy-module closure rather
  than the cold host shell. Before opening Inspector, the page had only the
  eager host stylesheet; afterwards it loaded the independent hashed Paper
  stylesheet.
- Room Inspector starts loading on pointer hover/down or keyboard focus from
  both the desktop header and the compact icon rail. Failed warm-up attempts
  clear their cached promise so a later explicit open can retry.
- At `320x568`, Paper Portfolio occupied the exact viewport with a sticky
  action footer, while Artifact remained inset at 20px on both sides. Both had
  document `scrollWidth == clientWidth == 320`, stable close focus, and no
  clipped horizontal content. Native browser `200%` zoom remains a separate
  assistive-technology acceptance item because this Browser runtime does not
  expose page-zoom mutation.
- Browser console warnings/errors and isolated server stdout/stderr remained
  `0`; the tab and viewport override were closed and port `18781` was released.

A sixth isolated browser pass used `127.0.0.1:18782` with the same explicit
system-temporary SQLite boundary. Provider credentials and proxy variables
were blank, Futu pointed to loopback port `1`, and no formal service or
database was opened.

- Artifact workspace CSS is now owned by the lazy Artifact dialog closure.
  The cold shell loaded only the eager hashed stylesheet; opening Inspector
  loaded its own domain slices but still no Artifact stylesheet. The Artifact
  stylesheet appeared only after opening the seeded `Bounded prototype review`
  artifact, while the stable close control received focus.
- At `320x568`, the Artifact dialog stayed inset at 20px, the document kept
  `scrollWidth == clientWidth == 320`, the evidence-verification control
  measured `96x44px` at `12px` with wrapping enabled, and the remove control
  measured `44x44px`.
- The Paper decision-lineage copy now renders at `10px` with normal wrapping,
  and its edit, recompute, and user-confirmation controls each measured 44px
  high. Compact member history/reorder actions form a separate `3 x 44px`
  action row rather than three adjacent `22x18px` controls.
- A real mounted Observation dialog test now edits every business field,
  submits one exact payload, proves the awaited `aria-busy`/duplicate-submit/
  Escape fail-closed interval, and restores the exact opener after success.
- Browser console warnings/errors and isolated server stdout/stderr remained
  `0`; the viewport override and tab were closed and port `18782` was released.

A seventh isolated browser pass used `127.0.0.1:18783`, an explicit
system-temporary runtime, and an explicit disposable SQLite file. Provider
credentials and proxy variables were blank, Futu was redirected to loopback
port `1`, and the formal database, formal `8770`, Provider routes, and the real
Futu/OpenD service were not contacted.

- Reduced-motion behavior is now owned by the actual animated surfaces rather
  than a blanket `*` reset. Drawers, dialogs, loaders, typing indicators,
  spinners, disclosure arrows, and hover transforms retain visible state while
  suppressing only motion. JavaScript-initiated chat and evidence-review
  scrolling also respects the user's reduced-motion preference.
- Chat history is no longer one large live region. A single short, atomic
  announcer stays silent for initial load, room switches, searches, typing, and
  history prepends, while announcing each genuine tail message once. A
  concurrent-history regression proves that a new tail arriving while older
  messages load is announced once and is not repeated after loading completes.
- Meeting readiness, convergence, and storage-sample acceptance now expose one
  compact state sentence with the first blocker or next action instead of
  repeatedly announcing an entire changing panel.
- Observation and Reflection styles moved into their lazy component closures.
  Their selector blocks were compared rule-by-rule with the preceding source
  snapshot; shared dialog, paper-lineage, safety, and form primitives remain in
  the eager host stylesheet.
- At `320x568`, the cold shell loaded only the host stylesheet. Opening Room
  Inspector loaded its domain slices but not Reflection or Artifact CSS;
  Artifact CSS appeared only after opening the artifact editor. The Artifact
  dialog remained inset at 20px, kept `scrollWidth == clientWidth == 320`, and
  used readable wrapped final-decision copy and a stable close-focus target.
- Browser console warnings/errors and isolated server stdout/stderr remained
  `0`; the tab and viewport override were finalized, the exact server process
  was stopped, and port `18783` was released.

An eighth isolated browser pass used `127.0.0.1:18784`, another explicit
system-temporary runtime, and a copied disposable SQLite fixture. Provider
credentials and proxy variables were blank, Futu was redirected to loopback
port `1`, and the formal database, formal `8770`, Provider routes, and the real
Futu/OpenD service were not contacted.

- On desktop, the closed Room Inspector no longer reserves an empty 340px
  fourth column. At `1440x900`, the conversation workspace measured 1096px
  while closed; opening the explicit `房间信息` control produced the expected
  `64 / 280 / 756 / 340px` four-column evidence-console layout. Both states
  kept document `scrollWidth == clientWidth == 1440`.
- Real browser QA caught a first implementation regression before acceptance:
  the desktop Inspector trigger was still hidden by its old compact-only CSS.
  The control is now visible at every viewport, and a source contract prevents
  the closed workspace from becoming a one-way state.
- At `320x568`, the header now keeps the truncated room identity and a neutral
  `待讨论` state on separate lines. The compact evidence brief, both safety
  hints, and the Composer were simultaneously visible with document
  `scrollWidth == clientWidth == 320`.
- The mobile Room Inspector remained a labelled modal drawer, measured about
  282px wide, made the conversation inert, focused its close action, and
  restored focus to the exact `房间信息` trigger after Escape.
- The first-load `React.lazy` dialog handoff now has mounted-DOM coverage. The
  loading fallback immediately owns modal focus, Escape closes only the top
  surface, resolved content keeps a stable focus target, ordinary close returns
  to the exact opener, and a successful formal launch avoids the still-disabled
  launch trigger.
- Browser console warnings/errors and isolated server stdout/stderr remained
  `0`; all tabs and temporary viewport overrides were finalized, the exact
  Python process was stopped, and port `18784` was released.

A ninth isolated browser pass used `127.0.0.1:18786`, an explicit
system-temporary runtime and disposable SQLite file. Provider credentials and
proxy variables were blank, Futu was redirected to `127.0.0.1:1`, and the
read-only status endpoint confirmed that exact host and port before browser
interaction. Formal `8770`, the formal database, Provider routes, and the real
Futu/OpenD service were not contacted.

- Room Inspector now presents the evidence dossier in decision order: target
  and round admission remain adjacent to the start control, then convergence,
  the first backend-prioritized blocker, next action, user-decision boundary,
  and only then Provider routing and workflow configuration.
- Technical gates and every remaining blocker are available in a native,
  keyboard-operable disclosure that starts closed. The user-decision row and
  the no-execution boundary never move behind that disclosure, and blocker
  counts no longer silently cap at two.
- Ordinary waiting, unconfigured, policy-disabled, offline, and fail-closed
  states now use neutral blue-grey or amber tokens. Verified research state is
  deliberately restrained; red remains available for explicit integrity,
  hash, binding, lifecycle, or read-only-boundary failures. The market gate now
  carries an explicit critical severity for a broken read-only contract rather
  than inferring severity from prose.
- `ObservationPanel` and `PaperPortfolioPanel` are nested lazy dependencies of
  the capability-gated Room Inspector. On a generic room, the page-asset
  inventory contained neither panel before nor after opening the Inspector;
  their JavaScript and CSS therefore do not ride along with an ordinary,
  football, or stock-room Inspector preload.
- At `1440x900`, document `scrollWidth == clientWidth == 1440` and the open
  Inspector measured `clientWidth == scrollWidth == 324`. The convergence card
  preceded Provider configuration, its detail disclosure started closed, and
  expanding it exposed all four technical gates plus both remaining blockers.
- At `320x568`, the labelled modal drawer measured about `282px`, its internal
  Inspector kept `clientWidth == scrollWidth == 266`, document width remained
  exactly `320px`, the close action owned focus, and Escape restored the exact
  `房间信息` trigger. Browser console warnings/errors were `0`.
- Screenshots are stored in the isolated QA directory at
  `screenshots/inspector-desktop-1440x900.png` and
  `screenshots/inspector-mobile-320x568.png`. The browser tab and viewport
  override were finalized, both temporary Python processes were stopped, and
  port `18786` was released.

## Automated evidence

- Latest full frontend tests: `407/407` passed.
- Mounted-DOM modal tests: `6/6` passed, covering initial focus, forward and
  reverse Tab, busy Escape, exact restoration, nested top-only Tab/Escape, and
  busy top-surface non-propagation.
- Mounted business-dialog regression: `1/1` passed with a complete edited
  Observation payload and an awaited busy transition.
- Vite production build: `1687` modules, successful, with no chunk above the
  warning threshold.
- Eager CSS: `142.17 kB` raw / `26.30 kB` gzip, still substantially below the
  `228.31 kB` / `40.01 kB` pre-split baseline while now also owning the neutral
  status-token and convergence-summary styles.
- Lazy Action Overview CSS: `8.65 kB` raw / `2.07 kB` gzip.
- Lazy Workflow Policy CSS: `6.78 kB` raw / `1.86 kB` gzip.
- Lazy Member Version History CSS: `5.67 kB` raw / `1.42 kB` gzip.
- Lazy Round Execution Trace and Discussion Audit CSS: `24.60 kB` raw /
  `4.60 kB` gzip.
- Lazy Paper Portfolio CSS: `6.46 kB` raw / `1.67 kB` gzip.
- Lazy Observation CSS: `4.93 kB` raw / `1.27 kB` gzip.
- Lazy Reflection CSS: `0.92 kB` raw / `0.45 kB` gzip.
- Lazy Artifact workspace CSS: `39.19 kB` raw / `7.02 kB` gzip.
- Main JS: `494.48 kB` raw / `150.02 kB` gzip.
- Lazy Room Inspector: `111.02 kB` raw / `35.01 kB` gzip. Its generic-room
  preload no longer observes the lazy Observation (`15.54 / 5.78 kB`) or Paper
  Portfolio (`42.14 / 14.20 kB`) JavaScript chunks or their CSS resources.
- HTTP security regression for static asset/API boundary: `9/9` passed in the
  isolated backend runner; no non-loopback request, formal port, or Provider
  call was made.

## Remaining frontend work

- Continue ownership-audited lazy CSS extraction only where the component
  boundary is closed. Room Inspector and the combined `Dialogs.jsx` chunk still
  share selectors with eager surfaces and must not be split by prefix alone.
- Add a mounted-DOM slow-chunk regression for a deep Action Desk navigation
  target. The current stable `PluginActionBoundary` wrappers avoid replacing a
  direct Inspector child during nested lazy handoff; a deliberately delayed
  module test would further protect scroll alignment under severe I/O delay.
- Continue native browser `200%` zoom, visual-viewport/virtual-keyboard height,
  and real screen-reader acceptance. The current pass proves the code-level and
  browser reduced-motion contracts at desktop, `390x844`, and `320x568`, but
  does not represent every assistive-technology combination.
