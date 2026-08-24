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
- Compact header, history, Room Inspector, and Composer actions now share the
  `--mobile-touch-target: 44px` token instead of mixing 36-44px controls. The
  Composer toolbar and action hierarchy were tightened without changing send,
  round-launch, or room-state semantics.
- The 12-member mention picker now exposes an owned labelled menu, roving
  keyboard focus, Arrow/Home/End navigation, Escape restoration, explicit
  Enter/Space activation, and exact textarea focus restoration after a choice.

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

### 2026-08-21 controlled refresh

The current tree was rerun through the README-designated frontend entry points,
not through a direct `node --test` invocation. `npm.cmd --prefix frontend test`
used `frontend/scripts/run-tests-safe.ps1` and completed 79 test files with
`423/423` tests passed, `0` failed, cancelled, skipped, or todo. The guarded
runner retained its per-file, single-process-tree boundary; the old top-level
Vite/JSDOM unresolved-lazy harness was not restored.

`npm.cmd --prefix frontend run build` completed with Vite 6.4.3, `1688`
modules, and no chunk-size warning. The new `dist` contains 51 files totalling
`1,276,478` bytes. The largest current outputs are main JavaScript `497,505`
bytes (`497.51 kB`, `150.94 kB` gzip), eager CSS `144,388` bytes (`144.39 kB`,
`26.78 kB` gzip), lazy Artifact JavaScript `129,186` bytes (`129.19 kB`,
`38.73 kB` gzip), and lazy Room Inspector JavaScript `111,145` bytes
(`111.15 kB`, `35.09 kB` gzip).

The Browser-plugin acceptance pass used `127.0.0.1:58413`,
`AI_STUDIO_SKIP_LOCAL_ENV=1`, one system-temporary runtime, an explicit SQLite
file inside that runtime, an ephemeral `AI_STUDIO_PORT=0` binding, blank
Provider credentials and proxy variables, and Futu fixed to `127.0.0.1:1`.
The disposable store was seeded before serving with one 12-member room and no
domain capability pack. Its final read-only status reported Provider calls `0`,
market/Futu reads `0`, outbound attempts `0`, blocked mutation requests `0`,
and an unchanged logical SQLite digest. The browser tab and viewport override
were closed, the server exited with code `0`, and its temporary runtime was
released.

- At `1440x900`, document and body `scrollWidth` equalled the 1440px client
  width. The 12-member picker exposed exactly 12 `menuitem` rows in a
  `231x382px` scroll surface (`scrollHeight=612px`) wholly inside the visual
  viewport; selecting the final row inserted `@投委会决策经理` without a POST.
- At `320x568`, document and body `scrollWidth` equalled the 320px client width.
  The picker again exposed all 12 rows in a `231x376px` scroll surface from
  `left=20px` to `right=268px`, and the final row remained selectable. The
  Composer ended at 558px, leaving the zero-inset runtime's 10px bottom gap.
- The effective 200% layout check used `720x450` CSS pixels, the result of
  halving the `1440x900` reference viewport. Document and body width remained
  exactly 720px; the 12-member picker remained bounded in a `231x258px` scroll
  surface. This was responsive-layout evidence, not native browser page zoom:
  `devicePixelRatio=1` and `visualViewport.scale=1` throughout.
- At all three sizes, `visualViewport` geometry matched the current CSS
  viewport and the root published matching
  `--visual-viewport-width/height/offset-left/offset-top/scale` values. The
  browser recognized `env(safe-area-inset-bottom)`, but this desktop runtime
  supplied a zero inset; it therefore does not prove behavior on a physical
  notched device or while a mobile keyboard changes the visual viewport.
- Desktop Room Settings focused the first editable field. Reverse Tab reached
  Close, a second reverse Tab wrapped to Save, forward Tab wrapped to Close,
  and Escape restored the exact Room Settings trigger. At `320x568`, the
  `~282px` Room Inspector drawer focused Close, made the background inert,
  wrapped reverse Tab to its final action and forward Tab back to Close, then
  restored the exact Room Information trigger on Escape. Neither surface
  introduced horizontal overflow.
- Console warnings and errors were `0` after load and after every tested menu,
  viewport, drawer, and dialog transition. No launch confirmation, message
  send, Provider probe, market refresh, or business mutation was submitted.

The evidence levels remain deliberately separate. The `visualViewport`,
`mobileViewport`, modal-focus, live-region, safe-area, and mobile-control tests
prove source and mounted-DOM contracts. The Browser-plugin pass proves rendered
Chromium behavior for the exact zero-inset viewports above. It does not exercise
a native browser 200% page-zoom control, an operating-system soft keyboard or
IME/composition session, a non-zero physical safe area, or a real screen reader.
That pass also did not prove a universal 44px touch target: it found several
visible compact header and Composer controls at 36-40px. The current-build
follow-up below supersedes that specific size gap, but not physical touch-device
acceptance.

### 2026-08-21 compact Composer follow-up

A separate current-build Browser-plugin pass used random loopback port
`127.0.0.1:59144`, `AI_STUDIO_SKIP_LOCAL_ENV=1`, a system-temporary runtime,
an explicit SQLite file inside that runtime, blank Provider credentials and
proxy variables, Futu fixed to `127.0.0.1:1`, and no capability packs. The
fixture seeded exactly 12 enabled committee members before its read-only HTTP
server started. Its instrumented status reported Provider execution attempts
`0`, outbound socket attempts `0`, and blocked business-write attempts `0`.

- At `1440x900`, the production page loaded with
  `document.scrollWidth == clientWidth == 1440`, no framework error overlay,
  and no console warning or error.
- At `320x568`, document width remained exactly 320px and `visualViewport`
  reported `320x568` with zero offset. The room-list and settings icons, Room
  Information, history controls, mention trigger, Start, Send, and modal close
  controls all measured at least 44px high. The Composer measured `300x144px`
  and ended 10px above the zero-inset viewport bottom.
- The mention picker exposed exactly 12 `menuitem` rows in a bounded
  `248x378px` scroll surface. ArrowDown opened it on the first item, End moved
  focus to `投委会决策经理`, Escape closed it and restored the mention trigger,
  and Enter/Space selected a focused row, closed the menu, inserted the exact
  mention text, and restored the textarea. The browser pass first caught the
  missing explicit Enter path; the current production build contains and
  re-exercised the fix.
- Mobile Room Settings rendered as one labelled `280x528px` dialog, focused its
  first input, kept the active element inside the dialog, used a `44x44px` close
  control, and restored the exact settings trigger after Escape. It introduced
  no horizontal overflow.
- The effective 200% layout check used `720x450` CSS pixels, half of the
  `1440x900` reference dimensions. Document width remained exactly 720px,
  `visualViewport` matched `720x450`, and the header, Composer, Room Information,
  settings, and mention controls remained reachable with 44px targets. This is
  responsive reflow evidence at `devicePixelRatio=1`; it is not native browser
  page-zoom evidence.
- Console warnings and errors remained `0` through load, viewport changes,
  menu keyboard interaction, and dialog open/close. No message, launch,
  Provider probe, market refresh, or business mutation was submitted.

The evidence levels remain separate. Static source tests prove the owned CSS,
ARIA, visual-viewport binding, and focus-control contracts. This browser pass
proves rendered Chromium geometry and keyboard events for the exact zero-inset
viewports above. It does not exercise native 200% page zoom, a mobile operating
system soft keyboard, IME/composition, a non-zero physical safe area, physical
touch accuracy, or a real screen reader.

### 2026-08-21 bounded late-lazy navigation follow-up

The current source extracts Inspector target alignment into the independent
`bindInspectorTargetNavigation` DOM binder. `RoomInspector` now owns only the
effect wiring, while the binder owns the four-second lifetime, late target
resolution, frame cancellation, focus-once behavior, observer cleanup, and
scroll realignment. Mutation delivery also attaches ResizeObserver to newly
inserted top-level children, so a resolved lazy surface remains observable for
later layout shifts.

`inspectorTargetNavigation.dom.test.js` adds a deliberately delayed React
Suspense target in a minimal JSDOM tree. It is designed to prove that the deep
Action Desk target is absent behind the fallback, then receives exact focus and
alignment after resolution, realigns after a preceding lazy panel changes
height, and releases frames, timers, and observers on unmount. It does not mount
the complete `RoomInspector`, start Vite SSR, or restore the retired unresolved
top-level lazy harness. This new test has not yet been included in an authorized
guarded run, so the latest executed aggregate below remains `423/423` rather
than treating source presence as passing evidence.

### 2026-08-21 Action Overview visual hierarchy follow-up

The lazy `action-overview.css` slice now presents the cross-room read-only view
as an evidence workbench rather than a dense utility list. The source change
adds a restrained layered drawer background, stronger heading identity, more
readable 10-13px information levels, state-specific count rails, clearer source
provenance, focused-card treatment, and a sticky search/status filter surface.
At narrow widths the adopted-total card spans the two-column count grid, while
every interactive control retains the existing 44px mobile minimum. No ranking,
winner, adoption, mutation, or navigation behavior changed.

This is currently source-level design work only. It has not yet been included
in a new production build or Browser-plugin pass, so responsive geometry,
rendered contrast, asset size, and console state remain unverified for this
specific visual revision and are not added to the executed evidence below.

### Football Research evidence-workbench visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/styles/football-research.css` now gives match facts, provenance classes, authorization context, JSON input, and evidence cards a clearer research-workbench hierarchy without changing the React/domain contract.
- Component container queries collapse the panel independently of the host viewport; narrow controls retain a 44 px minimum target and bottom spacing includes the safe-area inset.
- Focus-visible and reduced-motion rules remain explicit. Evidence and authorization cards use restrained left-edge status rails; the specific left-edge color is declared after the generic border color so the provenance/authorization cue is not cancelled by the cascade.

Acceptance limits:

- This CSS follow-up has not yet been included in a new safe frontend regression, production build, or rendered browser pass. The latest executed aggregate remains the earlier 423/423 frontend result and therefore does not validate the later delayed-lazy, Action Overview, or Football Research source changes.
- Source inspection is not evidence of real IME composition behavior, actual screen-reader browse/forms-mode output, native browser 200% zoom, operating-system text scaling, hardware safe-area behavior, or visual quality on a rendered device. Those remain explicitly uncovered until a dedicated acceptance run is authorized and recorded.

### Stock Research preflight-desk code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/components/StockResearchPanel.jsx` now invalidates in-flight inspections when the JSON source or room changes and guards responses with a monotonically increasing generation. Late responses therefore cannot repopulate a result after their source was superseded, even if abort delivery is delayed.
- Local file reads use a separate generation guard, so a slower earlier import cannot overwrite a later import, manual edit, room change, or unmount. Evidence claims are grouped in one linear pass instead of rescanning the full claim list for each evidence class.
- The disclosure now owns its controlled region through `aria-controls`; loading/completion uses a concise polite status, the region exposes `aria-busy`, JSON autocorrection is disabled, and the file control has an explicit keyboard focus treatment.
- `frontend/src/styles/stock-research.css` uses component container queries, source/status rails with redundant text labels, 44 px narrow controls, safe-area-aware bottom spacing, visible focus, and retained reduced-motion behavior. The stock read-only contract, explicit next-round authorization, and permanent no-execution boundary are unchanged.

Acceptance limits:

- These Stock Research changes have not yet been included in a new safe frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level ARIA and race guards do not prove real screen-reader announcements, IME composition, file-picker keyboard behavior, native 200% browser zoom, operating-system text scaling, hardware safe-area behavior, or rendered visual quality. Those remain uncovered until a dedicated isolated acceptance run is authorized and recorded.

### Paper Portfolio risk-ledger code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/components/PaperPortfolioPanel.jsx` now disables the complete editable dialog fieldset while risk recomputation is pending. The existing focus trap moves focus to the dialog surface, while fields, scenario controls, and mapping confirmation can no longer diverge from the submitted payload during that request.
- Default risk budgets are materialized once and cloned into each plan instead of constructing a full blank portfolio only to recover its budgets. Current-version walk-forward runs sort the new array returned by `filter`, avoiding dependence on `Array.prototype.toSorted` without mutating input data.
- Walk-forward loading has a concise polite status and asynchronous errors use `role="alert"`; risk-state data attributes provide styling hooks without changing the paper-only domain contract or action labels.
- `frontend/src/styles/paper-portfolio.css` raises the prior 7–9 px evidence text, preserves complete lineage text, adds component-container responsiveness, retains the tested 760/620 px dialog fallbacks, 44 px mobile targets, safe-area footer, and visible keyboard focus. Historical-return fields remain visually subordinate to lineage, gates, and the permanent no-order boundary.

Acceptance limits:

- These Paper Portfolio changes have not yet been included in a new safe frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source structure does not prove real focus behavior during a slow recomputation, screen-reader announcements, IME behavior in thesis/invalidation fields, native 200% browser zoom, operating-system text scaling, hardware safe-area behavior, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Observation forward-log code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/components/ObservationPanel.jsx` now formats resolved returns fail-closed: missing or non-finite return/hit fields render explicit “未返回” text instead of `NaN%` or an inferred miss. Unknown symbol/direction/status input also remains readable rather than throwing during render.
- Reflection indexing and confirmed-count calculation share one pass. Every panel action declares `type="button"`, refresh exposes a concise polite status, and the decorative status dot is hidden from assistive technology because the textual status already carries the meaning.
- Observation saves use a monotonically increasing request session. A failure from a superseded close/reopen or lineage reinitialization cannot overwrite the current dialog, duplicate submits are rejected, and the named dialog fieldset is disabled while the single existing submit chain is pending.
- `frontend/src/styles/observation.css` adds a component container, readable calibration and observation typography, status rails with redundant text, complete wrapping metadata, visible focus, 44 px mobile actions, a visual-viewport-height dialog, and safe-area-aware footer. The independently mounted dialog redeclares its evidence accent token and retains property-level fallbacks, so it does not depend on ancestry under `.observation-panel`. The forward observation remains separate from historical replay and cannot become a trading instruction.

Acceptance limits:

- These Observation changes have not yet been included in a new safe frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level guards and ARIA do not prove real slow-request focus behavior, screen-reader announcements, IME behavior in thesis/counter-case fields, native 200% browser zoom, operating-system text scaling, hardware safe-area behavior, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Reflection settlement-sheet code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/components/ReflectionDialog.jsx` now formats missing or non-finite source returns, peer returns, relative returns, hit states, symbols, directions, horizons, and hashes as explicit unavailable values instead of `NaN%`, inferred misses, blank fingerprints, or render errors.
- Both “保存草稿” and “确认并纳入记忆” pass through the same trimmed three-field validation, closing the prior gap where the `type="button"` confirmation path bypassed native `required` validation. The existing single save and optional confirm business-call chain is preserved.
- A monotonically increasing request session suppresses failures and follow-up confirmation from a superseded dialog surface. The named editable fieldset is disabled while pending, asynchronous errors use `role="alert"`, and the existing focus trap remains fail-closed.
- `frontend/src/styles/reflection-dialog.css` turns the source snapshot into a readable neutral settlement sheet, preserves full values, adds visible focus, visual-viewport-height mobile layout, 44 px mobile actions, and a safe-area-aware footer. Hit/miss and return values remain evidence, not a future-performance or execution signal.

Acceptance limits:

- These Reflection changes have not yet been included in a new safe frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source structure does not prove slow save/confirm behavior, real screen-reader announcements, IME behavior in reflection fields, native 200% browser zoom, operating-system text scaling, hardware safe-area behavior, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Round Execution Trace flight-recorder code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/components/RoundExecutionTraceSummary.jsx` exposes its read-only fetch state through `aria-busy` without changing the existing summary action or lazy-dialog boundary.
- `frontend/src/components/RoundExecutionTraceDialog.jsx` now treats non-string hashes, short identifiers, missing round identifiers, and malformed invalid-response error lists fail-closed. The surface renders explicit `未记录` or a local-validation fallback instead of throwing, duplicating a short hash, or inventing a server reason.
- `frontend/src/styles/round-execution-trace.css` strengthens the existing chronological timeline into a restrained read-only flight recorder: the sequence spine is the single signature element, integrity tones remain distinct from approval, evidence identifiers wrap instead of being visually discarded, and component-container fallbacks complement the existing `760/620px` viewport rules.
- The lazy CSS ownership, manual modal focus trap, Escape propagation guard, visual-viewport height, 44 px mobile disclosure/actions, safe-area footer, discussion-audit contract, provider-call budget, and permanent no-execution boundary are unchanged.

Acceptance limits:

- These Round Execution Trace changes have not yet been included in a new safe frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level fallbacks, ARIA, container queries, and focus selectors do not prove real modal focus order, screen-reader announcements, IME or virtual-keyboard behavior, native 200% browser zoom, operating-system text scaling, non-zero hardware safe-area behavior, long-trace rendering performance, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Member Version History archive-index code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/memberVersionDiff.js` now preserves explicit unknown states for missing or malformed participation and lifecycle fields. A missing boolean is no longer silently projected as “暂停参与” or “活动”, so before/after comparisons can distinguish unavailable evidence from a recorded false state.
- `frontend/src/components/MemberVersionHistoryDialog.jsx` only admits positive integer versions with `integrity_ok === true` into exact snapshot comparison. Missing integrity is rendered as a separate unknown state, malformed request rejections fall back safely, duplicate or invalid version rows receive stable render keys, and missing provider/model metadata remains explicit rather than implying a default.
- The dialog and comparison region expose busy state, loading copy uses polite status semantics, decorative icons are hidden from assistive technology, and diff values preserve boolean false instead of rendering it as empty.
- `frontend/src/styles/member-version-history.css` presents the read-only surface as an archive index with a chronological ledger spine and a semantic baseline-to-comparison bridge. It raises prior 8–10 px evidence text, preserves complete metadata wrapping, adds component-container fallbacks and visible focus, and retains the tested lazy CSS, visual-viewport, 44 px mobile action, and safe-area footer contracts.

Acceptance limits:

- These Member Version History changes have not yet been included in a new guarded frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level tri-state handling, ARIA, container queries, and focus selectors do not prove live API cancellation, real screen-reader announcements, native select behavior, modal focus order, mobile IME or virtual-keyboard behavior, native 200% browser zoom, operating-system text scaling, non-zero hardware safe-area behavior, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Workflow Policy routing-board code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/workflowPolicy.js` now treats stage, stance, capability, requirement, and member collections as arrays only when they are actually arrays. Workflow readiness counts only members with `enabled === true`; malformed strings or truthy substitutes no longer become character-level capabilities or enabled participants, and counterargument flags require an explicit boolean true.
- `frontend/src/components/WorkflowPolicyDialog.jsx` reuses the shared requirement matcher, guards duplicate submits synchronously, and assigns every save a request session. Closing, reopening, switching rooms, or unmounting invalidates late success/failure continuations so an old save cannot close or write an error into a newer form.
- The three editable sections are disabled as fieldsets while saving, malformed Promise rejections fall back safely, the dialog has explicit description/boundary ownership, loading exposes a polite status, and decorative icons no longer add duplicate accessible names. The normalized payload and user-confirmation/no-execution constants are unchanged.
- `frontend/src/styles/workflow-policy.css` presents stage order as a semantic routing rail with connected numbered nodes, raises prior 9–11 px evidence text, distinguishes capacity shortfall from safety/approval, adds component-container fallbacks and visible focus, and retains the lazy CSS, visual-viewport, 44 px mobile target, and safe-area footer contracts.

Acceptance limits:

- These Workflow Policy changes have not yet been included in a new guarded frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level request sessions, fieldset locking, ARIA, `:has()` styling, and container queries do not prove slow-save behavior, real screen-reader announcements, native select/number input behavior, IME composition in requirement names, modal focus order, native 200% browser zoom, operating-system text scaling, non-zero hardware safe-area behavior, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Artifact Version History redline-desk code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/artifactVersionDiff.js` no longer invents `DRAFT` or `undecided` for missing frozen fields, preserves scalar zero/false values, safely ignores malformed non-array evidence sections, and renders invalid timestamps as `时间未记录` rather than leaking `Invalid Date`.
- `frontend/src/components/ArtifactVersionHistory.jsx` separates list and exact-pair retry tokens, preventing one retry from launching both request classes. Only positive integer versions with `integrity_ok === true` enter selectors; excluded rows remain visible as a count, and exact responses must be complete, integrity-verified, and bound to both selected versions before a diff is rendered.
- Missing status, counts, hashes, summaries, and non-standard Promise errors stay explicit. Loading states are polite statuses, the disclosure owns busy state, decorative icons are hidden, and boolean/zero diff values are no longer rendered as empty.
- The scoped selectors in `frontend/src/styles/artifact-dialog.css` present the nested history as a revision redline desk with a meaningful baseline/comparison gutter, readable 9–12 px evidence text, complete wrapping, component-container fallback, visible focus, and 44 px narrow controls. The main Artifact dialog, evidence review, user decision, governance, and experiment surfaces are unchanged.

Acceptance limits:

- These Artifact Version History changes have not yet been included in a new guarded frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level integrity filters, retry separation, ARIA, container queries, and focus selectors do not prove live API cancellation, slow/stale response ordering, real screen-reader announcements, native select behavior, native 200% browser zoom, operating-system text scaling, non-zero hardware safe-area behavior, large-version rendering performance, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Artifact Evidence Review chain-of-custody code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/artifactEvidenceSources.js` now normalizes candidate, selected-key, review-map, and source-detail inputs before summarizing relations. Non-array collections or array-shaped maps no longer throw or produce character-level evidence relations.
- `frontend/src/components/ArtifactEvidenceReview.jsx` normalizes every incoming collection, reuses `auditDefaults`, and treats only `source_checked`, `corroborated`, or `disputed` as explicitly reviewed. Missing/unknown audits remain pending; malformed counts and versions cannot render as `NaN`, empty versions cannot silently become a recorded citation, and source-usage counts require explicit non-negative integers.
- Target selection is disabled when no target exists, progress uses a bounded polite status, decorative icons are hidden, review notes disable browser autocomplete, source metadata wraps completely, and semantic data attributes expose verification, evidence role, and exact-preview gaps without changing callbacks or confirmation rules.
- The scoped selectors in `frontend/src/styles/artifact-dialog.css` present each source as a chain-of-custody slip with a state-driven left rail. Verified, pending, disputed/counter, and exact-read gaps remain redundant with text/status strips; typography, focus, container fallbacks, and 44 px narrow controls improve without implying factual truth or artifact approval.

Acceptance limits:

- These Artifact Evidence Review changes have not yet been included in a new guarded frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level normalization, ARIA, data-state styling, container queries, and focus selectors do not prove real source-detail latency, focus scrolling, screen-reader announcements, native select/checkbox behavior, IME composition in review notes, native 200% browser zoom, operating-system text scaling, non-zero hardware safe-area behavior, long-list rendering performance, or rendered visual quality. Those remain uncovered until an isolated acceptance run is authorized and recorded.

### Artifact Candidate Governance sealed-dossier code and visual follow-up (source-only, 2026-08-21)

Code-level evidence from this follow-up:

- `frontend/src/candidateGovernance.js` now accepts only explicit non-negative safe integers for governance counts. Empty strings, booleans, fractions, and non-finite values fall back to derived review data or zero instead of being rounded or silently counted.
- A server `ready: true` candidate layer is locally downgraded unless every candidate has a unique ID, positive revision, origin message, and latest-version message. An applicable ready risk layer is downgraded unless every review has a classified disposition, exact/current candidate revisions, a current-or-stale status consistent with those revisions, and a review message. Local downgrade reasons are appended to the visible layer issues.
- `frontend/src/components/ArtifactCandidateGovernance.jsx` uses safe hash compaction, labels the projection as a `条件化首选` rather than a user decision, hides decorative icons from assistive technology, owns its boundary description, and exposes top/layer state for redundant styling. Risk support/challenge/reject remains disposition-only.
- The scoped selectors in `frontend/src/styles/artifact-dialog.css` present lineage and exact-version risk review as two sealed dossiers joined by a real layer bracket, then visually separate the third-layer user decision boundary. Typography, wrapping, container fallbacks, and state rails improve without turning completeness or support into approval or execution authority.

Acceptance limits:

- These Artifact Candidate Governance changes have not yet been included in a new guarded frontend regression, production build, or browser acceptance pass. The earlier 423/423 executed aggregate predates this follow-up and does not validate it.
- Source-level structural downgrades, ARIA, data-state styling, container queries, and safe compaction do not prove compatibility with every historical governance snapshot, real screen-reader narration, native 200% browser zoom, operating-system text scaling, long-list rendering, or rendered visual hierarchy. The existing compatibility rule that absent legacy top-level integrity/safety fields are not treated as explicit failures is unchanged and remains a separate migration/governance boundary.

## Project Readiness engineering preflight refinement (source-only, 2026-08-21)

- **Code-level evidence:** `projectReadiness.js` now keeps non-hash contribution, adapter, port, version, slot, and component identifiers case-sensitive while continuing to canonicalize SHA-256 text. Its display-only projection refuses to expose counts unless the normalized projection is valid and its `blocked` / `gaps_present` / `ready` state agrees with the three gap arrays.
- **Interaction and visual source evidence:** `ProjectReadinessPanel.jsx` now presents the deterministic order `阻断条件 -> 结构缺口 -> 证据缺口`, exposes the exact artifact version and sealed hashes, marks decorative icons as hidden from assistive technology, and gives the binding disclosure an explicit keyboard focus treatment. The scoped CSS turns the surface into an engineering preflight board and collapses its sequence, summary count, gap groups, and binding ledger at narrow widths.
- **Executed baseline boundary:** the latest executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this source batch. No new test run, production build, or rendered browser acceptance was performed for this change, so that baseline is not evidence that these new lines execute successfully.
- **Uncovered human and device boundary:** source inspection does not cover real IME composition, real screen-reader announcements or browse modes, native browser 200% zoom, hardware safe-area behavior, font fallback, or rendered 1440x900 / 320x568 quality. Those require a later controlled browser and assistive-technology pass.
- **Authority boundary:** this preflight remains a zero-provider, zero-market-read, zero-business-write structural projection. It does not authorize deployment, release, execution, ranking, winner claims, or replacement of the user's final decision.

## Candidate Experiment controlled workbench refinement (source-only, 2026-08-21)

- **Code-level evidence:** `candidateExperimentSelectionFingerprint()` now refuses selections outside 2–6 unique exact candidates, while `candidateExperimentControlState()` derives the select, authorize, run, and review phases without creating authorization. The component uses that projection for acknowledgement and run eligibility, cancels an obsolete coordinated request when selection semantics change, and handles non-`Error` request failures without throwing from its own error path.
- **Interaction and visual source evidence:** the candidate experiment now exposes a four-stage workflow, explicit selection order and count, a sealed cohort header, a compact arm index, and a keyboard-focusable contained comparison region. `candidate-experiment.css` is imported only by the candidate experiment component, adds a controlled-laboratory visual language, keeps the first comparison dimension sticky, and collapses selection, metadata, arm index, and workflow layouts at 760px and 420px.
- **Executed baseline boundary:** the last executed frontend result remains 79 files / 423 pass / 0 fail and predates this source batch. No fresh test run, production build, or browser rendering was performed, so it is not execution evidence for the new control projection or stylesheet.
- **Uncovered human and device boundary:** source contracts do not prove real IME composition behavior, screen-reader announcements, keyboard scrolling in target browsers, native 200% zoom, hardware safe-area behavior, or rendered 1440x900 / 320x568 quality. Those remain for controlled browser and assistive-technology acceptance.
- **Authority boundary:** the workbench preserves selection order and produces no ranking, winner, future-performance claim, transaction instruction, artifact support, or user decision. One historical market-data read remains distinct from provider calls and all execution authority remains absent.

## Artifact ledger and evidence entry refinement (source-only, 2026-08-21)

- **Code-level evidence:** `artifactPanelView.js` now accepts only unique enabled Provider-bound synthesizers, counts only array-backed artifact sections, validates the recorded preferred option against the exact option set, normalizes audit counts, and exposes the five-row display boundary. `evidenceUi.js` rejects negative, fractional, and non-finite relation counts and derives one calibrated frozen-source notice instead of rendering overlapping incomplete-preview warnings.
- **Interaction and visual source evidence:** `ArtifactPanel.jsx` now exposes a structured artifact ledger with generation state, version status, governance and decision badges, scannable metrics, and an explicit hidden-row count. The evidence strip is a labelled relation list, while the source dossier exposes load/retry status, frozen-round boundary text, keyboard-focusable wrapped preview content, and an external-window label. Dedicated scoped styles collapse at 720px, 520px, and 420px without relying on the Artifact Dialog stylesheet.
- **Executed baseline boundary:** the last executed frontend result remains 79 files / 423 pass / 0 fail and predates this source batch. The two new test files were authored but not run; no production build or rendered browser acceptance was performed.
- **Uncovered human and device boundary:** code and source contracts do not prove real screen-reader list/detail announcements, keyboard behavior in target browsers, IME composition elsewhere in the dialog, native 200% zoom, hardware safe-area behavior, or rendered 1440x900 / 320x568 quality.
- **Authority boundary:** artifact generation remains a draft-organizing request, evidence links are never auto-promoted to verified truth, and the ledger creates no approval, provider authority, ranking, winner claim, user decision, or execution permission.

## Provider routing local-preflight refinement (source-only, 2026-08-21)

- **Code-level evidence:** `normalizeProviderPreflight()` now treats malformed checks, duplicate routes, unknown status, invalid member/latency counts, missing or non-local verification scope, and missing/non-zero external-call counts as fail-closed issues. Null, blank, boolean, fractional, negative, non-finite, and unsafe integer counts are rejected rather than coerced to zero; duplicate semantic routes retain unique rendering keys while still failing integrity. A successful state requires at least one explicit ready route, `verification_scope=local_configuration_only`, `external_call_count=0`, and no normalization issue. `buildProviderRoutingPresentation()` centralizes catalog, route statistics, warnings, preflight rows, and the unchanged DeepSeek/豆包 bulk-route allowlist.
- **Async and interaction source evidence:** `ProviderRoutingPanel.jsx` now uses a room-bound operation epoch so a completed action from an obsolete room cannot overwrite the current panel, handles non-`Error` rejections safely, disables unavailable handlers, and distinguishes assigned, configured, locally checked, policy-disabled, and unassigned states. No request is issued by rendering the panel.
- **Visual source evidence:** `provider-routing.css` adds a scoped local route-control console with route statistics, availability chips, a two-column Provider catalog, explicit scope/external-call contract cells, preflight rows, action grouping, focus treatment, and 760px/460px responsive collapse.
- **Executed baseline boundary:** the latest executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this source batch. Updated provider-routing tests were authored but not run; no production build, Provider call, or browser acceptance was performed.
- **Uncovered boundary:** source evidence does not prove backend response compatibility, rendered desktop/mobile quality, keyboard and screen-reader behavior, native 200% zoom, or real connectivity. A local configuration pass never establishes Provider reachability and never authorizes a formal round.

## Chat timeline collaboration-log refinement (source-only, 2026-08-21)

- **Code-level evidence:** `chatTimelineView.js` now normalizes optional arrays and strings, rejects unsafe avatar CSS values, formats invalid timestamps as unknown, creates unique keys for duplicate or missing identities, inserts deterministic day dividers, and trims search queries before switching the timeline into search mode. Malformed citations and transient errors are omitted instead of reaching JSX.
- **Scroll and interaction source evidence:** `ChatTimeline.jsx` captures `scrollHeight` and `scrollTop` before loading older history, restores the reader's anchor in `useLayoutEffect`, and suppresses the false “new message below” state caused by prepended history. The timeline is keyboard-focusable; search, history, retry/error, typing, citation, reply, audit, and live-announcement paths retain distinct semantics.
- **Visual source evidence:** `chat-timeline.css` adds a scoped collaboration log with a masthead, command toolbar, date ledger, thread line, differentiated user/member messages, evidence chips, audit disclosure, empty-state dossier, contained scrolling, visible focus, and 700px/430px responsive layouts.
- **Executed baseline boundary:** the latest executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this source batch. The new timeline tests were authored but not run; no production build or rendered browser acceptance was performed.
- **Uncovered boundary:** source contracts do not prove real scroll anchoring in Chromium, screen-reader feed navigation, IME interaction with search/composer, native 200% zoom, hardware safe-area behavior, or rendered 1440x900 / 320x568 quality. Message rendering and search do not create evidence truth, Provider authority, or execution permission.

## Capability-pack lifecycle change-control refinement (source-only, 2026-08-21)

- **Code-level evidence:** `pluginLifecycle.js` now refuses transition payloads unless the normalized preview is intact and still matches the exact target, action, lifecycle head, preview SHA-256, namespaced client request ID, and 4–500 character reason. Client request IDs require `crypto.randomUUID()`; an environment without a cryptographically secure UUID source fails closed instead of falling back to `Math.random`.
- **Interaction and visual source evidence:** `pluginLifecycleUi.js` centralizes deterministic target identity, catalog totals, action framing, and the submit permit projection. `CapabilityPackLifecyclePanel.jsx` applies one operation epoch to preview and transition responses, aborts obsolete previews, ignores stale completions, guards absent handlers, handles non-`Error` failures, moves review focus to its labelled heading, and exposes busy, error, exact-version, history-preservation, no-migration, and tombstone-confirmation states. The dedicated stylesheet presents the surface as a global change-control ledger with a sealed impact sheet, explicit permit checklist, state rails, 760px/440px collapse, 44px narrow controls, focus rings, and reduced-motion handling.
- **Executed baseline boundary:** the latest executed frontend result remains 79 files / 423 pass / 0 fail and predates this source batch. The updated lifecycle test and new UI-projection/source-contract test were authored but not run; no production build, lifecycle request, or rendered browser acceptance was performed.
- **Uncovered human and device boundary:** source checks do not prove real service response compatibility, stale-response timing under live latency, browser focus scrolling, screen-reader announcements, checkbox narration, IME composition in the reason or tombstone fields, native 200% zoom, operating-system text scaling, non-zero hardware safe-area behavior, or rendered 1440x900 / 320x568 quality.
- **Authority boundary:** preview and transition remain explicit user-triggered lifecycle governance operations only. The console cannot delete history, auto-migrate rooms, replace a user decision, call a Provider, read markets, or create deployment, release, trading, or execution authority.

## Capability Registry frozen-dossier refinement (source-only, 2026-08-21)

- **Code-level evidence:** `capabilityRegistryUi.js` now rejects non-array, blank, non-text, and duplicate capability-pack selections rather than spreading strings or silently deduplicating malformed input. It derives frozen-contract trust separately from current lifecycle verification, so an auditable room snapshot cannot be presented as currently available for new bindings when the lifecycle catalog or an exact target is unavailable.
- **Interaction and visual source evidence:** `CapabilityRegistrySnapshot.jsx` now exposes the frozen registry version/hash, capability/adaptor/contribution totals, exact manifest and contract hashes, adapter ports, host slots, pending-selection drift, lifecycle uncertainty, and the fixed safety boundary as a labelled dossier. Decorative icons are hidden from assistive technology and expandable exact-port disclosures retain visible keyboard focus. `capability-registry.css` owns the blueprint-like visual surface, state rails, redundant labels, 620px/420px container collapse, 44px narrow disclosures, and reduced-motion rule.
- **Performance source evidence:** the legacy `.plugin-registry-*` block was removed from eager `styles.css`; the registry component now imports its scoped stylesheet directly. This establishes ownership for chunking but does not by itself prove the final production chunk graph or transferred-byte reduction.
- **Executed baseline boundary:** the latest executed frontend result remains 79 files / 423 pass / 0 fail and predates this source batch. The new projection/source-contract test was authored but not run; no production build or rendered browser acceptance was performed.
- **Uncovered human and device boundary:** source checks do not prove compatibility with every historical registry snapshot, live lifecycle drift, screen-reader reading order, native details narration, browser focus scrolling, native 200% zoom, operating-system text scaling, non-zero hardware safe-area behavior, or rendered 1440x900 / 320x568 quality.
- **Authority boundary:** the dossier is read-only. It cannot install code, load unknown components, mutate lifecycle state, save room settings, call a Provider, read markets, create a user decision, or authorize release, deployment, trading, or execution.

## Decision Lineage provenance-ledger refinement (source-only, 2026-08-21)

- **Code-level evidence:** `decisionLineageView.js` now owns source projection, exact resource revisions, portfolio-version parsing, lineage indexing, deterministic package keys, collection normalization, and panel summaries. Non-array inputs, malformed rows, duplicate identities, invalid run maps, booleans, blanks, fractions, and unsafe integers no longer become iterable characters or coerced versions. Any model issue closes further derivation while preserving audit rendering.
- **Async and interaction source evidence:** AI proposal binding now binds completion to a source/proposal epoch, ignores stale responses after source drift or unmount, handles non-`Error` rejections, exposes `aria-busy`, disables mutable fields while busy, and labels errors as alerts. Missing portfolio or observation handlers no longer leave a clickable path that throws; exact launch-trigger callbacks remain unchanged for focus restoration.
- **Visual source evidence:** `DecisionLineagePanel.jsx` presents a provenance ledger with current/history/broken/unlinked counters, explicit decision-to-simulation flow, separate forward-observation and historical-replay lanes, redundant package/node states, and a visible fail-closed model warning. Decorative icons are hidden from assistive technology. `decision-lineage.css` owns the paper-ledger surface, 720px/440px container collapse, wrapped evidence text, focus rings, 44px narrow controls, and reduced-motion behavior.
- **Performance source evidence:** the old `.decision-lineage-*` block was removed from eager `styles.css`, and `PaperPortfolioPanel.jsx` now imports pure lineage helpers from `decisionLineageView.js` rather than importing the full Decision Lineage UI module. Source ownership and dependency direction are improved, but production chunk or byte effects remain unproven until a fresh build.
- **Executed baseline boundary:** the latest executed frontend result remains 79 files / 423 pass / 0 fail and predates this source batch. The new pure-projection/source-contract test was authored but not run; no production build, rendered browser acceptance, or proposal/portfolio/observation request was performed.
- **Uncovered human and device boundary:** source checks do not prove compatibility with every historical lineage package, real stale-response timing, focus behavior after nested dialogs, screen-reader flow narration, native details/select behavior, IME composition in derivation notes, native 200% zoom, hardware safe-area behavior, or rendered 1440x900 / 320x568 quality.
- **Authority boundary:** the ledger remains research-only. Support, simulation, observation, and replay states do not establish profitability, evidence truth, an autonomous decision, a user decision, release approval, trading authority, or live execution permission.

## Round Launch authorization-console refinement (source-only, 2026-08-21)

- **Code-level evidence:** `roundLaunchUi.js` now owns Provider/blocker labels, strict decimal call-limit parsing, initial-limit selection, safe error/hash display, and the five-part submit permit projection. Blank, boolean, fractional, exponent, and unsafe numeric forms no longer reach authorization through generic `Number()` coercion. Backend plan normalization, blockers, hashes, and authorization payload construction remain the authority source.
- **Async and modal source evidence:** `RoundLaunchDialog.jsx` binds each submit completion to the current plan hash, client request ID, and open epoch; stale success/error/finally paths are ignored after plan drift, closure, or unmount. Missing handlers and non-`Error` rejections fail visibly. The shared `useModalFocus` contract, exact restore target, busy Escape/backdrop lock, and existing launch callbacks are unchanged.
- **Visual source evidence:** the dialog is now a controlled authorization console with a sealed masthead, explicit five-item launch permit, separated frozen context cards, call-budget ledger, route states, sticky header/footer, visible focus rings, safe-area padding, 520px container collapse, 560px viewport collapse, 44px narrow controls, and reduced-motion handling. Project, football, stock, Provider-total, moderator-sub-budget, and execution-boundary text remains explicit.
- **Performance source evidence:** roughly 200 lines of component inline-style data and the dedicated eager `.round-launch-*` block were replaced by `round-launch.css`, imported by the already-lazy Round Launch component. Generic `.dialog` rules and the shared reduced-motion selector remain global. Actual chunk and transferred-byte changes remain unproven until a production build.
- **Executed baseline boundary:** the latest executed frontend result remains 79 files / 423 pass / 0 fail and predates this source batch. The new pure-projection/source-contract test and updated mobile viewport contract were authored but not run; no production build, rendered browser acceptance, Provider preflight, market read, or round launch occurred.
- **Uncovered human and device boundary:** source checks do not prove native number-input behavior, IME interaction, real plan drift during submission, focus/scroll behavior with a software keyboard, screen-reader announcement order, native 200% zoom, non-zero physical safe-area geometry, or rendered 1440x900 / 320x568 quality.
- **Authority boundary:** this console only displays and submits an explicit, bounded authorization payload. It does not guarantee workflow completion, call counts, research validity, profitability, a user decision, release approval, trading authority, or live execution permission.

## Room Settings configuration-folio refinement (source-only, 2026-08-21)

- **Code-level evidence:** `roomSettingsUi.js` now projects room data, capability-pack selections, version history, error text, and the save permit through fail-closed helpers. String-backed or duplicate pack IDs no longer become an apparently valid selection, and malformed history rows are excluded while retaining an integrity warning.
- **Async and modal boundary:** the settings dialog now owns a save-request epoch in addition to its existing history epochs. A room/version/open transition invalidates stale save completion, close is kept outside the save-error classification, backdrop dismissal only accepts the backdrop itself, and the existing focus-trap contract remains unchanged.
- **Visual and responsive source:** `styles/room-settings.css` owns a warm configuration-folio surface, sticky header/footer, status ledger, 44px narrow controls, container breakpoints, safe-area-aware height, visible focus, and reduced-motion handling. Exclusive Room Settings selectors were removed from the global sheet; shared capability, stock-scope, and material-version primitives remain global.
- **Performance evidence boundary:** the new projection is linear over the small member, pack, and version collections and adds no network request, observer, timer, or runtime dependency. This is source-level reasoning only; no profiler trace or post-change bundle measurement was produced in this batch.
- **Executed baseline:** the last executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this Room Settings source change. `frontend/tests/roomSettingsUi.test.js` was authored for malformed inputs, strict permits, history integrity, style ownership, save epoch, safe area, and reduced-motion contracts, but was not run in this batch.
- **Uncovered boundary:** real IME composition, native 200% zoom, screen-reader announcement order, focus restoration across failed/successful saves, visualViewport keyboard movement, safe-area hardware, and browser console behavior remain unverified by a real browser/device pass.
- **Authority boundary:** a local save permit only describes client-side form readiness. It does not authorize provider access, migration apply, formal database writes outside the explicitly selected runtime, trading activity, or public release.

## Workflow Policy blueprint refinement (source-only, 2026-08-21)

- **Code-level evidence:** `workflowPolicyUi.js` now separates raw-policy integrity, editable-draft validation, non-Error request messages, and the local save permit. Malformed arrays, duplicate identifiers, invalid limits, and attempts to weaken confirmation/execution/live-trading locks are surfaced instead of being silently presented as pristine source data; the normalized draft continues to force the permanent safety values.
- **Async and modal boundary:** submission uses the existing room/open request session but now snapshots validated handlers, resets `busy` in the current-session `finally`, and invokes close only after the save error boundary. A missing or throwing close handler is no longer classified as a failed persistence request, and a successful save cannot leave the dialog permanently busy merely because the host keeps it open.
- **Visual and responsive source:** the owned blueprint stylesheet now adds a stage/coverage/member/save ledger, explicit ready/blocked frame states, repaired-source warning, safe-area-aware visualViewport sizing, single-column narrow layout, and reduced-motion handling while preserving 44px mobile controls and visible focus.
- **Performance evidence boundary:** policy integrity and permit derivation are synchronous linear passes over bounded stage and coverage collections. The UI adds no request, observer, timer, runtime package, or eager global stylesheet; no post-change profiler or bundle measurement was executed.
- **Executed baseline:** the last executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this Workflow Policy change. The existing `workflowPolicyDialog.test.js` now includes source-integrity, permanent-lock, save-permit, close-boundary, safe-area, ledger, and reduced-motion contracts, but was not run in this batch.
- **Uncovered boundary:** real IME input, screen-reader reading order, native 200% zoom, visualViewport keyboard movement, safe-area hardware, focus restoration after success/failure, and browser console behavior remain unverified.
- **Authority boundary:** Workflow Policy configures research discussion coverage only. It cannot weaken user confirmation, grant execution capability, permit live trading, authorize providers, apply formal migrations, or approve public release.

## Reflection provenance-notebook refinement (source-only, 2026-08-21)

- **Code-level evidence:** `reflectionDialogUi.js` now projects display-safe source data, form submissions, change detection, non-Error messages, and separate draft/confirmation permits. Confirmation requires a stable reflection ID, source observation ID, positive record version, non-empty frozen snapshot, and a 64-hex audit fingerprint; incomplete provenance may still preserve a draft but cannot enter confirmed memory.
- **Async and modal boundary:** one in-flight ref now closes the pre-render double-submit window. Save, confirm, and close are separate error domains: confirmation failure states that the draft was saved, close failure states which operation completed, and current-session `finally` always releases busy state. Existing initialized-surface focus ownership and restore target remain intact.
- **Visual and responsive source:** the owned reflection notebook adds draft/confirmation permit cells, provenance warning, safe-area-aware visualViewport sizing, 320px single-column permit layout, paper texture, visible focus, 44px mobile actions, and reduced-motion handling.
- **Performance evidence boundary:** all new projections are bounded synchronous passes over one reflection and three text fields. No request, observer, timer, package, eager stylesheet, or formal runtime connection was added; no profiler or bundle measurement was executed.
- **Executed baseline:** the last executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this Reflection change. `observationReflectionFocus.test.js` now carries provenance, dual-permit, phased-error, in-flight, style, safe-area, narrow-layout, and reduced-motion contracts, but was not run in this batch.
- **Uncovered boundary:** real IME composition, screen-reader announcement order, native 200% zoom, visualViewport keyboard movement, safe-area hardware, restored focus after each phase, and browser console behavior remain unverified.
- **Authority boundary:** confirmed reflection is provenance-bound discussion memory, not statistical proof, causal truth, provider authorization, migration approval, execution capability, live-trading permission, or public-release authorization.

## Member Version evidence-archive refinement (source-only, 2026-08-21)

- **Code-level evidence:** `memberVersionHistoryUi.js` now validates room/member identity, list response shape, record objects, positive versions, duplicate versions, integrity flags, and exact pair response versions. A malformed `versions` payload becomes a read error rather than a false empty history; duplicate or abnormal rows stay visible in the ledger but cannot enter precise selectors.
- **Async and modal boundary:** existing cancellable list/pair effects remain request-neutral, while exact pair projection binds both responses to the requested versions. Selectors lock during list or pair reads, error text accepts Error/string/unknown values safely, member labels are display-projected, and every dismissal path tolerates an absent host callback.
- **Visual and responsive source:** the owned evidence-archive stylesheet now adds list/pair status cells, integrity warning, ready/error frame states, safe-area-aware visualViewport sizing, 320px two-column status collapse, visible focus, 44px mobile controls, and reduced-motion handling.
- **Performance evidence boundary:** list projection is linear over version rows, with a small version-count map; pair projection is constant-time. No request, observer, timer, runtime dependency, eager stylesheet, rollback path, or mutation capability was added, and no profiler or bundle measurement was executed.
- **Executed baseline:** the last executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this Member Version change. `memberVersionHistoryDialog.test.js` now includes malformed list, duplicate exclusion, pair-version binding, optional close, loading lock, status, safe-area, and reduced-motion contracts, but was not run in this batch.
- **Uncovered boundary:** real screen-reader traversal, native 200% zoom, visualViewport keyboard movement, safe-area hardware, very long diff content, focus restoration, request timing, and browser console behavior remain unverified.
- **Authority boundary:** the archive remains read-only evidence. Loading or comparing snapshots cannot restore an identity, mutate membership, change lifecycle, authorize a provider, apply a migration, grant execution/trading capability, or approve public release.

## Round Execution flight-recorder refinement (source-only, 2026-08-21)

- **Code-level evidence:** `roundExecutionTraceDialogUi.js` re-normalizes every supplied trace through the existing fail-closed `normalizeRoundExecutionTrace`, projects external errors and round copy to display-safe text, and exposes a bounded event window. A caller-provided `valid: true` cannot bypass version/hash/identity/safety normalization.
- **Modal and callback boundary:** the dialog now uses shared `useModalFocus` instead of a second handwritten Tab/Escape implementation. Close remains available during read-only loads; retry, refresh, and server pagination buttons explicitly disable when their host callback is absent rather than presenting a false action.
- **Performance source:** trace normalization is memoized by the supplied trace object; at most 100 already-loaded events mount initially, expanding in explicit 100-row local steps while summary, integrity, and loaded/total counts continue to represent the complete normalized page set. The window resets across close/reopen and trace-hash changes, and existing `content-visibility` remains active on mobile. No event is deleted and server pagination remains a separate action.
- **Visual and responsive source:** the owned flight-recorder stylesheet adds render-window status, local reveal action, ready/invalid frame states, safe-area-aware visualViewport sizing, 44px mobile actions, narrow trace layouts, visible focus, and reduced-motion handling.
- **Executed baseline:** the last executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this Round Execution Trace change. `roundExecutionTrace.test.js` now includes raw-valid bypass, display safety, 230-event windowing, shared focus, local reveal, callback, safe-area, and reduced-motion contracts, but was not run in this batch.
- **Uncovered boundary:** real screen-reader traversal through windowed events, native 200% zoom, visualViewport keyboard movement, safe-area hardware, focus restoration, 20,000-event interaction, request timing, and browser console behavior remain unverified.
- **Authority boundary:** the flight recorder remains a normalized read-only projection. Rendering, revealing, retrying, or paginating records performs no Provider call itself and grants no execution, autonomous-decision, trading, migration, or release authority.

## Artifact evidence-docket refinement (source-only, 2026-08-21)

- **Code-level evidence:** `artifactEditorUi.js` now projects artifact records, content, identity, save responses, Error/string failures, and separate progress/draft/confirm/export permits. Non-array sections and non-text title/summary values no longer crash editor initialization or render as objects; malformed source is exposed as a repaired draft rather than silently labelled pristine.
- **Async and mutation boundary:** one request epoch plus an imperative in-flight lock closes pre-render double submission and invalidates completion after close. Save-progress verifies returned artifact identity and non-regressing version before replacing editor state; all four operation handlers are required explicitly and failures remain local instead of becoming unhandled event promises.
- **Interaction boundary:** the complete editable evidence docket sits inside a busy-disabled fieldset, while dismissal stays fail-closed during any operation. Evidence-source loading, identity mismatch, exact-source gaps, server confirmation issues, candidate governance, plugin read-only contracts, and backend confirmation remain independent existing gates.
- **Visual and responsive source:** the owned docket stylesheet adds three-operation permit cells, repaired-source and mutation-error notices, operation frame states, safe-area-aware visualViewport sizing, a 320px single-column permit ledger, 44px mobile actions inherited from the existing footer, visible focus, and reduced-motion handling.
- **Performance evidence boundary:** source and permit projections are bounded linear passes over the existing artifact section collections; no request, observer, timer, runtime dependency, eager stylesheet, Provider call, or new confirmation computation was added. No profiler or bundle measurement was executed.
- **Executed baseline:** the last executed frontend baseline remains 79 files / 423 pass / 0 fail and predates this Artifact Editor change. `artifactEditorUi.test.js` and updated focus contracts cover malformed sections, handler separation, double-submit state, save-response identity/version, busy fieldset, safe area, and reduced motion, but were not run in this batch.
- **Uncovered boundary:** real IME input, screen-reader traversal of the large docket, native 200% zoom, visualViewport keyboard movement, safe-area hardware, focus restoration after each operation, long artifact performance, request timing, download behavior, and browser console behavior remain unverified.
- **Authority boundary:** an editor permit is only a client-side precondition. It cannot bypass authoritative evidence, server confirmation, user-final-decision, plugin lifecycle, migration, Provider, execution, trading, or public-release gates.

## Artifact evidence-ledger refinement (source-only, 2026-08-21)

- Code-level evidence: `artifactEvidenceGraph.js` now rejects non-scalar text and integer projections, stops before traversing node/edge/event collections above the declared caps, normalizes every node/edge/review-event field rendered by the ledger, and fails closed on duplicate review-event hashes.
- Code-level evidence: the active-target summary now returns an explicit empty projection when no target is selected instead of repeating whole-graph totals. The component resets filters and pagination when the saved artifact identity changes, exposes pressed filter state, disables unavailable target scope, and announces visible/result totals.
- Visual source evidence: `artifact-evidence-graph-refinement.css` adds a scoped evidence-ledger treatment, visible focus rings, narrow-screen filter scrolling, mobile metric stacking, forced-colors borders, and reduced-motion handling without changing global artifact styles.
- Test-source evidence: `artifactEvidenceGraph.test.js` covers malformed display metadata, explicit empty active-target summaries, collection caps, and duplicate review hashes. These tests were authored but not executed in this continuation.
- Uncovered boundary: source inspection does not establish real keyboard/voice-control usability, screen-reader announcement quality, browser rendering at 1440x900/320x568/effective 200%, or production bundle impact. The ledger trusts the backend's authoritative cryptographic verification flag after checking schema, identity, limits, and hash shape; it does not independently recompute the server's hash chain.

## Artifact evidence-review workbench refinement (source-only, 2026-08-21)

- Code-level evidence: authoritative source envelopes now fail closed before normalizing more than 2,000 combined source/gap rows. Scalar cleaners reject objects and arrays instead of leaking `[object Object]` into identity or display fields.
- Code-level evidence: `artifactEvidenceReviewUi.js` deduplicates selected keys, source keys, and target keys; bounds candidate/selection/target projections; sanitizes every review-row display field; and synthesizes a non-selectable row for each saved binding missing from the current candidate response so the user can still remove it.
- Interaction source evidence: target, bind, audit, version-decision, and next-unreviewed controls now reflect handler availability. Candidate rendering starts at 80 rows, keeps every selected row visible outside the page window, and exposes explicit displayed/total counts and an incremental reveal control.
- Visual source evidence: the scoped review-workbench stylesheet adds a three-column audit ledger, source-integrity warnings, visible focus-within treatment, mobile stacking, forced-colors borders, and reduced-motion handling without changing global evidence styles.
- Test-source evidence: `artifactEvidenceReviewUi.test.js` covers safe labels, duplicate keys, missing selected-source recovery, envelope limits, handler-permit source contracts, pagination, and responsive CSS markers. These tests were authored but not executed in this continuation.
- Uncovered boundary: source evidence does not prove real keyboard/voice-control behavior, screen-reader verbosity and announcement order, IME composition inside review notes, 1440x900/320x568/effective-200% layout, or production bundle impact. The 2,000-source and 500-target limits are defensive UI/API ingestion limits, not proof that backend query cardinality is optimal.

## Candidate governance ledger refinement (source-only, 2026-08-21)

- Code-level evidence: candidate governance now bounds projection JSON, candidates, reviews, per-review risk IDs, issues, and rendered text. Non-scalar identity/display fields no longer become `[object Object]`, and oversized collections fail closed before full projection.
- Integrity source evidence: readiness is now derived from actual unique lineage candidates and actual review records. Current/stale/coverage/disposition counts are recomputed, declared counts must match, current reviews must point to a lineage candidate's exact current revision/message, duplicate current opinions and review-message IDs fail closed, and malformed attestation/snapshot SHA-256 shapes invalidate integrity.
- Safety source evidence: the no-execution boundary is checked at snapshot, projection, lineage, and risk-review layers. Risk dispositions remain read-only labels and cannot become a user decision or execution authorization.
- Visual source evidence: `candidate-governance-refinement.css` adds a scoped three-metric governance ledger, layer-state rails, progressive 40-candidate/60-review rendering, collapsible issue dossiers, mobile stacking, visible focus, forced-colors borders, and reduced-motion handling.
- Test-source evidence: `candidateGovernance.test.js` now covers declared-count drift, lineage-external/wrong-version reviews, collection limits, malformed attestation hashes, pagination, and responsive CSS markers. These tests were authored but not executed in this continuation.
- Uncovered boundary: source inspection does not prove backend cryptographic attestation, real screen-reader navigation through collapsed issue dossiers, keyboard behavior after progressive expansion, 1440x900/320x568/effective-200% layout, or bundle impact. Client SHA checks validate shape and declared alignment only; they do not recompute the server attestation.

## Candidate experiment controlled-lab refinement (source-only, 2026-08-21)

- Code-level evidence: cohort normalization now rejects over-limit authorization selections, arms, scenarios, per-arm evidence, issue records, and common-spec/dataset-seal fields before expensive projection. Error, blocker, candidate snapshot, evidence, and issue strings are bounded and non-scalar display values fail closed.
- Interaction source evidence: a missing room identity now closes the run gate; the run handler checks the request coordinator's synchronous `inFlight` flag before constructing another request; create-response identity is compared against the exact request payload using strict string/integer types before the mandatory reread.
- Rendering source evidence: exact context serialization is memoized, candidate selection starts at 24 rows while preserving every selected candidate outside the page window, and visible/total counts are announced. This follows the existing create-then-reread and latest-request cancellation contracts rather than adding another request state machine.
- Visual source evidence: the scoped refinement stylesheet adds a neutral three-cell result ledger, candidate-list progress treatment, focus-visible outlines, mobile stacking, forced-colors borders, and reduced-motion handling. It does not introduce ranking, winner emphasis, or result-based sorting.
- Test-source evidence: `candidateExperiment.test.js` now covers bounded errors, excessive arms/evidence/issues, coordinator in-flight source contracts, room gating, progressive candidate display, and responsive CSS markers. These tests were authored but not executed in this continuation.
- Uncovered boundary: source evidence does not prove actual server atomicity, browser double-click timing, real keyboard/screen-reader behavior, 1440x900/320x568/effective-200% rendering, or bundle impact. Client hash checks validate shapes and cross-field equality; they do not recompute server cohort, dataset, spec, or aggregate hashes.

## Candidate comparison review-bench refinement (source-only, 2026-08-21)

- Code-level evidence: eligible-run collection now validates portfolio groups, per-portfolio runs, and total runs before sorting, returning an explicit failed-closed eligibility state on overflow. IDs and labels are bounded, numeric identity fields use strict JSON numbers, and the compatibility helper still returns only the safe run list.
- Response source evidence: candidate and selected-run collections are checked against the 2–6 contract before mapping; scenario arrays must be exactly the three declared friction cases; string issues now block rather than disappearing; issue text is bounded and deduplicated. Any failure hides all historical metrics.
- Interaction source evidence: selection identity uses JSON rather than delimiter joins, eliminating fingerprint collisions. The compare action requires a callable handler, holds a local ref lock across the parent Promise, copies selected IDs before submission, and keeps stale responses hidden when selection changes.
- Visual source evidence: the selector is now a semantic fieldset with 18-row progressive display, selected/visible counts, a neutral result ledger, keyboard-focusable horizontal table region, mobile stacking, forced-colors borders, and reduced-motion handling. Results remain in server/request order and are never sorted by return.
- Test-source evidence: `candidateComparisonView.test.js` now covers collection caps, delimiter-safe fingerprints, bounded errors, excessive candidates/scenarios, string issues, submission locking, progressive display, and responsive CSS markers. These tests were authored but not executed in this continuation.
- Uncovered boundary: source evidence does not prove parent-request behavior under real rapid clicks, browser table scrolling, screen-reader fieldset/table narration, 1440x900/320x568/effective-200% rendering, or bundle impact. Client SHA checks compare declared shapes and identities; they do not recompute server comparison or basis hashes.

## Project readiness implementation-docket refinement (source-only, 2026-08-21)

- Code-level evidence: readiness contract strings now reject non-string or over-limit values rather than coercing/truncating identities; gap code/item/message limits are 120/240/1,000 characters. Frozen contribution, pack, adapter, and port-resolution collections are checked before host resolution traverses them.
- Request source evidence: the read-only GET identity now uses JSON serialization instead of delimiter joins, preserving room/artifact IDs that contain separator characters. API error messages use a separate bounded display projection and never enter React as objects.
- Rendering source evidence: presentation is memoized by validated projection. Each blocker/structure/evidence group initially renders 40 rows, reports visible/total counts, preserves server order, and expands incrementally without changing readiness state.
- Visual source evidence: the scoped implementation-docket stylesheet adds state rails, progressive-list status treatment, visible focus, mobile binding stacking, forced-colors borders, and reduced-motion handling. Copy continues to distinguish contract-clear from deployment, release, execution, or approval.
- Test-source evidence: `projectReadiness.test.js` now covers delimiter-safe request identity, over-limit contract text failure, bounded errors, progressive row source contracts, and responsive CSS markers. These tests were authored but not executed in this continuation.
- Uncovered boundary: source evidence does not prove the backend projection's semantic completeness, real GET cancellation timing, screen-reader traversal of long gap groups, 1440x900/320x568/effective-200% rendering, or bundle impact. A clear deterministic contract remains engineering evidence only, not deployment or release authorization.

## Artifact ledger workbench refinement (source-only, 2026-08-21)

- Rendering source evidence: `artifactPanelRows()` now projects only the visible 5–20 item window while retaining the true total count, instead of normalizing every artifact before slicing. A single artifact section above 5,000 rows skips evidence traversal and displays unavailable summary metrics rather than misleading zeroes.
- Control source evidence: member selectors fail closed above 500 records; member IDs/provider/model and artifact identities are bounded; display titles are separately truncated. Generate/edit controls require callable handlers, and generation holds a local ref lock across the parent Promise.
- Interaction source evidence: the ledger starts with five artifacts and expands by five up to the existing 20-item view boundary. Control-state, visible-count, and synthesizer-count cells make routing and list scope explicit; unavailable handlers and local errors are rendered as bounded messages.
- Visual source evidence: the scoped artifact-ledger stylesheet adds a three-cell control ledger, disabled-row treatment, visible focus, mobile stacking, forced-colors borders, and reduced-motion handling while preserving the existing status/governance/user-decision badge hierarchy.
- Test-source evidence: `artifactPanelView.test.js` now covers visible-window-only evidence summarization, oversized artifact sections, member limits, missing handlers, generation locking, progressive rows, and responsive CSS markers. These tests were authored but not executed in this continuation.
- Uncovered boundary: source evidence does not prove parent generation idempotency, real rapid-click behavior, screen-reader badge verbosity, 1440x900/320x568/effective-200% rendering, or bundle impact. A generated/confirmed artifact and governance badge remain engineering records only, not release or execution authorization.

## Chat timeline bounded projection and control refinement (source-only, 2026-08-21)

This continuation hardens the main discussion timeline without changing any provider, database, or execution boundary. The projection helper now caps messages, director decisions, member indexing, citations, transient errors, identity fields, error text, and rendered message content before expensive sorting or repeated mapping. The newest bounded message and decision windows remain available, omitted counts are surfaced explicitly, duplicate message identities no longer create duplicate DOM ids, and long content or citation collections are disclosed rather than silently presented as complete.

At the component level, history loading, search submission, search clearing, and search pagination share a synchronous ref gate around parent Promises. Missing handlers disable their controls rather than leaving no-op interactions. The visible integrity ledger distinguishes projected records from source records and indexed members, while local action failures and active projection limits use bounded live status or alert text. The dedicated stylesheet adds safe-area offsets, narrow-screen type and target sizing, forced-colors treatment, and reduced-motion scroll behavior.

Evidence in this section is code-level only: helper and source-contract tests were updated but were not executed in this continuation. No new production build, browser runtime, console capture, effective-200-percent viewport pass, real IME composition session, screen-reader announcement/focus traversal, or assistive-technology verification was performed. These source changes therefore do not replace the earlier measured regression/build baseline and do not establish real IME or screen-reader compatibility.

## Room inspector bounded command rail (source-only, 2026-08-21)

The room inspector continuation introduces a dedicated pure projection layer for active and archived members, providers, shared materials, current-round director decisions, artifact fingerprints, workflow stages and coverage, and walk-forward history detection. Hard caps are applied before local sorting, indexing, fingerprint construction, or list rendering. Provider lookup is now map-based rather than a provider scan per member, artifact fingerprints use deterministic JSON tuples instead of delimiter-joined strings, and an over-limit walk-forward registry conservatively keeps the historical workspace visible instead of claiming that history is absent.

The command rail now progressively reveals member, archived-member, and material rows, reports projected/source counts, rejects non-hex avatar backgrounds, bounds locally rendered identity and error text, and disables controls whose handlers are unavailable. Start, pause, resume, and end actions share a synchronous Promise gate with a bounded local error channel. Optional decision-lineage, market-snapshot, and storage-acceptance panels now sit behind nested lazy/Suspense boundaries, preserving their host-owned read-only wrappers while reducing unconditional RoomInspector dependency loading.

Evidence remains source-level only. Pure-helper and source-contract tests were added or updated but were not executed in this continuation, and no production build was run to measure the resulting RoomInspector chunk split. No browser console pass, narrow/effective-200-percent viewport run, real IME composition, screen-reader announcement/focus traversal, or forced-colors device session was performed. The code and CSS contracts therefore establish intended bounded behavior and accessibility affordances, not runtime or assistive-technology proof.

## 2026-08-21 guarded regression, production build, and isolated browser acceptance

### Guarded regression and production build

- The frontend regression was run through the README entry point, `AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test`. This used `frontend/scripts/run-tests-safe.ps1`; it did not invoke `node --test` directly and did not restore the retired top-level Vite/JSDOM unresolved-lazy gate.
- Result: 92 test files, 495 tests, 495 passed, 0 failed, 0 cancelled, 0 skipped, and 0 todo.
- The production build was run with `AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend run build`.
- Vite 6.4.3 transformed 1,722 modules and completed in 8.63 seconds.
- Key emitted assets: main JavaScript 518.28 kB / 158.26 kB gzip; `RoomInspector` JavaScript 89.72 kB / 27.59 kB gzip; `ArtifactDialog` JavaScript 163.07 kB / 48.90 kB gzip; main CSS 148.07 kB / 27.35 kB gzip; `RoomInspector` CSS 22.90 kB / 4.37 kB gzip.
- Measured `dist`: 58 files and 1,556,444 bytes total. JavaScript accounts for 42 files / 1,130,972 bytes, CSS for 15 files / 424,966 bytes, and one other file for 506 bytes.
- At this baseline, the main JavaScript chunk remained above Vite's 500 kB warning threshold. A successful build was therefore not evidence that the remaining bundle-splitting work was complete; the follow-up measurement below supersedes that warning state.

### Vendor-chunk follow-up

- After adding explicit `react-vendor` and `icon-vendor` boundaries, the same production-build entry point transformed 1,722 modules and completed in 2.42 seconds. The elapsed-time difference is observational only because the second build used a warm local environment.
- The main JavaScript chunk decreased from 518.28 kB / 158.26 kB gzip to 364.66 kB / 109.57 kB gzip. Vite emitted no 500 kB chunk warning.
- The extracted chunks measured 134.67 kB / 43.22 kB gzip for React and React DOM, and 42.13 kB / 9.44 kB gzip for Lucide icons.
- The vendor-only build measured 38 files and 1,550,113 bytes total: JavaScript 22 files / 1,124,476 bytes, CSS 15 files / 424,966 bytes, and one other file / 671 bytes.
- The new boundary improves entry-chunk size and independent browser caching. It does not mean that the combined JavaScript required by every user journey fell by the same amount; route-level request and cache-hit measurements remain separate work.

### Isolated browser evidence

- The browser run used `AI_STUDIO_SKIP_LOCAL_ENV=1`, a system-temporary runtime, an explicit temporary SQLite copy, cleared Provider credentials, a loopback Futu sink at `127.0.0.1:9`, and random loopback URL `http://127.0.0.1:50246/`. The formal service was not started and the formal database was not migrated or written.
- After the run, the formal SQLite file remained 5,062,656 bytes with SHA256 `B32E88A0C0BE5DB2D052904221C6C85D1B1C7862FD76F45EB8DF08B7EC41CC05`; its WAL remained 0 bytes and SHM remained 32,768 bytes. The isolated server stderr was empty, its process tree was stopped, and port 50246 had no listener.
- At 1440 x 900, `documentElement` and `body` both reported client/scroll width 1440. `visualViewport` reported 1440 x 900, zero offsets, and scale 1. The 12-member storage-committee mention menu rendered 12 `menuitem` elements in a 248 px-wide bounded scroll region without creating page-level horizontal overflow.
- At 320 x 568, `documentElement` and `body` both reported client/scroll width 320, so no page-level horizontal scroll was created. A later focused pass proved that the timeline still had internal clipping; that finding and its correction are recorded below. `visualViewport` and the runtime CSS variables reported 320 x 568, zero offsets, and scale 1. The 12-member menu rendered all 12 items from x=20 through x=268 and remained vertically scrollable inside the visible viewport.
- At the 720 x 450 CSS viewport used as the effective viewport of a 1440 x 900 display at 200%, `documentElement` and `body` both reported client/scroll width 720. `visualViewport` and its CSS variables reported 720 x 450, zero offsets, and scale 1. The mention menu again rendered all 12 items without page-level horizontal overflow.
- Desktop modal focus was exercised with the member identity-history dialog: opening focus landed on its close control, reverse Tab remained inside the dialog, Escape removed the dialog, and focus returned to the exact history trigger.
- At 320 x 568, `RoomInspector` is itself a dialog and the identity-history dialog is a second, topmost dialog. Focus landed inside the topmost dialog, reverse Tab stayed inside it, and Escape closed only that topmost dialog while restoring focus to its trigger inside the remaining inspector.
- The browser console contained 0 warning/error entries across the acceptance run.

### 320 px timeline visual follow-up

- A focused 320 x 568 box-model audit found that the timeline's implicit grid track was 320.719 px inside a 294 px client-width scroll container. `overflow-x: hidden` prevented page scrolling but clipped the right side; the message viewport was also only 46 px high because the masthead, search controls, and two-row integrity ledger consumed most of the available height.
- The timeline now declares `grid-template-columns: minmax(0, 1fr)`. At 430 px and below, the masthead removes redundant supporting copy, the history action and search share one row, and the three integrity cells remain in one row. The rendered message viewport increased from 46 px to 160 px, while the primary grid track decreased to 278 px.
- A second audit found five overflowing legacy system-event text spans caused by flex-item `min-width: auto`. Those text items now use `min-width: 0`, `flex: 1 1 auto`, and `overflow-wrap: anywhere`.
- Final 320 x 568 runtime evidence: timeline client/scroll width 294 / 294, grid track 278 px, document scroll width 320, zero overflowing descendants, and 0 browser warning/error entries. The representative legacy event text ended at x=283 inside the x=16 through x=304 timeline content boundary.
- The final production build still transformed 1,722 modules and kept the main JavaScript chunk at 364.66 kB / 109.57 kB gzip. Main CSS measured 148.81 kB / 27.46 kB gzip. The final `dist` contains 38 files / 1,550,854 bytes: JavaScript 22 files / 1,124,476 bytes, CSS 15 files / 425,707 bytes, and one other file / 671 bytes.

### 320 px composer density follow-up

- The focused 320 x 568 baseline measured a 144 px composer: 78 px textarea, 60 px toolbar, and a 10 px bottom margin. The mention, launch, and send controls were each 44 px high, so the non-interactive textarea/toolbar allocation rather than the touch targets was the safe compression point.
- The mobile textarea now measures 62 px with reduced internal padding, and the toolbar measures 52 px with 8 px bottom padding around the unchanged 44 px controls. The composer bottom margin remains `max(10px, env(safe-area-inset-bottom))`.
- At that stage, final runtime evidence measured a 120 px composer and a 184 px message viewport instead of 160 px; the three primary controls remained 44 px high, document client/scroll width remained 320 / 320, and the browser reported 0 warning/error entries.
- The 12-member mention menu remained complete and bounded at x=20 through x=268 and y=127 through y=505. This verifies the zero-inset rendered layout; it does not replace the separate real-IME, virtual-keyboard contraction, or non-zero safe-area gaps stated below.

### 1440 px timeline typography follow-up

- A focused 1440 x 900 audit found that the dedicated research-timeline theme rendered message copy at 9 px, author names at 9 px, roles/citations/replies at 7 px, timestamps and ledger labels at 6 px, and ledger values at 8 px. The content was structurally present but visually too small for sustained desktop reading.
- The timeline now uses a scoped 8 / 9 / 10 / 12 px type ladder. Rendered desktop values are 12 px / 19.8 px line-height for message copy, 11 px for author names, 9 px for roles/citations/replies/audit controls, 8 px for timestamps and micro labels, and 10 px for search controls and ledger values. The masthead title/count now render at 14 / 18 px.
- Final 1440 evidence: timeline client/scroll width 1,064 / 1,064, document client/scroll width 1,440 / 1,440, and 0 browser warning/error entries. Card widths and message semantics were unchanged; only the scoped visual hierarchy changed.
- The existing <=430 px overrides continue to render mobile message copy at 11 px and role/citation/reply metadata at 9 px. The clearer 14 px masthead title and 18 px count reduce the post-composer message viewport from 184 px to 177 px, which remains 17 px larger than the original 160 px baseline. Mobile timeline and document scroll widths remain 294 / 294 and 320 / 320 respectively.
- The production build transformed 1,722 modules in 2.46 seconds with main JavaScript unchanged at 364.66 kB / 109.56 kB gzip. Main CSS measured 149.41 kB / 27.52 kB gzip. The resulting `dist` contains 38 files / 1,551,454 bytes: JavaScript 22 files / 1,124,476 bytes, CSS 15 files / 426,307 bytes, and one other file / 671 bytes.

### Evidence boundary

- Source and automated-test evidence covers `visualViewport` variable propagation, top/bottom safe-area formulas, modal focus helpers, the 12-member mention contract, and horizontal-overflow regressions. It proves those code paths and contracts under the guarded test environment; it does not by itself prove physical-device behavior.
- The browser run exercised the rendered application and zero-offset/zero-inset desktop emulation. It did not emulate a non-zero notch, home indicator, floating/contracted visual viewport, or a physical mobile browser's safe-area values.
- The 720 x 450 run is an effective 200% CSS viewport check. The browser runtime still reported `visualViewport.scale === 1` and `devicePixelRatio === 1`; this is not evidence from an actual browser zoom setting of 200%.
- No real IME composition session was performed. Existing composition-event source/tests remain code-level evidence only, not proof for Microsoft Pinyin, Sogou, Gboard, iOS composition, candidate-window placement, or virtual-keyboard viewport contraction.
- No NVDA, JAWS, Narrator, VoiceOver, TalkBack, or other real screen reader was used. Roles, labels, live-region choices, and focus behavior were inspected in the DOM and browser, but spoken output, browse-mode navigation, announcements, and screen-reader/IME interaction remain unverified.

## Automated evidence

- Latest full frontend tests: `423/423` passed across 79 guarded files; failures,
  cancellations, skips, and todo results were all `0`.
- Mounted-DOM modal tests: `6/6` passed, covering initial focus, forward and
  reverse Tab, busy Escape, exact restoration, nested top-only Tab/Escape, and
  busy top-surface non-propagation.
- Mounted business-dialog regression: `1/1` passed with a complete edited
  Observation payload and an awaited busy transition.
- Vite production build: `1688` modules, successful, with no chunk above the
  warning threshold.
- Eager CSS: `144.39 kB` raw / `26.78 kB` gzip, still substantially below the
  `228.31 kB` / `40.01 kB` pre-split baseline while now also owning the neutral
  status-token and convergence-summary styles.
- Lazy Action Overview CSS: `9.12 kB` raw / `2.17 kB` gzip.
- Lazy Workflow Policy CSS: `7.03 kB` raw / `1.93 kB` gzip.
- Lazy Member Version History CSS: `5.95 kB` raw / `1.48 kB` gzip.
- Lazy Round Execution Trace and Discussion Audit CSS: `25.09 kB` raw /
  `4.69 kB` gzip.
- Lazy Paper Portfolio CSS: `6.92 kB` raw / `1.78 kB` gzip.
- Lazy Observation CSS: `4.93 kB` raw / `1.27 kB` gzip.
- Lazy Reflection CSS: `1.25 kB` raw / `0.51 kB` gzip.
- Lazy Artifact workspace CSS: `39.19 kB` raw / `7.02 kB` gzip.
- Main JS: `497.51 kB` raw / `150.94 kB` gzip.
- Lazy Room Inspector: `111.15 kB` raw / `35.09 kB` gzip. Its generic-room
  preload no longer observes the lazy Observation (`15.54 / 5.78 kB`) or Paper
  Portfolio (`42.14 / 14.19 kB`) JavaScript chunks or their CSS resources.
- HTTP security regression for static asset/API boundary: `9/9` passed in the
  isolated backend runner; no non-loopback request, formal port, or Provider
  call was made.

## Remaining frontend work

- Continue ownership-audited lazy CSS extraction only where the component
  boundary is closed. Room Inspector and the combined `Dialogs.jsx` chunk still
  share selectors with eager surfaces and must not be split by prefix alone.
- Execute the new bounded Action Desk late-lazy mounted-DOM regression through
  the README-designated guarded entry before promoting it to passing evidence.
  Keep the test scoped to the extracted binder and tiny Suspense tree; do not
  restore the retired full Vite/RoomInspector unresolved-lazy harness.
- Continue native browser `200%` zoom, visual-viewport/virtual-keyboard height,
  and real screen-reader acceptance. The current pass proves the code-level and
  rendered zero-inset contracts at desktop, `390x844`, `320x568`, and the
  `720x450` effective-200% CSS viewport, but does not represent native zoom,
  mobile IME/composition, physical safe-area, or screen-reader behavior.
- Validate the shared 44px compact-control policy on a physical touch device.
  Current source contracts and zero-inset Chromium geometry cover the owned
  header, history, Inspector, modal, and Composer controls, but do not prove
  finger accuracy, operating-system scaling, or a non-zero physical safe area.

## 2026-08-21：短视口与最终浏览器闭环补证

### 安全回归与 production build

- 回归只通过 README 指定的 `npm.cmd --prefix frontend test` 入口执行，由 `frontend/scripts/run-tests-safe.ps1` 守护；没有直接运行 `node --test`，也没有恢复旧的顶层 Vite/JSDOM lazy gate。
- 实测为 92 个测试文件、495 个测试；495 pass，0 fail，0 skipped，0 cancelled，0 todo。
- 最终 production build 使用 Vite 6.4.3，转换 1,722 modules，用时 2.50 s；没有 500 kB chunk warning。
- 主要产物：主 CSS 150.09 kB（gzip 27.63 kB），主 JS 364.66 kB（gzip 109.56 kB），`react-vendor` 134.67 kB（gzip 43.22 kB），`icon-vendor` 42.13 kB（gzip 9.44 kB）。
- `frontend/dist` 最终实测为 38 files、1,552,139 B。测试通过和本地 production build 只构成工程证据，不代表 Provider 可用、正式迁移获批或发布授权。

### 隔离浏览器实测

所有浏览器验收均设置 `AI_STUDIO_SKIP_LOCAL_ENV=1`，使用系统临时 runtime、显式临时 SQLite、随机回环端口，清空 Provider key，并将 Futu 指向 `127.0.0.1:9`。没有连接 Provider、Futu/OpenD、SEC 或 IR，也没有写正式数据库。

- 720×450（1440×900 的有效 200% 逻辑视口）：`innerWidth/innerHeight` 与 `visualViewport` 均为 720×450，`visualViewport.scale=1`。页头由基线 70 px 压缩到 56 px，textarea 由 62 px 压缩到 48 px，composer 为 106 px，时间线由基线 46 px 增至 117 px；页面 root/body 均为 720/720，时间线为 696/696，无横向溢出。移动端 composer 三个按钮高度均为 44 px。
- 720×450 的 12 人提及菜单完整呈现 12 个 `menuitem`，菜单为 248×260 px，内部 `clientHeight/scrollHeight=258/612`，边界为 x=20..268、y=127..387，始终位于视口内。
- 720×450 的 modal 焦点闭环通过：打开房间列表后焦点进入“关闭房间列表”；从首个焦点项执行 `Shift+Tab` 会回绕到对话框末项；`Escape` 关闭后焦点归还“打开房间列表”。
- 1440×900 守护：页头 70 px、textarea 86 px、时间线 `clientWidth/scrollWidth=1064/1064`，root/body 均为 1440/1440，未出现横向溢出，桌面布局未被短视口 clamp 改写。
- 320×568 守护：页头 70 px、textarea 62 px、composer 120 px、时间线 179 px；root/body 均为 320/320，时间线为 294/294，视口外元素为 0。探针记录的 9 个内部宽度差均来自显式 `overflow-x:hidden`、`text-overflow:ellipsis` 与 `white-space:nowrap`，不是页面横向溢出。
- 320×568 的 12 人提及菜单仍为 12 项，边界 x=20..268、y=127..505，`clientHeight/scrollHeight=376/612`，位于视口内；三个 composer 按钮高度均为 44 px。
- 三档验收完成后浏览器 console 记录为 0。页面截图与 DOM 几何均显示时间线和 composer 仍可用。

### 代码级证据与尚未覆盖的边界

- 代码级与浏览器级已证实：CSS 支持 `env(safe-area-inset-bottom)`；短视口时间线计算后 `padding-bottom=20px`；`--visual-viewport-height` 会跟随逻辑视口更新（320×568 实测为 `568px`），并驱动页头/textarea clamp。Windows 验收环境的 safe-area inset 实际为 0，不能据此宣称真实刘海设备通过。
- 720×450 是等效于 1440×900 在 200% 下的逻辑视口验收，覆盖重排与可用空间，但 `visualViewport.scale=1`；它不等同于真实浏览器缩放手势、操作系统放大镜或移动端双指缩放的完整行为验证。
- modal 的键盘焦点进入、回绕和归还是浏览器自动化实证；尚未使用 NVDA、JAWS、VoiceOver 或 TalkBack 验证朗读顺序、语义播报与 live region 体验。
- textarea 与 `visualViewport` 响应路径已有代码和几何证据，但尚未在真实中文/日文 IME、移动软键盘候选窗、composition event 序列或物理刘海/safe-area 设备上验收。因此不能把本轮结果表述为“真实 IME、屏幕阅读器或全部移动设备已覆盖”。

## 2026-08-22：当前树安全回归、production build 与隔离浏览器复验

### 当前代码级证据

- 本轮已恢复 React 动态列表的无歧义键，并补齐项目实施前结构复核面板与 Composer 定位相关的源码契约；这些结论属于源码与自动化契约证据，不等同于所有真实辅助技术、输入法或设备环境均已覆盖。
- 完整前端回归只通过 README 指定的 `frontend/scripts/run-tests-safe.ps1` 执行，没有直接运行 `node --test`，也没有恢复旧的顶层 Vite/JSDOM 未决 lazy gate。
- 当前真实结果：`91` 个测试文件，`496` 个测试，`496 pass / 0 fail`。
- production build：Vite `6.4.3`，转换 `1743` 个 modules，耗时 `2.32s`，无 build warning。
- 关键产物：main JS `368.87 kB`（gzip `111.07 kB`）、main CSS `186.04 kB`（gzip `34.67 kB`）、ArtifactDialog JS `169.30 kB`（gzip `50.38 kB`）、react-vendor `134.67 kB`（gzip `43.23 kB`）、RoomInspector JS `86.63 kB`（gzip `26.55 kB`）。这些是本次 build 的实际输出，不是预算承诺。

### 当前树隔离浏览器证据

- 运行边界：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环 `127.0.0.1:54495`；12 人房间为 `Frontend Acceptance 12 Role`，启动后所有业务写路由均锁闭。
- `1440x900`：document/body `scrollWidth = clientWidth = 1440`，可见越界元素 `0`；Composer `x=387.19`、`width=1009.63`、`bottom=880`，保留 `20px` 下边距；`visualViewport=1440x900`，offset `0/0`，scale `1`，对应 CSS 自定义属性同步为 `1440px / 900px / 0px / 0px / 1`。
- `320x568`：document/body `scrollWidth = clientWidth = 320`，可见越界元素 `0`；Composer `x=10`、`width=300`、`bottom=558`，保留 `10px` 下边距；`visualViewport=320x568`，offset `0/0`，scale `1`，CSS 自定义属性同步。
- 有效 `200%` 视口 `720x450`：document/body `scrollWidth = clientWidth = 720`，可见越界元素 `0`；Composer `x=10`、`width=700`、`bottom=440`，保留 `10px` 下边距；`visualViewport=720x450`，offset `0/0`，scale `1`，CSS 自定义属性同步。
- 三个视口中的提及菜单均为准确的 `12` 个 `menuitem`。桌面菜单 `248x384`、`scrollHeight=612`；`320x568` 菜单 `248x378`、`scrollHeight=612`；`720x450` 菜单 `248x260`、`scrollHeight=612`。三者均完整位于当前视口内，并通过纵向滚动容纳全部成员。
- 房间设置 modal 打开后，初始焦点位于 dialog 内的输入框；dialog 有 `41` 个可聚焦元素。`Shift+Tab` 后焦点仍在 dialog 内并回绕到“保存房间设置”，`Escape` 关闭 modal 后焦点归还 `aria-label="房间设置"` 的原触发按钮。
- 浏览器 console 的 `warn / warning / error` 记录为 `0`。
- 隔离状态：`enabled_member_count=12`、`provider_calls=0`、`external_market_reads=0`、`outbound_connect_attempts=0`、`post_start_business_writes=0`、`database_unchanged_since_server_start=true`。服务正常停止，stderr 为 `0 B`；随机端口与 `8770 / 11111 / 18787` 均无残留 listener。
- 正式 SQLite 未被使用且复验后指纹不变：main `5,062,656 B`，SHA256 `b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05`；WAL `0 B`；SHM `32,768 B`，SHA256 `fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb`。

### 尚未覆盖的真实环境边界

- `720x450` 是把 `1440x900` CSS 视口折半得到的有效 200% 布局验收；本次 `visualViewport.scale` 仍为 `1`，因此不能宣称已覆盖浏览器原生缩放 200% 或 Windows 显示缩放 200%。
- 当前浏览器确认支持 `env(safe-area-inset-bottom)` 语法，但回环桌面环境没有非零刘海、圆角屏或系统手势区 inset；证据只覆盖 safe-area/visualViewport 代码路径与零 inset 下的 Composer 定位。
- 没有执行真实中文/日文 IME composition、移动系统软键盘抬升、语音输入或候选窗交互；自动化 textarea 契约不等同于真实 IME 已覆盖。
- 没有使用 NVDA、Narrator、JAWS、VoiceOver 或 TalkBack，也没有核验实际朗读顺序、虚拟光标、触摸探索与不同屏幕阅读器的 modal 宣告。
- 因此本节只能支持“当前源码契约、完整安全回归、production build 与三个 CSS 视口的真实浏览器行为已通过”，不能外推为“全部输入法、屏幕阅读器、原生缩放、物理 safe-area 或移动设备均已覆盖”。

## 2026-08-22：空时间线证据启动路径与短视口复验

### 本批实现

- `ChatTimeline.jsx` 将原有两枚被动提示改为语义化有序路径：“明确问题 → 核对证据 → 确认启动”。路径没有 `onClick`、button role 或 tab stop，不伪装成可执行控件。
- `styles/chat-timeline.css` 将桌面空状态重构为本地证据启动台；在 `max-width: 760px` 或 `max-height: 600px` 时隐藏长说明，只保留标题和三步路径，避免短视口要求用户先滚动空时间线。
- 新增 `chatTimelineEmptyState.test.js`，锁定三步顺序、非交互语义和窄/短视口压缩契约。

### 自动化与 production build

- README 安全入口 `frontend/scripts/run-tests-safe.ps1`：`92` 个测试文件，`498 tests / 498 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。未直接执行 `node --test`，未恢复旧的顶层 Vite/JSDOM 未决 lazy gate。
- Vite `6.4.3` production build：`1743 modules`，`7.15s`，exit `0`，无 warning。
- 当前主产物：main JS `369.48 kB`（gzip `111.16 kB`），main CSS `189.25 kB`（gzip `35.27 kB`），ArtifactDialog JS `169.30 kB`（gzip `50.38 kB`），react-vendor `134.67 kB`（gzip `43.23 kB`），RoomInspector JS `86.63 kB`（gzip `26.55 kB`）。

### 当前 dist 的隔离浏览器证据

- `1440x900`：启动台 `640x195.14px`，完整位于 `449.5px` 高的时间线内；长说明可见，三步路径完整，document `scrollWidth = clientWidth = 1440`。
- `320x568`：启动台 `288x77px`，完整位于 `169px` 高的时间线首屏；长说明按契约隐藏，标题与三步路径均可见，document `scrollWidth = clientWidth = 320`。
- 有效 `200%` 视口 `720x450`：启动台 `682x69px`，完整位于 `111px` 高的时间线首屏；标题与三步路径均可见，document `scrollWidth = clientWidth = 720`。
- 三个视口均准确呈现“明确问题、核对证据、确认启动”三步。Composer 位置分别为：桌面 `bottom=880 / margin-bottom=20px`，移动 `bottom=558 / margin-bottom=10px`，有效 200% `bottom=440 / margin-bottom=10px`。
- 三个视口的 `visualViewport` 与 CSS 自定义属性一致，offset 均为 `0/0`、scale 均为 `1`；浏览器支持 `env(safe-area-inset-bottom)` 语法。
- `320x568` 的提及菜单仍为 `12` 个 menuitem，菜单完整位于视口内，`clientHeight=376 / scrollHeight=612`。
- 房间设置 modal 打开后初始焦点位于 dialog 内；`Shift+Tab` 后焦点仍受限于 dialog；`Escape` 关闭后焦点归还“房间设置”触发按钮。
- 浏览器 console 的 warn/warning/error 为 `0`。
- 隔离状态：`provider_calls=0`、`external_market_reads=0`、`outbound_connect_attempts=0`、`post_start_business_writes=0`、临时数据库启动后逻辑快照不变。随机服务正常停止，stderr `0 B`；随机端口与 `8770 / 11111 / 18787` 均无 listener。
- 正式 SQLite 指纹不变：main `5,062,656 B`，SHA256 `b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05`；WAL `0 B`；SHM `32,768 B`，SHA256 `fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb`。

### 证据边界

- `720x450` 仍是 CSS 视口折半形成的有效 200% 验收，`visualViewport.scale=1`；不代表浏览器原生缩放或 Windows 显示缩放 200%。
- safe-area 只覆盖语法支持和零 inset 下的布局，没有非零物理刘海、圆角屏或系统手势区证据。
- 本批没有真实 IME、移动软键盘、语音输入、NVDA、Narrator、JAWS、VoiceOver 或 TalkBack 运行证据；自动化与 DOM 焦点结果不能外推为这些环境已覆盖。

## 2026-08-22：Composer 工具栏显式网格与重叠修复

### 问题与实现

- 当前 dist 的 `320x568` 几何证明确有重叠：提及按钮横向区间为 `20–64px`，Provider 启动确认状态为 `16–102px`，两者相交。该问题不是截图错觉，也不是页面横向溢出，而是工具栏隐式 grid 放置导致。
- `styles/composer-polish.css` 现在为 `≤430px` 使用两行显式区域：第一行 `status status`，第二行 `mention actions`；提及按钮仍保持 `44px` 触控目标，未使用绝对定位、负边距或缩小字号遮掩问题。
- `431–760px` 使用单行 `mention status actions`，避免有效 `200%` 的 `720x450` 因工具栏增高而压缩时间线。
- 首轮移动修正的浏览器复验同时发现桌面既有缺口：键盘提示显示后，四个工具栏表面只进入三个隐式列，动作区 `bottom=907` 超出 Composer `bottom=880`。最终 `≥1181px` 明确使用 `mention keyboard status actions` 四区域网格。
- 新增 `composerMobileLayout.test.js`，分别锁定桌面四区域、移动公共区域分配、≤430px 双行布局和 431–760px 单行布局。

### 最终自动化与 build

- README 安全入口 `frontend/scripts/run-tests-safe.ps1`：`93` 个测试文件，`502 tests / 502 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。
- Vite `6.4.3` production build：`1743 modules`，`2.11s`，exit `0`，无 warning。
- 当前 main JS `369.48 kB`（gzip `111.15 kB`），main CSS `190.31 kB`（gzip `35.43 kB`）；ArtifactDialog、react-vendor 与 RoomInspector JS 分别保持 `169.30 / 134.67 / 86.63 kB`。

### 最终 dist 浏览器证据

- `1440x900`：网格为 `mention keyboard status actions`，列宽 `38 / 635.625 / 86 / 200px`；提及、键盘提示、状态和动作区均完整位于工具栏内，彼此交集均为 `0`。动作区最终 `bottom=865`，已收回 Composer `bottom=880` 内。
- `320x568`：网格为两行 `status status / mention actions`，工具栏 `298x86px`；状态 `20–106px`、提及按钮 `20–64px` 但位于不同行，所有交集为 `0`。空状态启动台仍完整位于 `135px` 高的时间线内。
- 有效 `200%` 的 `720x450`：网格为单行 `mention status actions`，列宽 `44 / 430 / 190px`；全部表面位于工具栏内、交集为 `0`，空状态仍完整位于 `111px` 高的时间线内。
- 三个视口的 document `scrollWidth = clientWidth`，console warn/warning/error 为 `0`。320 档 12 人提及菜单仍完整位于视口内。
- 隔离状态：Provider、外部市场、outbound connect、启动后业务写入均为 `0`；临时数据库逻辑快照不变，服务 stderr `0 B`，随机端口及 `8770 / 11111 / 18787` 无 listener。
- 正式 SQLite 指纹不变：main `5,062,656 B`，SHA256 `b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05`；WAL `0 B`；SHM `32,768 B`，SHA256 `fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb`。

### 仍未覆盖

- 本节仍不构成真实 IME、移动软键盘、屏幕阅读器、浏览器原生 200% 缩放、Windows 显示缩放或非零物理 safe-area 证据。

## 2026-08-22：移动历史搜索可用宽度与单一清除控件

### 问题与实现

- `320x568` 当前 dist 的几何测量显示：禁用的“已到最早记录”按钮占 `94px`，search form 只有 `111.36px`，实际 input 仅 `17.36px`；占位符与用户查询均不可读。页面本身没有横向溢出，因此根因是移动网格分配而不是 viewport 宽度。
- `ChatTimeline.jsx` 现在通过 `data-history-terminal` 明确区分“无更早记录且未在搜索”与可操作状态。
- `styles/chat-timeline.css` 在 `≤430px` 让 search 占满第一行。终止态只隐藏无操作价值的禁用历史按钮；可加载历史或搜索态仍保留第二行 history 区域，不删除功能。
- Chromium 原生 `search` cancel 伪控件已隐藏，应用自己的“清除消息搜索”按钮保留统一可访问名称与状态处理，避免两个 X 同时出现。
- 新增 `timelineMobileSearchLayout.test.js`，锁定终止态标记、移动 search/history 网格、终止态隐藏范围和单一应用清除控件契约。

### 自动化与 build

- README 安全入口：`94` 个测试文件，`506 tests / 506 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。
- Vite `6.4.3` production build：`1743 modules`，`2.11s`，exit `0`，无 warning。
- 当前 main JS `369.55 kB`（gzip `111.19 kB`），main CSS `190.83 kB`（gzip `35.54 kB`）。

### 最终 dist 浏览器证据

- `320x568` 终止态工具栏为单一 `search` 区域，form 从 `111.36px` 扩展为 `292px`。input 空值宽 `198px`；显示“证据链”且出现应用清除按钮时仍有 `158px`。
- 页面只有 `1` 个可访问名称为“清除消息搜索”的按钮。点击后 React 属性值与 DOM input value 均恢复为空字符串。
- 终止态历史区域 computed display 为 `none`；空状态启动台仍完整位于时间线内。
- `320 / 720 / 1440` 三档 document `scrollWidth = clientWidth`，console warn/warning/error 为 `0`。
- 隔离状态：Provider、外部市场、outbound connect、启动后业务写入均为 `0`；服务 stderr `0 B`，随机端口及 `8770 / 11111 / 18787` 无 listener。
- 正式 SQLite 指纹不变：main `5,062,656 B`，SHA256 `b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05`；WAL `0 B`；SHM `32,768 B`，SHA256 `fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb`。

### 证据边界

- 本批只验证 Chromium 当前 build 的搜索视觉与应用清除行为，没有覆盖其他浏览器的原生 search 装饰实现、真实 IME、屏幕阅读器、原生 200% 缩放或非零物理 safe-area。

## 2026-08-22：极窄屏会话头部密度优化

### 问题与实现

- `320x568` 原始几何：房间身份区宽 `145px`，标题仅 `93px`，完整文本需 `210px`；右侧操作区宽 `147px`，其中“房间信息”文字按钮占 `97px`。
- `styles.css` 现在只在 `≤430px` 将“房间信息”压缩为一个 `44px` 图标表面，并清除内部视觉 gap。按钮 DOM 文本继续保留为可访问名称，没有使用 `display:none` 或 `visibility:hidden`，也没有缩小触控目标。
- 新增 `headerMobileDensity.test.js`，锁定“房间信息”文本仍存在于宿主 JSX，以及极窄屏 44px、零 padding、零字号视觉压缩和不隐藏按钮的契约。

### 自动化与 build

- README 安全入口：`95` 个测试文件，`508 tests / 508 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。
- Vite `6.4.3` production build：`1743 modules`，`2.05s`，exit `0`，无 warning。
- 当前 main JS `369.55 kB`（gzip `111.18 kB`），main CSS `191.09 kB`（gzip `35.58 kB`）。

### 最终 dist 浏览器证据

- `320x568`：右侧操作区由 `147px` 收紧到 `94px`；房间身份区由 `145px` 增至 `198px`；标题可用宽度由 `93px` 增至 `146px`。
- “房间信息”表面为 `44x44px`，computed `font-size=0 / gap=0`，浏览器可访问树仍有且仅有 `1` 个名称为“房间信息”的 button。
- `720x450` 和 `1440x900` 不命中极窄规则，文字标签恢复，按钮宽分别为 `97px` 与 `101px`。
- 三个视口 document `scrollWidth = clientWidth`，console warn/warning/error 为 `0`。
- 隔离状态：Provider、外部市场、outbound connect、启动后业务写入均为 `0`；服务 stderr `0 B`，随机端口及 `8770 / 11111 / 18787` 无 listener。
- 正式 SQLite main/WAL/SHM 指纹继续与既有基线一致。

### 证据边界

- 极窄屏图标通过 DOM 文本维持可访问名称，但本批没有真实屏幕阅读器宣告证据；真实 IME、原生 200% 缩放及非零物理 safe-area 仍未覆盖。

## 2026-08-22 空状态样式所有权收敛与隔离复验

### 实施结果

- frontend/src/styles.css 已删除重复的全局 .conversation-empty-* 基础与窄屏规则；空时间线的视觉规则现在只由 frontend/src/styles/chat-timeline.css 在 .chat-timeline-workspace 作用域内持有。
- 已删除不再对应 DOM 的 .conversation-empty-hints 旧契约；当前三步启动路径由 .conversation-empty-route 唯一表达。
- frontend/tests/mobileResearchContext.test.js 不再强制保留已废弃的全局空状态样式。
- 新增 frontend/tests/emptyStateStyleOwnership.test.js，要求宿主全局样式不再出现 conversation-empty-*，组件文件中的相关选择器必须受 .chat-timeline-workspace 约束，并防止已删除 hints 标记回流。

### 当前工作树证据

- README 指定安全入口 frontend/scripts/run-tests-safe.ps1：96 个测试文件全部通过。相对已核验的 95 files / 508 tests 基线，本批只新增 2 tests，因此当前为 510 tests / 510 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo。
- Production build：Vite 6.4.3，1743 modules，2.14 s。
- 主 JS：369.55 kB，gzip 111.18 kB，与前一基线一致。
- 主 CSS：189.23 kB，gzip 35.22 kB；相对 191.09 kB / gzip 35.58 kB，分别减少 1.86 kB / 0.36 kB。
- 隔离浏览器运行时使用 AI_STUDIO_SKIP_LOCAL_ENV=1、系统临时目录内 SQLite、随机回环端口 56201、12 名启用成员；启动后业务写请求保持禁用，启动器退出时 stderr 为 0。
- 1440x900、320x568、720x450（1440x900 的有效 200% CSS 视口）均满足 document/body scrollWidth == clientWidth，无横向溢出。
- 三个视口中的空状态均可见、位于时间线工作区内，并保留 3 个顺序步骤；Composer 工具栏所有可见子项均在自身边界内。
- 320x568 的提及菜单通过真实“提及成员”按钮展开，aria-expanded=true，显示 12 个成员项，菜单位于视口内且未产生横向溢出。
- visualViewport 绑定在三个视口分别发布准确的宽高、offset 0、scale 1 与 --keyboard-inset-bottom: 0px。
- 移动端“房间信息”打开后，焦点进入对话框内的“关闭房间信息”按钮；按 Escape 关闭后，焦点回到原触发器。
- Browser console 记录为 0。
- 验收后隔离进程已退出；8770、11111、18787 与随机端口 56201 均无 listener。
- 正式 SQLite 保持不变：main 5,062,656 B / SHA256 b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05；WAL 0 B；SHM 32,768 B / SHA256 fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb。

### 证据边界

- 上述结果证明当前源码契约、安全测试入口、production bundle 与 Chromium 浏览器中的几何和焦点行为，不等同于真实输入法或辅助技术验收。
- 尚未覆盖中文、日文等原生 IME 的 composition 候选窗、确认/取消序列与移动系统键盘实际 resize 行为。
- safe-area 目前只核验 CSS/visualViewport 发布与无键盘 inset 的浏览器路径；未在带刘海、圆角或动态岛的实体设备上验证系统 env(safe-area-inset-*)。
- modal 的 DOM 焦点进入、Escape 关闭与触发器恢复已实测，但尚未使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 核验朗读顺序、角色宣告、虚拟光标和转子导航。
- 本批没有启动正式服务、连接 Provider/Futu/OpenD/SEC/IR、写正式库、实施 P28 或执行正式迁移。

## 2026-08-22 房间列表长标题与时间语义优化

### 实施结果

- frontend/src/components/RoomSidebar.jsx 继续以 room.id 作为每个房间行的稳定 React key。
- 时间节点已移入 room-copy 内部，标题不再被独立第三列持续挤压。
- 长房间名保留完整 title 文本，并以最多两行的受控布局显示；房间类型与成员数仍保持独立元信息行。
- time 现在提供规范化 ISO 8601 dateTime；无效时间不会输出误导性可见文本或无效语义属性。
- frontend/src/styles/room-sidebar-polish.css 将房间行收敛为状态点与内容两列，时间绝对定位在内容右上角，并为标题预留明确空间。
- 新增 frontend/tests/roomSidebarReadability.test.js，约束稳定 key、全文标题、时间嵌套、ISO 规范化、两列结构、两行上限与时间预留。
- 首轮浏览器检查发现毫秒时间被直接写入 dateTime；该问题已在同批修正并通过第二轮完整回归与浏览器复验。

### 当前工作树证据

- README 安全入口 frontend/scripts/run-tests-safe.ps1：97 个测试文件全部通过；当前为 512 tests / 512 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo。
- Production build：Vite 6.4.3，1743 modules，2.21 s。
- 主 CSS：189.35 kB，gzip 35.25 kB。
- 主 JS：369.75 kB，gzip 111.22 kB。
- 1440x900：测试房间名 Frontend Acceptance 12 Role 完整显示为两行，fullTextFits=true，时间预留无碰撞，房间列表 clientHeight 与 scrollHeight 均为 638，无新增滚动。
- 720x450 有效 200% 视口：移动房间抽屉内标题一行完整显示，焦点进入关闭按钮；Escape 后抽屉关闭并恢复到打开房间列表触发器。
- 320x568：长标题完整显示为两行，时间位于右上且无碰撞；抽屉焦点进入与 Escape 恢复均通过。
- 三个视口均无横向溢出，visualViewport 变量与实际视口一致，Browser console 为 0。
- 最终 dateTime 为 2026-08-22T12:09:56.184Z；Date.parse 返回有限值，new Date(parsed).toISOString() 与属性值完全一致。
- 最终隔离运行只读取 /api/bootstrap 1 次；Provider calls、external market reads、outbound connect attempts、post-start business writes 均为 0，stderr 为 0。
- 验收后随机端口 59792 与正式边界端口 8770、11111、18787 均无 listener。
- 正式 SQLite 指纹保持不变：main 5,062,656 B / SHA256 b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05；WAL 0 B；SHM 32,768 B / SHA256 fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb。

### 证据边界

- title 属性与有效 time/dateTime 改善了浏览器语义，但本批未使用真实屏幕阅读器核验标题与时间的朗读行为。
- 两行截断已通过当前夹具长标题和 CSS 契约验证；更极端语言组合、浏览器字体替换和系统级放大镜仍需独立实体环境覆盖。
- 本批未启动正式服务、连接 Provider/Futu/OpenD/SEC/IR、写正式库、实施 P28 或执行正式迁移。

## 2026-08-22 房间搜索清除与移动侧栏稳定性

### 实施结果

- frontend/src/components/RoomSidebar.jsx 将房间搜索改为带名称的 search landmark，并明确使用受控 type=search 输入。
- 查询存在时显示应用自有“清除房间搜索”按钮；按钮清空后恢复焦点到输入框。
- Escape 在查询存在时只清除查询并保留输入焦点；查询为空时继续交给移动模态焦点契约关闭抽屉并恢复打开按钮。
- frontend/src/styles/room-sidebar-polish.css 隐藏 Chromium 原生 search cancel，避免与应用清除按钮重复。
- 清除按钮在桌面为 32x32，在移动视口为 36x36，并拥有明确 hover、active、focus-visible 与 reduced-motion 行为。
- 搜索框固定 min-height 40px，避免清除按钮挂载/卸载导致高度变化。
- 移动侧栏品牌、主操作、搜索和分区标签固定 flex: 0 0 auto；room-list 独占剩余高度并 overflow-y: auto，筛选不再压缩品牌区或推动搜索框。
- 新增 frontend/tests/roomSidebarSearch.test.js，覆盖命名搜索、按钮/Escape 双清除、焦点恢复、原生 cancel 隐藏、固定高度、固定控制区和列表滚动所有权。

### 当前工作树证据

- README 安全入口 frontend/scripts/run-tests-safe.ps1：98 个测试文件全部通过；当前为 514 tests / 514 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo。
- Production build：Vite 6.4.3，1743 modules，2.33 s。
- 主 CSS：190.40 kB，gzip 35.42 kB。
- 主 JS：370.17 kB，gzip 111.29 kB。
- 1440x900、720x450 有效 200% 与 320x568 均通过真实输入、按钮点击、Escape 与焦点检查。
- 三个视口中空白、过滤、清空三态的搜索框均保持 height 40px；移动品牌区均保持 height 70px，搜索框均保持 y 174px，stableHeaderAndSearch=true。
- 输入 Frontend 后可见房间从 6 个收敛为 1 个，清除后恢复 6 个；桌面按钮清除与 Escape 清除均保持输入焦点。
- 720x450 与 320x568 中，清除按钮完整位于搜索框边界内；查询为空后 Escape 关闭房间抽屉并恢复到“打开房间列表”按钮。
- 三个视口均无横向溢出；移动 room-list 固定从 y 257px 开始，只由列表自身处理剩余高度与滚动。
- Browser console 为 0。
- 最终隔离运行只读取 /api/bootstrap 1 次；Provider calls、external market reads、outbound connect attempts、post-start business writes 均为 0，stderr 为 0。
- 验收后随机端口 62939 与正式边界端口 8770、11111、18787 均无 listener。
- 正式 SQLite 指纹保持不变：main 5,062,656 B / SHA256 b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05；WAL 0 B；SHM 32,768 B / SHA256 fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb。

### 证据边界

- search landmark、aria-label、键盘焦点与 Escape 行为已在 Chromium DOM 中验证，但尚未使用真实屏幕阅读器核验 landmark 列表与清除按钮的朗读。
- 本批未覆盖原生移动输入法候选窗、实体设备虚拟键盘 resize 或系统放大镜；visualViewport 本轮保持无键盘 inset。
- 本批未启动正式服务、连接 Provider/Futu/OpenD/SEC/IR、写正式库、实施 P28 或执行正式迁移。

## 2026-08-22 Icon Rail 稳定身份与键盘导航

### 实施结果

- frontend/src/components/IconRail.jsx 将导航列表 key 从可翻译 label 改为稳定 section 身份，并保留唯一 aria-current=page 活动项。
- 图标标记 aria-hidden=true 与 focusable=false，按钮本身继续提供完整 aria-label。
- ArrowDown 与 ArrowUp 在四个可用导航按钮间环绕移动焦点；Home 与 End 跳到首尾。方向键 helper 不调用 onNavigate，Enter/Space 或显式点击仍是唯一模块切换动作。
- frontend/src/styles/icon-rail-polish.css 增加克制的动作区分隔线、聚焦图标反馈、活动图标光晕和 reduced-motion 收敛。
- rail 建立 z-index 40，允许 z-index 90 的焦点 tooltip 跨过侧栏层叠上下文真实可见，同时 tooltip 保持 pointer-events none。
- 混合鼠标与键盘输入时，rail 内存在 focus-visible 后隐藏其他仅 hover tooltip，只保留当前键盘焦点标签。
- 新增 frontend/tests/iconRailNavigation.test.js，覆盖稳定 key、显式激活、方向键环绕、Home/End、无隐式导航、层叠、单 tooltip 与 reduced-motion 契约。

### 当前工作树证据

- README 安全入口 frontend/scripts/run-tests-safe.ps1：99 个测试文件全部通过；当前为 517 tests / 517 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo。
- Production build：Vite 6.4.3，1743 modules，2.25 s。
- 主 CSS：191.19 kB，gzip 35.55 kB。
- 主 JS：370.72 kB，gzip 111.46 kB。
- 1440x900 中按钮数为 4；初始 rooms 聚焦后 ArrowDown 到 members，End 到 artifacts，Home 回 rooms，ArrowUp 从首项环绕到 artifacts，ArrowDown 再环绕到 rooms。
- 上述所有方向键步骤中 aria-current 始终只有 rooms，证明焦点移动没有隐式模块切换。
- 聚焦 tooltip 的 content 与按钮名称一致、opacity 1、pointer-events none。
- 最终混合输入态中鼠标仍 hover rooms 时其 tooltip opacity 为 0，键盘 focus-visible 的 artifacts tooltip opacity 为 1，visibleTipCount=1。
- 720x450 有效 200% 与 320x568 中 Icon Rail display=none、几何宽高为 0，未增加移动负担。
- 三个视口均无横向溢出，Browser console 为 0。
- 最终隔离运行只读取 /api/bootstrap 1 次；Provider calls、external market reads、outbound connect attempts、post-start business writes 均为 0，stderr 为 0。
- 验收后随机端口 61232 与正式边界端口 8770、11111、18787 均无 listener。
- 正式 SQLite 指纹保持不变：main 5,062,656 B / SHA256 b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05；WAL 0 B；SHM 32,768 B / SHA256 fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb。

### 证据边界

- DOM 焦点、aria-current、tooltip 几何与键盘事件已在 Chromium 中验证，但尚未使用 NVDA、Narrator、JAWS 或 VoiceOver 核验全局导航的朗读顺序和快捷导航。
- Home/End 与方向键行为不改变模块是本组件契约；实际激活各懒加载模块的业务内容不在本批范围内。
- 本批未启动正式服务、连接 Provider/Futu/OpenD/SEC/IR、写正式库、实施 P28 或执行正式迁移。

## 2026-08-22 移动主标题、安全测试入口与最终隔离验收补记

### 代码级修正与契约

- 本批既有的 React 动态列表无歧义键、项目实施前结构复核面板与 Composer 定位契约，均保留在最终完整回归范围内；本节不把源码契约外推为真实辅助技术或输入法覆盖。
- 主会话标题现在发布完整 `title` 文本。`conversation-shell-polish.css` 作为最终样式所有者，在 `<=760px` 移动 header 中显式将继承的 grid `row-gap` 收敛为 `0`；在 `<=430px` 才启用最多两行的完整长标题布局。这样同时修复了 `720x450` 有效 200% CSS 视口和 `320px` 窄屏的 header 越界，未扩散到桌面布局。
- 新增窄屏 containment 静态契约，并保留既有移动 header 密度契约；两行标题的完整文本、样式所有权和移动行距均由源码测试约束。
- README/npm 安全入口原先在 Windows PowerShell 5.1 下直接访问不可用的 `.NET ProcessStartInfo.ArgumentList`，会在任何测试断言执行前 fail-closed。runner 现在先探测 `ArgumentList`；现代运行时继续使用 typed argument collection，5.1 使用逐参数受控加引号的 `Arguments` 回退，并拒绝无法安全表示的引号或尾随反斜杠参数。逐文件、单并发、超时、内存、输出与进程树守卫均未移除。

### 最终自动化与 production build

- 最终只通过 README 指定入口 `AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test` 执行，没有直接运行 `node --test`，也没有恢复旧的顶层 Vite/JSDOM 未决 lazy gate。
- 真实结果：`100` 个测试文件，`519 tests / 519 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。
- Production build：Vite `6.4.3`，`1743 modules`，`2.06s`，exit `0`，无 warning。
- 主要产物：main CSS `191.60 kB`（gzip `35.64 kB`），main JS `370.76 kB`（gzip `111.48 kB`），ArtifactDialog JS `169.30 kB`（gzip `50.38 kB`），react-vendor `134.67 kB`（gzip `43.23 kB`），RoomInspector JS `86.63 kB`（gzip `26.55 kB`）。

### 隔离浏览器验收

- 运行边界：仅 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 与随机回环端口 `62252`；12 名启用成员。最终状态为 `0` provider calls、`0` external market reads、`0` outbound attempts、`0` post-start business writes，临时数据库自服务启动后未变化，stderr `0 B`；`8770 / 11111 / 18787` 均无 listener。
- `1440x900`、`720x450` 有效 200% CSS 视口、`320x568` 与额外短高 `320x450` 均无横向溢出。四种视口中标题、真实 `.status` 与操作区均无碰撞，标题和状态均完整包含在 header 内；`720px` 与 `320px` 的计算 `row-gap` 均为 `0px`。
- `320px` 下 `Frontend Acceptance 12 Role` 实测为完整两行，`white-space: normal`，完整文本与 tooltip 均保留；`320x450` 时身份区仍完整位于 `56px` header 内。
- `visualViewport` 的 width、height、offset、scale 与 root CSS 变量在四种视口一致，Composer 始终落在可见 visual viewport 内。`320x568` 的提及菜单实测 `12` 项，菜单完整位于视口且无横向溢出。
- 房间设置 modal 初始焦点进入房间名称输入框；从首个关闭按钮逆向 Tab 环回最后的保存按钮，再正向 Tab 回到关闭按钮；Escape 关闭后焦点精确恢复到“房间设置”触发器。浏览器 console 为 `0`。

### 尚未覆盖的真实环境边界

- 未使用真实中文/日文 IME 组合输入验证 composition 生命周期、候选窗位置或软键盘厂商差异。
- 未使用 NVDA、JAWS、VoiceOver、TalkBack 等真实屏幕阅读器验证朗读顺序、模式切换或用户偏好组合。
- `720x450` 是 1440x900 的有效 200% CSS 视口等价验收，不等同于每种浏览器原生 zoom、Windows DPI 或 OS 文本缩放组合。
- 当前环境没有物理刘海/圆角设备，safe-area 结论限于代码合同、visualViewport CSS 变量和 Composer 可见范围；不声称已覆盖真实设备的非零 `env(safe-area-inset-*)`。
- 上述结果是当前源码、自动化与隔离浏览器证据，不代表正式迁移授权、正式库写入、Provider/Futu/OpenD/SEC/IR 接入、P28 实施或公开发布批准。
## 2026-08-22 下一轮项目焦点卡片信息层级续作

### 本批代码级变化

- `ProjectRoundFocusCard` 继续使用既有只读 preview / frozen record、精确产物绑定、房间上下文指纹和显式目标回填授权；未修改 API、授权载荷、Provider 预算、市场读取预算或业务写入边界。
- 有效视图现在按“精确来源 -> 三类缺口 -> NEXT PASS 建议目标 -> 前三条优先依据 -> 操作边界”排序。服务端已排序的第一条焦点只用于生成“结构 / 阻断 / 证据优先”标签，不在前端重新排名。
- 结构、阻断和证据计数使用独立的蓝、琥珀和青绿色阶；琥珀仅表示需要注意的阻断，不使用完整性错误的红色语义。
- 建议目标前移并保持为可编辑目标的显式回填按钮；仍不自动开始轮次、不点名成员、不改变流程，也不生成批准结论。
- 超过三条时不再显示没有对应操作入口的“请打开”指令，改为说明完整缺口保留在精确确认产物中，避免本面板替代产物审阅。
- 操作边界改为“只预填 / 不自动开始 / 用户最终决定”三格合同。`<=300px` 容器降为单列，按钮在窄容器维持 44px；forced-colors 继续使用系统 Canvas / CanvasText。
- `projectRoundFocus.test.js` 增加上述信息顺序、无死端文案、三格边界、窄容器与 forced-colors 的静态源码契约。

### 当前证据边界

- 本批之后尚未重新运行 README 安全回归、production build 或隔离浏览器验收；上一批 `100 files / 519 tests`、bundle 和浏览器结果只证明修改前基线，不能外推为本批验证结果。
- 尚未实测卡片在真实长中文目标、浏览器原生 zoom、非零 safe-area、真实屏幕阅读器和键盘组合下的最终呈现。
- 这些变化只改善当前源码的信息架构和视觉合同，不代表正式迁移、Provider/Futu/OpenD/SEC/IR 接入、P28 或公开发布获得授权。
### 下一轮项目焦点卡片最终自动化与隔离边界（2026-08-22）

- 首次验证时 README 安全回归已为 `100 files / 519 tests / 519 pass / 0 fail`，但 production build 报出一个 `Unexpected "}"` CSS warning。该 warning 来自 `project-round-focus-polish.css` 的单个多余闭合括号，因此首次 build 只记为 exit `0`、存在 warning，不记为干净通过。
- 删除多余括号后，`projectRoundFocus.test.js` 增加 CSS 花括号数量平衡断言，使同类错误以后在安全回归阶段直接失败。
- 最终再次通过 `AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test` 完整运行：`100` 个测试文件，`519 tests / 519 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。没有直接执行 `node --test`，没有恢复旧的顶层 Vite/JSDOM 未决 lazy gate。
- 最终 production build：Vite `6.4.3`，`1743 modules`，`2.08s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `370.76 kB`（gzip `111.47 kB`）；包含本卡片的 RoomInspector CSS `42.33 kB`（gzip `7.09 kB`），RoomInspector JS `87.35 kB`（gzip `26.74 kB`）。
- 隔离浏览器仅使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 `55874`。切换到“项目可行性讨论”并打开房间信息后，fixture 只提供“尚未创建项目研究工作区”的准备态，`ProjectRoundFocusCard` 数量为 `0`；未通过开始一轮或业务写入制造卡片数据。
- 因此当前浏览器证据只证明该项目准备态在 `1440x900` 无横向溢出且 console 为 `0`，不能证明本批焦点卡片的真实桌面或窄屏渲染。真实卡片视觉验收仍需要未来的只读已封印 fixture。
- 实例最终为 `0` provider calls、`0` external market reads、`0` outbound attempts、`0` post-start business writes，临时数据库自启动后未变化，stderr `0 B`；`8770 / 11111 / 18787` 均无 listener。
## 2026-08-22 ProjectRoundFocus 可复现 exact fixture 与真实视觉验收

### QA 基础设施修正

- 既有 `scripts/run_isolated_project_round_focus_qa.py --scenario exact` 在绑定端口前 fail-closed，因为仍断言旧 `round_launch_plan_v4`；当前后端已使用 `round_launch_plan_v5` 与通用 `round_context_authorizations`。
- 脚本现在直接构造闭合的 `round_context_authorization_set_v1`，其中唯一条目为 `project_round_focus / core.round.context/v1`，并通过 `round_context_authorizations=` 关键字调用 plan service。启动前自检精确核对 v5、授权集合、registry 封印、ready 状态和只读安全字段；不恢复旧专属授权字段。
- readiness JSON 增加显式 `flush=True`，解决重定向 stdout 时自动化无法获得随机端口的问题。`tests/test_isolated_qa_scripts.py` 新增 v5、通用 context、无 v4 断言和显式 flush 契约；实测 `4/4` 通过。

### 卡片真实渲染问题与修正

- exact fixture 成功生成确认产物 v2，并在房间信息中真实渲染 `ProjectRoundFocusCard`：结构 / 阻断 / 证据计数为 `3 / 1 / 1`，服务端首项显示“阻断优先”，前三条焦点和另两条保留说明均存在。
- 首次几何发现桌面 Inspector 外宽 `329px`、卡片 content box 约 `297px`，原 `max-width:300px` container query 在桌面误触发，三类计数和三项边界全部单列；超长建议目标使桌面卡片高 `1183.75px`、目标区 `375.44px`。320x568 下卡片高 `1255.44px`、目标区 `394.63px`。
- 修正后，计数与边界只在真正极窄的 `<=220px` content box 才单列；当前桌面三列宽约 `95px`，320px drawer 三列宽约 `75.53px`，两种视口都无横向溢出。
- 超过 160 字符的建议目标默认五行，完整文本仍保留在 DOM；“展开完整目标 / 收起完整目标”发布 `aria-expanded`。桌面折叠后卡片高 `801.72px`、目标区 `215.94px`；320px 下卡片高 `911.22px`。展开后 320px 段落由 `96px` 恢复为完整 `269px`，再次收起回到 `96px`。
- 桌面两颗目标按钮并排；320px 下为两颗 `44px` 单列按钮。三项操作边界保持同一行，未使用错误红色语义。

### Composer 焦点合同

- 首次移动回填实测只写入 textarea，Inspector 关闭后的共享焦点恢复又把焦点放回触发按钮。单个 `requestAnimationFrame` 无法覆盖该恢复顺序。
- App 现在使用一次性的 `inspectorPostCloseFocusRef`：移动回填先登记 Composer textarea，再关闭 Inspector；共享关闭 effect 优先消费该目标，普通关闭仍恢复原触发器。桌面路径保持下一帧直接聚焦。行动项填入路径同步使用该合同。
- 最终 320x568 实测：卡片离开视口，335 字符目标完整写入，活动元素为 `TEXTAREA`，`aria-label="消息内容"`，无横向溢出，console `0`。

### 最终自动化、build 与隔离边界

- 最终 README 安全入口：`100 files / 519 tests / 519 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- Production build：Vite `6.4.3`，`1743 modules`，`2.04s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `371.00 kB`（gzip `111.54 kB`），RoomInspector CSS `43.05 kB`（gzip `7.19 kB`），RoomInspector JS `87.88 kB`（gzip `26.89 kB`）。
- 最终 exact 实例只使用系统临时 runtime、显式临时 SQLite 和随机回环端口 `49398`。状态为 `v5_round_context_plan_verified=true`、`exact_focus_verified=true`、`0` provider calls、`0` market reads、`0` outbound attempts、`0` business writes；stderr `0 B`，实例关闭后端口不再监听，`8770 / 11111 / 18787` 均无 listener。
- 当前仍未使用真实屏幕阅读器、真实 IME、浏览器原生 zoom、Windows DPI 或非零物理 safe-area；这些边界不能由当前 Chromium CSS 视口验收外推。

## 2026-08-22 ProjectRoundFocus bootstrap / inactive-record 补齐与单操作布局收口

### 代码级修正

- `bootstrap` 与 `inactive-record` 都只提供一个目标操作。桌面双列操作区原先仍把唯一按钮放在左列，实测操作区为 `266px`、按钮仅 `129.5px`；`project-round-focus-polish.css` 现在让 `.project-round-focus-objective-actions > :only-child` 跨越完整两列。exact 场景的双按钮布局不受影响。
- `projectRoundFocus.test.js` 增加独立静态契约，约束唯一操作必须使用 `grid-column: 1 / -1`。该变化只调整卡片表现和源码契约，没有修改 preview / frozen-record API、授权载荷、排序、Provider 或市场预算、业务写入边界。
- React 动态列表的无歧义 key、12 人提及、modal focus、visualViewport / safe-area 代码合同和 Composer 回填定位继续由完整安全回归覆盖；本节不把这些源码断言外推成真实辅助技术或输入法结论。

### 真实自动化与 production build

- 新契约首次进入 README 安全 runner 时真实失败：`projectRoundFocus.test.js` 为 `8 pass / 1 fail`，原因是新测试越过局部作用域引用了不存在的 `cardStyleSource`。该轮完整回归因此记为 fail，没有按通过报告。
- 测试改为在自身作用域只读同一 CSS 后，最终仅通过 `AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test` 完整运行：`100` 个测试文件，`520 tests / 520 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。没有直接执行 `node --test`，也没有恢复旧的顶层 Vite/JSDOM 未决 lazy gate。
- Production build：Vite `6.4.3`，`1743 modules`，`2.26s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `371.00 kB`（gzip `111.54 kB`），RoomInspector CSS `43.14 kB`（gzip `7.20 kB`），RoomInspector JS `87.88 kB`（gzip `26.88 kB`）。build 后只修正了测试局部变量，产品源码未再变化。

### 隔离浏览器补齐

- 所有实例只使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口；没有启动正式服务、使用正式库或连接 Provider/Futu/OpenD/SEC/IR。
- `bootstrap` 初轮在 `1440x900` 与 `320x568` 真实渲染零缺口卡片：三类计数均为 `0`、没有焦点行、短目标没有展开按钮，卡片高度分别约 `471.08px` 与 `509.77px`，两个视口均无横向溢出且 console 为 `0`。初轮桌面唯一回填按钮仅占 `129.5 / 266px`；最终 post-fix `1440x900` 复验为按钮与操作区同为 `266px`，仍无横向溢出、console 为 `0`。本次修正后的 bootstrap 未重复运行 `320x568`，移动结论限于修正前真实几何与共享 CSS 契约。
- `inactive-record` 真实显示“本轮冻结项目焦点 / 已冻结”、三条焦点和“这是当前轮次的冻结记录，不能回填到下一轮。”，且只有展开按钮、没有回填操作。初轮桌面卡片约 `823.66px` 高、目标区约 `237.88px`；初轮 `320x568` 卡片约 `882.16px` 高，三类计数各约 `75.53px`，无横向溢出。
- `inactive-record` 最终 post-fix 复验中，`1440x900` 的展开按钮从 `129.5 / 266px` 修正为 `266 / 266px`；`320x568` 的操作区与按钮均为 `207.59px`，按钮高 `44px`，卡片宽约 `270.59px`、高约 `882.16px`。两个视口均无横向溢出，console 为 `0`。
- 最终 bootstrap 状态为 `bootstrap_none_verified=true`、`v5_round_context_plan_verified=true`、`1` 次 focus preview GET；最终 inactive-record 状态为 `inactive_frozen_record_verified=true`、`v5_round_context_plan_verified=true`、`2` 次 frozen-record GET。两者均为 `0` provider calls、`0` market reads、`0` business writes、`0` outbound connect attempts，stderr `0 B`；随机端口 `53846 / 59736` 关闭后均无 listener。
- 正式边界再次核验：`8770 / 11111 / 18787` 均无 listener；正式 SQLite main 为 `5,062,656 B`、SHA256 `b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05`，WAL `0 B`，SHM `32,768 B`、SHA256 `fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb`，与本批开始边界一致。

### 仍未覆盖的真实环境边界

- 本批的按钮尺寸、横向溢出、焦点、`aria-expanded` 和 console 结论来自 Chromium DOM 与几何检查；没有使用 NVDA、Narrator、JAWS、VoiceOver 或 TalkBack 验证朗读顺序、模式切换与用户偏好组合。
- 没有使用真实中文/日文 IME、候选窗、实体软键盘或厂商输入法验证 composition 生命周期。Composer 聚焦结论只证明浏览器活动元素合同，不代表真实输入法交互已覆盖。
- 这两个补齐场景本批未在 `720x450` 有效 200% 视口、浏览器原生 zoom、Windows DPI / 文本缩放或非零物理 safe-area 上重复运行；不能把此前应用壳层的 200% / visualViewport 验收外推为本场景专属真实设备结论。
- 当前证据仍不代表正式迁移授权、正式库写入、Provider/Futu/OpenD/SEC/IR 接入、P28 实施、公开发布批准或任何投资结论。

## 2026-08-22 Inspector 启动前结构复核定位与短高密度

### 信息架构与代码级变化

- exact 隔离页面首次测量时，Inspector 可视高约 `913px`，目标区高 `405.28px`、收敛区高 `546.48px`，`ProjectRoundFocusCard` 从 `y=951.77px` 才开始；用户在首屏点击“开始一轮”时看不到结构复核入口。
- `RoomInspector` 源码顺序由“本轮目标 -> 收敛与决策门 -> 下一轮项目焦点 -> 路由配置”调整为“本轮目标 -> 下一轮项目焦点 -> 收敛与决策门 -> 路由配置”。结构复核是启动目标的来源证据，不再被当作路由配置细节排在通用收敛状态之后。
- `inspectorInformationArchitecture.test.js` 集中约束上述顺序，组件专属测试不再重复维护同一断言。
- `max-width:760px` 且 `max-height:600px` 时，`room-inspector-refinement.css` 只压缩目标区的非交互间距：section 上下 padding、标题/正文/ledger/capability/readiness/provider 间隔；正文行高仍为 `1.55`，没有隐藏信息或修改按钮、API、授权与业务行为。

### 真实失败、最终回归与 build

- 初次只交换组件顺序后，README 安全 runner 在既有 `inspectorInformationArchitecture.test.js` 真实 fail-fast：累计 `233 tests / 232 pass / 1 fail`。原因是旧契约仍要求 convergence 早于 project focus；该轮没有按通过报告。
- 复核旧契约语义后，将集中式信息架构更新为目标与结构复核早于状态和配置；随后短高密度契约加入最终回归。
- 最终仅通过 `AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test` 完整运行：`100 files / 521 tests / 521 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`。未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM 未决 lazy gate。
- 最终 production build：Vite `6.4.3`，`1743 modules`，`2.22s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `371.00 kB`（gzip `111.54 kB`），RoomInspector CSS `43.73 kB`（gzip `7.30 kB`），RoomInspector JS `87.88 kB`（gzip `26.89 kB`）。

### 隔离浏览器几何与连续工作流

- `1440x900` 中卡片起点由 `y=951.77px` 提前到 `y=405.28px`；标题、精确来源、三类计数、建议目标和目标操作进入 Inspector 首屏，源码与真实 DOM 顺序均为 objective -> project focus -> convergence。无横向溢出。
- `320x568` 中，单纯重排先把卡片起点从 `y=1054.39px` 提前到 `y=507.91px`；短高密度收敛后进一步提前到 `y=470.31px`，标题范围为 `y=486.31..543.31px`，完整落在首屏内。visualViewport 为 `320x568 / scale 1`，无横向溢出。
- `720x450` 有效 200% CSS 视口中，短高收敛将目标区从 `405.28px` 降至 `367.69px`；卡片从原先首屏外的 `y=451.86px` 提前到 `y=414.27px`，标题从 `y=430.27px` 开始真实可见。visualViewport 为 `720x450 / scale 1`，无横向溢出。
- 最终 `320x568` 真实点击“填入下一轮目标”后，Inspector 关闭，`335` 字符目标完整进入 Composer，活动元素为 `TEXTAREA`、`aria-label="消息内容"`，无横向溢出，console 为 `0`。
- exact QA 只使用系统临时 runtime、显式临时 SQLite 与随机回环端口 `62232`；最终为 `exact_focus_verified=true`、`v5_round_context_plan_verified=true`、`3` 次 focus preview GET、`0` provider calls、`0` market reads、`0` business writes、`0` outbound attempts，stderr `0 B`。实例关闭后 `62232 / 8770 / 11111 / 18787` 均无 listener。
- 正式 SQLite 再次保持 main `5,062,656 B` / SHA256 `b32e88a0c0be5db2d052904221c6c85d1b1c7862fd76f45eb8df08b7ec41cc05`、WAL `0 B`、SHM `32,768 B` / SHA256 `fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb`。

### 证据边界

- 本批证明源码顺序、Chromium 可见几何、visualViewport、按钮点击、Composer 焦点和 console；没有使用真实 IME 或 NVDA、Narrator、JAWS、VoiceOver、TalkBack 验证输入法与朗读行为。
- `720x450` 是有效 200% CSS 视口证据，不等同于浏览器原生 zoom、Windows DPI、系统文本缩放或物理设备非零 safe-area。
- 本批不改变正式迁移、正式库、Provider/Futu/OpenD/SEC/IR、P28、公开发布或投资结论边界。

## 2026-08-22 Inspector 移动主操作 44px 触控收口

### 代码与契约

- 上一批最终几何显示，`320x568` 与 `720x450` 中 `.round-controls` 的“开始一轮 / 暂停”仍只有 `40px` 高，低于同一移动工作流中已统一的 `44px` 关键触控目标。
- `room-inspector-refinement.css` 在 `max-width:760px` 普通移动层将两颗主操作设为 `min-height:44px`；短高媒体层仍只收敛非交互间距，不覆盖按钮高度。
- `inspectorInformationArchitecture.test.js` 在既有短高合同中同时约束移动 `44px` 主操作，并继续禁止短高规则自行缩小控件。按钮文本、禁用状态、事件、API、授权和业务边界均未改变。

### 自动化、build 与真实浏览器

- README 安全入口最终为 `100 files / 521 tests / 521 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM 未决 lazy gate。
- Production build：Vite `6.4.3`，`1743 modules`，`2.22s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `371.00 kB`（gzip `111.54 kB`），RoomInspector CSS `43.82 kB`（gzip `7.32 kB`），RoomInspector JS `87.88 kB`（gzip `26.89 kB`）。
- `720x450` 有效 200% CSS 视口中，两颗主操作均为 `44px` 高；结构复核卡片从 `y=418.27px` 开始，标题从 `y=434.27px` 开始，仍在首屏可见。visualViewport 为 `720x450 / scale 1`，无横向溢出。
- `320x568` 中，两颗主操作均为 `44px` 高；结构复核卡片从 `y=474.31px` 开始，标题范围为 `y=490.31..547.31px`，完整落在首屏内。visualViewport 为 `320x568 / scale 1`，无横向溢出，console 为 `0`。
- exact QA 仅使用系统临时 runtime、显式临时 SQLite 与随机回环端口 `62225`；最终 `exact_focus_verified=true`、`v5_round_context_plan_verified=true`、`1` 次 focus preview GET，Provider、市场读取、业务写入和出站连接均为 `0`，stderr `0 B`。实例关闭后 `62225 / 8770 / 11111 / 18787` 均无 listener，正式 SQLite 指纹不变。

### 边界

- `44px` 结论来自 Chromium 计算几何，不代表已使用实体触控设备、真实 IME 或屏幕阅读器完成验证。
- `720x450` 仍是有效 200% CSS 视口证据，不外推到所有浏览器原生 zoom、Windows DPI、系统文本缩放或物理 safe-area 组合。

## 2026-08-22 Inspector 移动交互双维度审计与产物选择器修正

### 审计发现与代码修正

- 对 `320x568` Inspector 内全部 `button / summary / a / input / textarea / select` 同时检查宽高后，首屏控件均已达到合同，但整条面板仍有四个真实交互目标任一维小于 `44px`：讨论流程“设置”为 `34x44px`，成员与资料“添加”均为 `52x17px`，会议草稿整理成员 select 为 `16x44px`。
- `.section-heading .text-action` 现在在 `max-width:760px` 下统一拥有至少 `44x44px` 的触控盒，并保持透明文字操作外观、居中内容与轻量横向 padding。
- artifact panel 已在 `max-width:520px` container 中把 control deck 切成单列，但内部 `.artifact-synthesizer` 仍保留两列，导致 select 被说明文本挤压。当前窄容器改为标签说明上行、全宽 select 下行；宽容器仍保留原布局。
- `inspectorInformationArchitecture.test.js` 同时约束移动文字操作双维度和 artifact synthesizer 的窄容器单列，不修改成员候选、Provider 绑定、生成行为、API 或授权边界。

### 自动化、build 与真实浏览器

- README 安全入口最终为 `100 files / 521 tests / 521 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM 未决 lazy gate。
- Production build：Vite `6.4.3`，`1743 modules`，`2.29s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `371.00 kB`（gzip `111.54 kB`），RoomInspector CSS `44.05 kB`（gzip `7.36 kB`），RoomInspector JS `87.88 kB`（gzip `26.89 kB`）。
- 修正前 select 在桌面 Inspector 也仅为 `65.14x44px`；最终 desktop artifact container 中为 `249x44px`，标签说明 `249px` 全宽位于上方，无横向溢出。
- 最终 `320x568` 中，“设置”为 `44x44px`，两个“添加”均为 `56x44px`，select 为 `196.59x44px` 且保留 `5` 个选项。对 Inspector 全部真实表单和操作重新扫描后，任一维小于 `44px` 的交互目标数量为 `0`。
- 成员列表和会议产物台账均完成滚动后视觉复核；操作未遮挡标题，select 说明、当前值和下拉箭头完整可见。visualViewport 为 `320x568 / scale 1`，无横向溢出，console 为 `0`。
- exact QA 仅使用系统临时 runtime、显式临时 SQLite 与随机回环端口 `50958`；最终 `2` 次 focus preview GET，Provider、市场读取、业务写入和出站连接均为 `0`，stderr `0 B`。实例关闭后 `50958 / 8770 / 11111 / 18787` 均无 listener，正式 SQLite 指纹不变。

### 边界

- 双维度审计覆盖当前 exact fixture 实际渲染的 Inspector 交互元素；条件未渲染的插件、错误态、归档扩展态与独立懒加载 dialog 需要各自 fixture，不能由本轮结果外推。
- 触控几何和视觉结果来自 Chromium，不等同于实体触控设备、真实屏幕阅读器、IME、原生 zoom 或非零物理 safe-area 验收。

## 2026-08-22 移动 Inspector sticky 分区索引

### 现状与实施

- exact fixture 的移动 Inspector 高 `5,262px`，内部没有 `nav`、navigation role 或 fragment link；桌面 Icon Rail 在 `320px` 为 `display:none / 0px`。目标、模型、流程、成员、资料、产物已有部分稳定 ID，但结构复核与收敛门没有可定位身份。
- `RoomInspector` 在移动端新增“房间信息分区”语义导航，固定提供“目标 / 结构 / 门禁 / 成员 / 资料 / 产物”六个原生 fragment link；结构复核和收敛卡片补齐稳定 ID。导航在桌面默认 `display:none`，不替代 Icon Rail。
- 移动导航位于 Inspector 自身滚动容器内并 sticky 在顶部，链接为 `48x44px`、可横向滚动、保留可见细滚动条、scroll snap、safe-area padding 和 focus-visible；页面本身不获得横向滚动。
- 六个锚点 section 使用 `tabIndex=-1`，可由 fragment 导航接收焦点但不进入顺序 Tab 流；`scroll-margin-top:72px` 为实际 `67px` sticky 导航保留空间。

### 真实失败与修正

- 首轮浏览器验收发现初始 `scroll-margin-top:58px` 小于导航真实高度：结构、成员和产物目标均被覆盖约 `9px`，`targetClearOfNav=false`；原生 fragment 后活动元素落到 `BODY`。该轮没有按通过报告。
- 将 margin 调整为 `72px` 并给目标 section 增加 `tabIndex=-1` 后，四个实测跳转均把活动元素交给对应 `SECTION`，没有污染顺序 Tab 流。

### 自动化、build 与最终浏览器证据

- README 安全入口最终为 `100 files / 522 tests / 522 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM 未决 lazy gate。
- Production build：Vite `6.4.3`，`1743 modules`，`2.19s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `371.00 kB`（gzip `111.53 kB`），RoomInspector CSS `44.99 kB`（gzip `7.56 kB`），RoomInspector JS `88.50 kB`（gzip `27.00 kB`）。
- `320x568` 中导航宽 `270.59px`、client width `271px`、scroll width `324px`、高 `67px`；六个链接均为 `48x44px`，horizontal overflow 只由导航自身所有，document 无横向溢出。
- “结构 / 成员 / 产物 / 目标”真实点击后 Inspector `scrollTop` 分别为 `422 / 3456 / 4117 / 0`，页面 `scrollY` 始终为 `0`。目标起点相对 sticky 底部余量分别为 `4.69 / 5.22 / 5.22 / 0px`，均未遮挡；活动元素 ID 与 fragment 目标完全一致。
- Browser console 为 `0`。`1440x900` 中移动导航为 `display:none / 0x0`，Icon Rail 为 `display:flex / 64px`，目标区仍从 `y=0` 开始，无桌面布局位移。
- exact QA 仅使用系统临时 runtime、显式临时 SQLite 与随机回环端口 `53030`；最终 `3` 次 focus preview GET，Provider、市场读取、业务写入和出站连接均为 `0`，stderr `0 B`。实例关闭后 `53030 / 8770 / 11111 / 18787` 均无 listener，正式 SQLite 指纹不变。

### 边界

- 原生 fragment 会更新 URL hash，这是本批明确采用的浏览器语义；本批未把它外推为路由状态或深链接恢复合同。
- Chromium 已证明 section 焦点与滚动几何；真实屏幕阅读器对 fragment 导航后的朗读、实体触控横向滑动、IME、原生 zoom 和物理 safe-area 仍需独立覆盖。

## 2026-08-22 移动分区目标名称与 modal 生命周期

### 代码级语义

- 移动 fragment 已能把焦点交给 section，但目标、成员、资料和产物 section 原先只有可见标题，没有显式可访问名称；收敛 loading 分支也缺少与正常分支一致的名称。
- 六个导航目标现在分别发布“本轮目标 / 下一轮项目焦点 / 收敛与决策门 / 成员与身份 / 共享资料 / 结论与待办”。`tabIndex=-1` 继续只允许程序化或 fragment 聚焦，不加入顺序 Tab 流。
- `inspectorInformationArchitecture.test.js` 约束六个稳定 ID、焦点身份和名称一一对应；没有增加新的视觉层、请求或业务状态。

### 自动化、build 与浏览器证据

- README 安全入口最终为 `100 files / 522 tests / 522 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM 未决 lazy gate。
- Production build：Vite `6.4.3`，`1743 modules`，`2.34s`，exit `0`，无 warning。main CSS `191.60 kB`（gzip `35.64 kB`），main JS `371.00 kB`（gzip `111.54 kB`），RoomInspector CSS `44.99 kB`（gzip `7.56 kB`），RoomInspector JS `88.65 kB`（gzip `27.01 kB`）。
- `320x568` 中依次真实点击六个导航链接后，活动元素均为对应 `SECTION`，活动 ID 与 URL fragment 一致，可访问名称依次精确为上述六项；无缺名目标。
- 从 `#inspector-artifacts` 对应“结论与待办”section 直接按 Escape，Inspector 正常关闭，焦点精确恢复到“房间信息”按钮；fragment 按原生语义保留为 `#inspector-artifacts`，不声称它是应用深链接状态。
- 无横向溢出，Browser console 为 `0`。exact QA 仅使用系统临时 runtime、显式临时 SQLite 和随机回环端口 `63815`；最终 `1` 次 focus preview GET，Provider、市场读取、业务写入和出站连接均为 `0`，stderr `0 B`。实例关闭后 `63815 / 8770 / 11111 / 18787` 均无 listener，正式 SQLite 指纹不变。

### 边界

- DOM 已证明 section 的 ID、可访问名称、活动元素和 Escape 焦点恢复；真实屏幕阅读器是否按用户期望朗读 section 名称与内部摘要仍未覆盖。
- 本批未覆盖浏览器 Back/Forward 对多个 fragment 历史项的用户体验，也未把 fragment 持久化为房间状态。

## 2026-08-22 移动分区 `:target` 当前态

### 代码级证据

- `RoomInspector` 的六个移动分区入口继续使用原生同页 fragment；本批没有新增 React 当前项状态、`hashchange` 监听器或房间状态写入。
- 移动端样式通过六组限定在 `.room-inspector-workbench` 内的 `:has(#inspector-*:target)` 选择器，将 URL fragment 对应的唯一导航项显示为当前态。该状态直接派生自浏览器中的目标元素，不维护第二份 JavaScript 状态。
- 静态契约覆盖六个 `:target` 映射，并固定当前态的边框与背景标识；这只能证明源码结构与样式合同，不等同于真实浏览器、辅助技术或输入法行为。

### 自动化回归与 production build

- 使用 README 指定的安全入口 `AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test`：100 个测试文件，522 个测试，522 pass，0 fail。
- production build：Vite 6.4.3，1743 modules，2.19 s，退出码 0，无 build warning。
- 主要产物：主 CSS 191.60 kB（gzip 35.64 kB），主 JS 371.00 kB（gzip 111.53 kB），`RoomInspector` CSS 45.79 kB（gzip 7.68 kB），`RoomInspector` JS 88.65 kB（gzip 27.01 kB），`ArtifactDialog` JS 169.30 kB（gzip 50.38 kB），`react-vendor` 134.67 kB（gzip 43.23 kB）。

### 隔离浏览器证据

- 在 320x568 视口依次激活“目标 / 结构 / 门禁 / 成员 / 资料 / 产物”：每一步恰有一个当前态，文字分别与入口一致；目标 ID、目标分区可访问名称及 URL fragment 均逐项匹配。
- 六步中页面 `scrollY` 均为 0，未出现横向溢出，console 记录为 0。
- 隔离运行时使用系统临时目录、显式临时 SQLite 与随机回环端口 `56977`；状态端点记录 3 次只读焦点预览，Provider 调用、市场读取、业务写入及出站连接尝试均为 0，正式 SQLite 与正式端口均未使用。验收后相关进程已停止，`56977/8770/11111/18787` 均无 listener，stderr 为 0 B。

### 尚未覆盖的边界

- 浏览器控制器未可靠执行 Back/Forward，因此本批不声称已验证多个 fragment 历史项的后退/前进体验；`:target` 会以当前 URL fragment 为来源是代码与平台机制判断，不是本批直接交互证据。
- 本批没有真实 IME 候选窗、组合输入、屏幕阅读器浏览/朗读、触控设备 safe-area 或系统级 200% 缩放复验；已有源码合同和模拟视口证据不能替代这些真实环境验收。

## 2026-08-22 移动分区导航全量可见性

### 问题与实现

- 当前 production 基线在 320x568 下的房间信息面板宽 281.59 px，分区导航可用宽 271 px、内容宽 324 px；六个入口各 48x44 px，间距 4 px、横向内边距 8 px，导致“产物”仅露出一角并出现原生横向滚动条。
- 移动端导航改为六列 grid：`repeat(6, minmax(44px, 1fr))`、1 px 分隔、零横向内边距。现有 `overflow-x: auto` 保留，因此低于可容纳六个 44 px 目标的宽度时仍能滚动，不以缩小触控目标换取强行适配。
- 本批不改变导航文字、fragment、分区顺序、React 状态或房间持久化，只优化同一信息架构的首屏可见性；静态合同固定六列布局和 44 px 下限。

### 自动化与 production build

- README 安全入口通过 100 个测试文件，退出码 0；本批只在既有测试内增加断言，没有增加测试用例。
- Vite 6.4.3 production build：1743 modules，2.09 s，退出码 0，无 build warning。
- `RoomInspector` CSS 为 45.96 kB（gzip 7.70 kB）；主 CSS 191.60 kB（gzip 35.64 kB），主 JS 371.00 kB（gzip 111.54 kB），`RoomInspector` JS 88.65 kB（gzip 27.02 kB）。

### 浏览器证据

- 新构建在 320x568 下把六列均分为 44.265625x44 px；导航 client/scroll width 均为 271 px，`scrollLeft` 为 0，横向溢出从 53 px 降为 0，导航高度由 67 px 降为 57 px。
- 依次激活“目标 / 结构 / 门禁 / 成员 / 资料 / 产物”：每步恰有一个匹配当前态，fragment、目标 ID 与目标分区可访问名称逐项一致；导航 top 始终为 47.625 px，页面 `scrollY` 始终为 0。
- 320x568 页面无横向溢出、console 为 0；1440x900 下移动导航仍为 `display: none`，桌面检查侧栏宽 340 px，页面无横向溢出、console 为 0。
- 最终隔离实例使用随机回环端口 `51956` 与系统临时 SQLite，仅记录 1 次只读焦点预览；Provider、市场读取、业务写入和出站连接尝试均为 0，正式 SQLite 和正式端口未使用。实例停止后 `59686/51956/8770/11111/18787` 均无 listener。

### 证据边界

- 本批证明的是 Chromium Browser 控制器中的 320x568 与 1440x900 行为；没有新增真实设备 safe-area、系统级 200% 缩放、IME、屏幕阅读器或 Back/Forward 验收证据。
- 静态 CSS 合同只能防止布局声明被意外移除；44.265625 px 实际尺寸、无溢出与唯一当前态来自本批浏览器测量，二者不可互相替代。

## 2026-08-23 桌面检查侧栏粘性分区索引

### 问题与最终实现

- 1440x900 production 基线中，桌面房间检查侧栏宽 340 px，`.room-inspector-workbench` 可视高约 913 px、内容高约 4730 px，但六分区导航为 `display: none`；跳到深层“产物”后只能手动长距离滚动返回目标、结构或门禁。
- 701 px 及以上显示同一组六分区原生 fragment 入口：六列 grid、4 px 间距、每项至少 44x44 px；导航是 workbench 的直接子元素，并在真实侧栏滚动容器内使用 `position: sticky; top: 0`。
- 当前态仍直接派生自 `:target`，没有新增 React 状态或 `hashchange` 监听器。workbench 使用 `scroll-padding-top: 68px`，使目标标题落在粘性索引下方。
- 浏览器迭代发现 `#root` 原有 `overflow: hidden` 仍允许 fragment 进行程序化滚动，曾导致 `rootScrollTop=68`、导航离开视口。最终仅在桌面且 `.app-shell.inspector-open` 时把 `#root` 改为 `overflow: clip`：保留裁切，同时移除错误的外层滚动容器；移动抽屉继续使用原有 `overflow: hidden`。
- 新增独立静态契约，固定桌面媒体边界、root clip 限定、粘性六列、44 px 目标、六个当前态映射及 workbench 级滚动补偿；既有移动合同改为只统计桌面标记之前的六组映射。

### 自动化与 production build

- README 安全入口最终通过 101 个测试文件，523 个测试，523 pass，0 fail；新增 1 个桌面分区索引契约测试。
- Vite 6.4.3 production build：1743 modules，2.12 s，退出码 0，无 build warning。
- `RoomInspector` CSS 47.86 kB（gzip 7.95 kB）；主 CSS 191.60 kB（gzip 35.64 kB），主 JS 371.00 kB（gzip 111.53 kB），`RoomInspector` JS 88.65 kB（gzip 27.02 kB）。

### 浏览器证据

- 1440x900 首屏：导航宽 329 px、高 61 px，六列约 48.17x44 px，client/scroll width 均为 329 px，无横向溢出，console 为 0。
- 依次激活“目标 / 结构 / 门禁 / 成员 / 资料 / 产物”：六步均只有一个匹配当前态，fragment、目标 ID 与可访问名称逐项一致；`navTop=0`、`rootScrollTop=0`、页面 `scrollY=0` 始终不变，目标 top 约为 68 px。
- 320x568 回归：`#root` 仍为 `overflow: hidden`，导航保持 271/271 px、最小入口 44.265625x44 px、top 47.625 px，无导航或页面横向溢出，console 为 0。
- 最终隔离实例使用随机回环端口 `55929` 和系统临时 SQLite，仅记录 1 次只读焦点预览；Provider、市场读取、业务写入与出站连接尝试均为 0，正式 SQLite 与正式端口未使用。停止后 `50102/57093/54988/55929/8770/11111/18787` 均无 listener，且无项目 QA 进程。

### 证据边界

- 本批没有直接验证浏览器 Back/Forward 对桌面粘性索引的历史导航体验，也没有把 fragment 持久化为房间状态。
- 本批 Chromium Browser 控制器证据不替代真实屏幕阅读器、键盘完整遍历、系统级缩放、IME 或真实设备 safe-area 验收。

## 2026-08-23 Composer 提及菜单 stacking 修复

### 问题与代码级修正

- 320x568 的 12 人房间基线中，提及菜单宽 248 px、可视高 378 px、scroll height 612 px，12 个成员均在同一内部滚动容器中；单项尺寸与短视口高度合同本身没有问题。
- 遮挡来自 stacking context：菜单 `z-index: 20` 位于 `.composer { isolation: isolate }` 内，但 Composer 父层没有 z-index；时间线空状态卡是外部兄弟层 `z-index: 1`，因此覆盖菜单中段并阻断成员选择。
- 最终修正只在菜单存在时使用 `.composer:has(.mention-menu) { z-index: 4; }`。菜单关闭后 Composer 不保留抬升层级；`z-index: 300` 的全局错误提示仍高于菜单，错误信息不会被交互层隐藏。
- 新增独立静态契约，固定动态父 stacking context，不改菜单尺寸、成员顺序、滚动上限、键盘合同或提及解析逻辑。

### 自动化与 production build

- README 安全入口通过 102 个测试文件，524 个测试，524 pass，0 fail；新增 1 个 Composer stacking 契约测试。
- Vite 6.4.3 production build：1743 modules，2.09 s，退出码 0，无 build warning。
- 主 CSS 191.64 kB（gzip 35.65 kB）；主 JS 371.00 kB（gzip 111.54 kB）。`RoomInspector` CSS/JS 分别为 47.86/88.65 kB（gzip 7.95/27.01 kB）。

### 浏览器证据

- 最终 320x568 exact 房间中，4 人菜单与空状态卡实际相交 248x63 px；Composer z-index 为 4、空状态卡为 1，交点命中菜单项内部 `STRONG`，证明菜单位于背景卡上方。菜单项最小高度 50 px，页面无横向溢出，console 为 0。
- 选择“方案架构师”后菜单被移除、触发器 `aria-expanded=false`，textarea 获得焦点并写入 `@方案架构师 `；没有发送消息或启动新轮。
- 1440x900 下菜单为 248x214 px，完整位于视口内，Composer 仅在菜单展开时为 z-index 4；页面无横向溢出，console 为 0。
- 最终验收实例使用随机回环端口 `61884` 与系统临时 SQLite，Provider、market reads、业务写入与出站连接尝试均为 0，综合零使用标志为 true；正式 SQLite 和正式端口未使用。停止后 `56785/61884/8770/11111/18787` 均无 listener，且无项目 QA 进程。

### 安全与证据边界

- 为取得 12 人基线曾切换到隔离 fixture 中的存储房间，触发 2 次本地行情依赖访问；哨兵对象立即拒绝，`futu_or_opend_connected=false`、出站连接为 0，隔离脚本最终以 `a market dependency was accessed during QA` 失败退出。该实例只证明修复前的 12 人遮挡，不属于通过的安全验收。
- 修复后的直接运行时证据来自同样与空状态卡相交的 4 人 exact 菜单；动态 stacking 选择器不依赖成员数量，但本批不把这一代码推论表述为“修复后 12 人房间已在零 market-read fixture 中直接复验”。
- 本批没有真实 IME 组合输入、屏幕阅读器菜单朗读、完整键盘方向键遍历或触控设备验收。

## 2026-08-23 提及菜单键盘合同与有效 200% 视口

### 提及菜单键盘运行时证据

- 在 320x568 exact 房间中，提及菜单展开后触发器保留焦点；ArrowDown 把焦点送入第一项并继续移动，End 定位末项，Home 返回首项，首项 ArrowUp 循环到末项。
- Escape 关闭菜单、设置 `aria-expanded=false` 并把焦点恢复到“提及成员”触发器；重新展开后使用 End + Enter 选择“方案架构师”，菜单关闭、textarea 获得焦点并写入 `@方案架构师 `。
- 全路径 console 为 0，没有发送消息、启动新轮或产生业务写入。该直接证据来自 4 人 exact 菜单，不替代 12 人菜单的完整键盘实测，也不等同于屏幕阅读器朗读验证。
- 现有 `role=menu/menuitem` 与实际方向键、首尾键、循环、Escape 和 Enter 行为一致，因此本批没有为制造代码变化而改写已正确的焦点实现。

### 720x450 有效 200% 视口

- 以 1440x900 物理画布折算后的 720x450 CSS 视口审计当前 production：页面无横向或纵向根级溢出，header 高 56 px，时间线可视高 111 px，Composer 高 106 px，全部位于 450 px 视口内。
- 打开房间信息后得到 340 px 宽的紧凑覆盖抽屉与 scrim；关闭控件可见，六分区索引高 61 px，页面无横向溢出。
- 跳到“产物”时侧栏 scrollTop 为 3419，`rootScrollTop=0`、页面 `scrollY=0`，粘性索引保持在抽屉顶部；Escape 关闭后焦点恢复到“房间信息”，root 从桌面检查态的 `overflow: clip` 恢复为 `overflow: hidden`。
- 全路径 console 为 0。最终实例使用随机回环端口 `56019` 与系统临时 SQLite，仅有 1 次只读焦点预览；Provider、market reads、业务写入与出站连接尝试均为 0，综合零使用标志为 true。停止后 `56019/8770/11111/18787` 均无 listener，且无项目 QA 进程。

### 证据边界

- “有效 200%”仅指 Browser 控制器中的 720x450 CSS 视口，不是 Windows 显示缩放、浏览器原生 zoom=200%、设备像素比或真实高 DPI 文本栅格化验收。
- 本批没有源码或测试代码变化；自动化/build 仍采用紧邻此前且随后用于本次 production 浏览器验收的 102 文件、524/524 pass 与 1743 modules、2.09 s build 结果。

## 2026-08-23 证据状态条移动网格修复

### 代码级证据

- 根因是同一证据复核容器的后加载 refinement 样式把 `artifact-evidence-review` 容器名覆盖为 `evidence-review`，使既有 650px 单列 container query 不再命中。
- `artifact-evidence-review-refinement.css` 现在同时声明 `artifact-evidence-review evidence-review`，恢复原单列合同并保留 refinement 的 720px/560px ledger 合同。
- `artifactEvidenceReviewUi.test.js` 已升级为锁定双容器名，避免后续再次静默覆盖任一响应式合同。
- `EvidenceStatusStrip` 使用稳定领域键，视觉列表固定命名，动态汇总由独立 `role="status"` 节点播报；这些属于源码和运行时 DOM 证据，不等同于真实屏幕阅读器验收。

### 自动化与 production build

- README 安全入口完整回归：102 个文件，524 tests，524 pass，0 fail。
- Production build：Vite 6.4.3，1743 modules，2.06s。
- 主要产物：index CSS 191.64 kB（gzip 35.65 kB），index JS 371.00 kB（gzip 111.53 kB），RoomInspector CSS 50.96 kB（gzip 8.52 kB），RoomInspector JS 89.62 kB（gzip 27.27 kB），ArtifactDialog CSS 121.84 kB（gzip 19.28 kB）。

### 真实 Chromium 布局证据

- 320x568：`.artifact-evidence-progress` 为 177px 单列；状态条宽 177px，双列各 86px；四项均为 28px 高，无重叠；document 320/320，无根级横向溢出，console 为空。
- 1440x900：progress 保留三列 407.188px / 266.812px / 125px；状态条宽 266.812px，四项保持同一行；document 1440/1440，无根级横向溢出，console 为空。
- 模态打开后焦点位于“关闭会议产物工作区”，页面根滚动保持 0。

### 隔离与未覆盖边界

- 浏览器实例仅使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 51359。
- 最终哨兵：正式库未使用、8770 未使用、Futu/OpenD 未连接、Provider 0、market reads 0、outbound 0、业务写入 0；停止后 51359、8770、11111、18787 均无 listener，且无 QA 进程。
- 尚未执行真实屏幕阅读器播报、真实 IME 输入、操作系统级 200% 缩放、浏览器原生 200% zoom 或高 DPI 光栅验收，不能据此宣称这些边界已通过。

## 2026-08-23 冻结来源预览披露与长度语义

### 实施结论

- `EvidenceSourcePreview` 在来源身份或选中状态变化后，会把当前选中来源的 `<details>` 同步为展开态；这修复了仅依赖 `defaultOpen`、后续选中切换不生效的问题。
- 披露摘要保持原生 `<summary>` 语义，并增加显式方向箭头；`Enter` 与空格的键盘处理只切换当前 `<details>.open`，且调用 `preventDefault()`，避免浏览器差异造成重复切换。
- 预览截断现在分别保留原始字符数与实际显示字符数；达到 12,000 字符上限时，文案不再把省略号误计入可见字符数。
- `<pre>` 通过 `aria-describedby` 关联字符统计说明；图标继续使用 `aria-hidden`，没有把装饰性内容加入可访问名称。
- 移动端保留直接 summary grid，未引入额外包装层；展开态摘要高度回到 92 px，避免中间包装层造成的 108 px 回归。

### 代码级证据

- 自动展开由 `selected` 与来源 `identity` 驱动的 effect 完成；这是 React 状态切换契约，不等同于辅助技术实测。
- 键盘处理显式覆盖 `Enter` 与空格，并在 summary 本身监听；测试固定了键值判断和 `preventDefault()` 契约。
- 来源预览模型同时返回 `originalLength`、`visibleLength` 与 `truncated`；字符说明由稳定 id 连接到预览正文。
- 样式显式隐藏 WebKit 默认 marker、绘制旋转箭头，并为 `prefers-reduced-motion` 关闭该过渡。

### 真实 Chromium 浏览器证据

- `320x568`：选中来源初始自动展开；展开面板宽 154 px，summary 为 `138x92` px。鼠标点击可收起，`Enter` 可重新展开，空格可再次收起；两次键盘切换后焦点均留在 `SUMMARY`。
- `320x568`：根元素 `clientWidth/scrollWidth` 为 `320/320` px，body scroll width 为 320 px；`visualViewport` 为 `320x568`、scale 1、offset 0；console 为空。
- `320x568`：预览正文的 `aria-describedby` 实际解析到字符说明，测试数据显示 `60 字符`；箭头在展开/收起时实际发生旋转。
- `1440x900`：展开面板宽 809 px，summary 为 `793x52` px；根元素 `clientWidth/scrollWidth` 为 `1440/1440` px，焦点留在 `SUMMARY`，console 为空。
- QA 状态端点确认使用系统临时 SQLite、未使用正式数据库和 8770、Provider/行情/外连均为 0；停止后 55704、8770、11111、18787 均无 listener，且无 Python/Node QA 残留进程。

### 安全回归与 production build

- 安全入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际由 `frontend/scripts/run-tests-safe.ps1` 调度；没有直接执行 `node --test`，也没有恢复顶层 Vite/JSDOM lazy gate。
- 测试：102 个文件，524 tests，524 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules，2.06 s。
- 主要产物：`index-CoCzG0FX.js` 371.00 kB / gzip 111.54 kB；`react-vendor-BG6ruE9h.js` 134.67 kB / gzip 43.23 kB；`ArtifactDialog-b3OYI61R.js` 169.89 kB / gzip 50.59 kB。
- 主要样式：`index-B5p_-6Hy.css` 191.64 kB / gzip 35.65 kB；`ArtifactDialog-DSH5yu3c.css` 122.39 kB / gzip 19.38 kB。

### 未覆盖边界

- 本轮没有真实操作系统 IME 组合输入测试，也没有 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 的实际朗读与导航验证。
- `aria-describedby`、原生 summary、焦点保留和键盘切换是代码级与 Chromium 行为证据，不能替代屏幕阅读器可理解性结论。
- `visualViewport` 的回环桌面浏览器数据不能证明真实刘海屏、圆角屏或软键盘弹出后的 safe-area 行为。
- 前文的有效 200% CSS 视口是布局收窄证据；本小节没有执行浏览器原生 zoom 200%，因此不得把两者合并表述为原生缩放验收。

## 2026-08-23 项目实施前结构复核导航与长列表反馈

### 实施结论

- 主面板标题与三组缺口标题分别使用真实 `h3`、`h4`，包装容器为 `div`；没有把 heading 放入只允许 phrasing content 的 `span`。
- 主 section 通过 `aria-labelledby` 关联标题，通过 `aria-describedby` 关联只读权限边界；三组明细分别通过自己的 heading id 命名。
- 三步闸门各提供一个 44 x 44 px 的原生链接，fragment 精确指向对应明细 section。目标 section 使用 `tabIndex=-1`，浏览器导航后可获得焦点，并以 `:target` / `:focus` 显示明确轮廓。
- 长缺口列表继续按 40 项增量展开，并增加当前可见数量、剩余数量、只读进度线和 `role=status` / `aria-live=polite` 播报。
- 行键继续使用 `(code, itemKey)` 的 JSON 元组。归一化层会把同组重复元组判为完整性失败并隐藏全部指标，因此该键已由数据合同保证唯一，不增加索引或展示文案作为伪唯一后缀。
- 所有改动均位于展示层；readiness API、精确绑定、零预算、fail-closed 和用户最终决定边界没有改变。

### 真实 Chromium 浏览器证据

- 使用专用 `scripts/run_isolated_project_readiness_qa.py`、`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 SQLite 与随机回环端口 53278；没有使用 Action Desk fixture 冒充 readiness fixture。
- `320x568`：面板宽 243 px；真实 H3/H4 的父元素均为 DIV，主标题、权限边界和三组标题 id 均可解析。
- `320x568`：三个闸门链接均为原生 A 元素且实际尺寸 44 x 44 px，三个 fragment 目标均存在。
- `320x568`：点击结构缺口后 hash 更新到精确 section，活动元素为 SECTION，3 px focus/target outline 生效，目标顶部约 100.83 px。
- `320x568`：单项 fixture 的进度前景和轨道均为 64 px；状态实际为 `role=status`、`aria-live=polite`、`aria-atomic=true`。
- `320x568`：根元素 `clientWidth/scrollWidth` 为 `320/320` px，body scroll width 为 320 px；console 为空。
- `1440x900`：面板为 `871 x 812.42` px；闸门和三组明细均形成三列，每列 275 px；根元素 `clientWidth/scrollWidth` 为 `1440/1440` px。
- `1440x900`：使用精确可访问名称点击第三步后，hash 切换到证据缺口 section，目标获得焦点、3 px outline 生效，顶部约 101.06 px；console 为空。

### 安全回归与 production build

- README 安全入口实际运行 102 个文件、524 tests：524 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build 转换 1743 modules，耗时 2.05 s。
- `ArtifactDialog-D5967Afs.js` 为 170.75 kB / gzip 50.91 kB；`ArtifactDialog-B-He_zlU.css` 为 124.57 kB / gzip 19.73 kB。
- `index-BQF3BP-p.js` 为 371.00 kB / gzip 111.54 kB；`index-B5p_-6Hy.css` 为 191.64 kB / gzip 35.65 kB。

### 隔离与未覆盖边界

- QA 状态记录 2 次 readiness HTTP 请求、2 次服务尝试与 2 次成功；Provider 调用、市场读取、业务写入和 outbound connect attempt 均为 0。
- 专用 runner 未提供 `/__qa/stop`；该路径由 SPA fallback 返回首页。实例最终按 runner 自身说明使用 PTY `Ctrl+C` 退出，随后 53278、8770、11111、18787 均无 listener，且无 Python/Node QA 残留进程。
- 当前专用 fixture 每组只有 1 项，因此 40 项以上的“再显示”行为只有代码级和静态合同测试证据，没有真实浏览器长列表实测。
- 本轮没有真实屏幕阅读器朗读、操作系统 IME、软键盘、刘海安全区或浏览器原生 200% zoom 验收；不得用 Chromium DOM/焦点证据替代这些结论。

## 2026-08-23 Readiness 请求去重与长列表完成态

### 根因与实现

- 仅把 effect 从整个 `plan` 对象改为 `requestKey + room/artifact/version` 原语后，真实浏览器仍产生 2 次相同 readiness GET；这证明重复来源跨越单次等值重渲染，不能把原语依赖修改单独表述为已去重。
- 最终实现按完整 `requestKey` 共享 Promise。该 key 已包含房间、精确产物版本、插件快照及 contribution/adapter/port/view-model 合同哈希，因此只复用同一冻结读取身份，不把不同合同或版本合并。
- 缓存最多保留 32 个身份；失败 Promise 会立即从缓存移除，后续读取仍可重试。一个组件卸载只停止自身状态提交，不中断其他挂载方共享的只读请求。
- 该缓存仅存在于当前前端模块生命周期；页面完整 reload 会创建新的模块实例和新的 GET，不能表述为后端或跨会话缓存。
- 长列表在最后一页不再卸载分页按钮。完成态保留同一 button，显示“已显示全部”，设置 `aria-disabled=true` 并保持 no-op，因此激活最后 1 项后不会把焦点丢到 body。
- 专用 runner 现在生成 45 个有唯一 id 的待补需求；当前投影形成 81 个结构缺口。runner 在监听前强制要求结构缺口大于 40 且 `(code, item_key)` 身份唯一，否则 fail closed。
- 专用 runner 增加受 guard 保护的 `/__qa/stop`，通过 `server.shutdown()` 正常退出 `TemporaryDirectory`，不再依赖 PTY 中断作为唯一清理路径。

### 真实 Chromium 对照证据

- 缓存前控制组：同一路径打开一次产物后，`readiness_http_requests/service_attempts/service_successes` 均为 2。
- 缓存后：同一路径打开一次产物后，上述三个计数均为 1；完成两次分页、等待和视口切换后仍保持 1。
- `320x568` 初始：展示 `40 / 81`，进度前景 31.36 px / 轨道 64 px；分页按钮 `191 x 44` px，标签为“再显示 40 项，当前尚余 41 项”。
- 第一次激活后：展示 `80 / 81`，进度前景 63.36 px；同一按钮获得焦点，标签更新为“再显示 1 项，当前尚余 1 项”。
- 第二次激活后：展示 `81 / 81`，进度为 64 / 64 px；按钮仍为 44 px 高，`aria-disabled=true`，标签为“已显示全部 81 项”，焦点仍在该按钮。继续等待 900 ms 后焦点保持不变。
- 三个移动端阶段根元素均为 `clientWidth/scrollWidth = 320/320` px，body scroll width 为 320 px；console 为空。
- `1440x900` 完整 81 项状态：面板宽 871 px，三组明细列宽均为 275 px；根元素为 `1440/1440` px。完成态按钮 `138.44 x 44` px 且仍持有焦点，console 为空。

### 最终回归、构建与隔离状态

- README 安全入口：102 个文件、524 tests，524 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules，2.15 s。
- `ArtifactDialog-DV4Lyw_S.js` 为 171.30 kB / gzip 51.11 kB；`ArtifactDialog-ll06bj-e.css` 为 124.68 kB / gzip 19.75 kB。
- `index-CMilYLmn.js` 为 371.00 kB / gzip 111.53 kB；`index-B5p_-6Hy.css` 为 191.64 kB / gzip 35.65 kB。
- 最终 QA 状态：临时 SQLite，Provider 调用 0、市场读取 0、业务写入 0、outbound connect attempt 0。
- `/__qa/stop` 实际返回 `{ok:true, stopping:true}`；退出后 65197、8770、11111、18787 均无 listener，且无 Python/Node QA 残留进程。

### 剩余边界

- 本轮真实验证了 Chromium 中的分页数量、进度、焦点和网络计数，但没有使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 验证 live-region 和 `aria-disabled` 的实际朗读。
- 81 项桌面面板高度约 6165.86 px，这是完整展开后的真实内容高度；当前证据证明没有横向溢出，不等同于已完成虚拟滚动或超长文档阅读效率研究。
- 缓存的正确性建立在精确确认版本与合同哈希不可变的现有 readiness 合同上；若未来接口改为可变投影，必须重新设计失效策略，不能沿用当前结论。

## 2026-08-23 Readiness 长列表可逆收起与可见焦点

### 最终交互

- 上一节记录的完成态 `aria-disabled=true` 是中间方案，现已被可逆交互取代：结构列表展示全部 81 项后，同一按钮切换为“收起至前 40 项”。
- 收起不改变原始 rows、排序、id、heading 或 fragment，只把本地 `rowLimit` 恢复为 40；状态播报、进度和“再显示”文案同步恢复。
- 按钮通过 `data-list-action=more|collapse` 区分视觉状态。collapse 使用温暖但中性的强调色，不表达批准、赢家或 readiness 授权。
- 第一次实现只保持了 DOM 焦点；真实浏览器发现收起后按钮被上方行移除带到视口外约 -879 px。最终实现于 React 提交后的下一帧调用同一 ref 的 `scrollIntoView({block:"center", inline:"nearest"})`，同时保证焦点身份与视觉可见性。
- 定位使用即时滚动，不引入平滑动画；`prefers-reduced-motion` 边界不被削弱。

### 真实 Chromium 证据

- `320x568`、81 个结构缺口，实际循环为 `40 -> 80 -> 81 -> 40`。
- 初始 40 项面板高度 5137.17 px；80 项为 8684.67 px；81 项为 8753.36 px；收起后恢复为 5137.17 px。
- 81 项完成态按钮标签为“收起到前 40 项，当前共 81 项”，`data-list-action=collapse`，实际尺寸 `191 x 44` px，焦点保留。
- 收起后同一按钮恢复 `data-list-action=more` 和“再显示 40 项，当前尚余 41 项”；实际位置为 top 282.33 px / bottom 326.33 px，处于 568 px 视口内。
- 收起后继续等待 900 ms，按钮位置、焦点和 40 项状态均保持不变。
- 全循环根元素 `clientWidth/scrollWidth` 为 `320/320` px，body scroll width 为 320 px；console 为空。
- 全循环结束后 readiness HTTP requests、service attempts、service successes 均为 1；Provider、市场、业务写入和 outbound connect attempt 均为 0。

### 最终回归与构建

- README 安全入口：102 个文件、524 tests，524 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules，2.17 s。
- `ArtifactDialog-Mn8abuKT.js` 为 171.51 kB / gzip 51.24 kB；`ArtifactDialog-C3FJlCyR.css` 为 124.91 kB / gzip 19.79 kB。
- `index-GYDOooZ-.js` 为 371.00 kB / gzip 111.54 kB；`index-B5p_-6Hy.css` 为 191.64 kB / gzip 35.65 kB。
- `/__qa/stop` 正常退出；随后 61446、8770、11111、18787 均无 listener，且无 Python/Node QA 残留进程。

### 剩余边界

- 移动端初始 40 项仍产生约 5137 px 的面板高度；可逆收起解决了“展开后无法快速恢复”的问题，但没有证明 40 项就是最佳默认页长。
- 本轮没有真实屏幕阅读器验证“收起/再显示”文案与 live region 的朗读顺序，也没有触屏惯性滚动和软键盘状态测试。
- 不采用虚拟滚动是有意选择：当前优先保留完整语义列表、浏览器查找和 fragment/heading 导航。若未来引入虚拟化，必须先定义辅助技术与查找行为的等价合同。

## 2026-08-23 Readiness 移动端自适应页长

### 实现

- 桌面默认页长继续为 40；`max-width: 620px` 的窄视口默认页长改为 16。没有通过缩小字体、裁切内容、固定高度或嵌套滚动换取表面紧凑。
- `currentGapPageSize()` 在客户端首次挂载时读取同一 620 px media query，避免先渲染 40 再闪烁为 16。无 `window` 或无 `matchMedia` 的环境安全回退为 40。
- `MediaQueryList` 优先使用 `addEventListener/removeEventListener`，并为旧实现保留 `addListener/removeListener` 回退。
- 视口断点变化时，如果列表仍在旧默认页，行数切换到新默认页；如果用户已经展开超过默认页，则保留当前 rowLimit，不丢失阅读进度。
- “再显示”和“收起”都使用当前 pageSize，因此移动端循环为 16 项步进并收回 16，桌面仍为 40 项步进并收回 40。

### 真实 Chromium 证据

- `320x568` 初始展示 16 / 81 项，按钮为 `191 x 44` px，标签“再显示 16 项，当前尚余 65 项”；面板高度 3008.67 px。
- 相比此前移动端固定 40 项的 5137.17 px，初始高度减少 2128.50 px，约 41%。
- 移动端实际循环：`16 -> 32 -> 48 -> 64 -> 80 -> 81 -> 16`；对应中间面板高度为 3008.67、4427.67、5846.67、7265.67、8684.67、8753.36、3008.67 px。
- 完整展开后按钮标签为“收起到前 16 项，当前共 81 项”；收起后按钮恢复“再显示 16 项，当前尚余 65 项”，top 281.83 / bottom 325.83 px，焦点保持。继续等待 700 ms 后状态不变。
- 从收起的移动端切到 `1440x900` 后，默认行数自动变为 40，按钮标签恢复桌面 40 项步进，面板高度 3398.92 px；根宽为 `1440/1440` px。
- 桌面展开至 80 项后切换到 `320x568` 仍为 80，再切回桌面仍为 80，证明断点同步没有折回用户已展开的阅读进度。
- 视口模拟切换时浏览器工具将焦点转移到房间关闭控件，因此本轮只认定“行数进度保留”，不宣称模拟跨设备后分页按钮焦点保持。
- 所有移动和桌面状态根元素均无横向溢出，console 为空。

### 最终回归、构建与隔离

- README 安全入口：102 个文件、524 tests，524 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules，2.48 s。
- `ArtifactDialog-DQG2GCXm.js` 为 172.06 kB / gzip 51.44 kB；`ArtifactDialog-C3FJlCyR.css` 为 124.91 kB / gzip 19.79 kB。
- `index-C21snamB.js` 为 371.00 kB / gzip 111.55 kB；`index-B5p_-6Hy.css` 为 191.64 kB / gzip 35.65 kB。
- 最终 readiness HTTP requests、service attempts、service successes 均为 1；Provider、市场、业务写入和 outbound connect attempt 均为 0。
- `/__qa/stop` 正常退出；随后 56143、8770、11111、18787 均无 listener，且无 Python/Node QA 残留进程。

### 剩余边界

- 16 是基于当前 81 项 fixture 和 320x568 布局得到的工程默认值，不是用户研究证明的最佳认知负荷阈值。
- 本轮没有真实设备旋转、分屏、软键盘或屏幕阅读器测试；matchMedia 行为证据来自桌面 Chromium 视口模拟。
- 自适应页长不改变服务端上限、投影完整性或全部缺口数量，只控制当前 DOM 中的只读可见前缀。

## 2026-08-23 Readiness 闸门分组披露

### 实现

- 三个缺口组继续使用 section + H4 + 精确 fragment id；组体改为具有稳定 id 的持久容器，通过 `hidden` 控制布局与辅助技术暴露，不删除 rows、顺序或列表语义。
- 默认只展开第一个有缺口的闸门：有 blocker 时先展开阻断条件；无 blocker 时依次选择结构、证据；全部 clear 时保留第一组作为说明入口。
- 每个组头新增 44 x 44 px 原生 button，`aria-expanded` 与 `aria-controls` 精确关联组体，Chevron 仅作装饰并随状态旋转。
- 三步闸门 fragment 链接会先把目标 key 加入展开集合，再执行原生 fragment 导航；目标 section 仍获得焦点和 target outline。
- 披露不是互斥 accordion。用户可同时展开多组，也可全部收起；标题、状态和数量始终可见。
- 当前运行环境中仅依赖原生 button 合成 click 时，真实 CUA 的 Enter/Space 没有改变状态。最终 toggle 显式处理 Enter 与空格并调用 `preventDefault()`，避免与浏览器合成 click 重复。

### 真实 Chromium 移动端证据

- `320x568` 默认状态：仅 1 项 blocker 组展开，结构 16 项和证据 1 项组体 hidden；面板高度 1415.77 px。
- 相比上一阶段“16 项但三组均展开”的 3008.67 px，再减少约 53%；相比最初固定 40 项的 5137.17 px，累计减少约 72%。
- 三个 toggle 均为 `44 x 44` px，三个 `aria-controls` 均解析到现存 body id；展开状态与 body.hidden 一致。
- 点击第二步结构导航后，结构组自动展开，section 获得焦点，fragment 精确更新，目标顶部约 101.42 px；阻断组保持展开，面板高度 2944.77 px。
- 收起 blocker 后只保留结构组展开，面板高度 2810.08 px；全部组收起时为 1281.08 px。
- 从全部收起状态再次点击结构导航，结构组重新展开并定位到约 100.73 px；分页到 32 项后仍无横向溢出。
- 全部移动端状态根元素均为 `320/320` px，console 为空。

### 真实 Chromium 桌面与键盘证据

- `1440x900` 三组继续为三列，每列 275 px；组头 toggle 保持 44 x 44 px，根元素为 `1440/1440` px。
- 默认 blocker 展开、其他组收起时面板高度 683.36 px；结构组展开 40 项后为 3402.92 px，再收起恢复 683.36 px。
- 通过结构 fragment 导航使离屏组先展开并进入视口；点击 toggle 收起后，焦点留在该 button。
- 聚焦 toggle 后，Enter 实际把结构组从 hidden 展开，空格实际再次收起；两次状态切换后焦点均留在同一 button，console 为空。
- 一次未先定位离屏组的自动化 click 没有改变状态；通过真实 fragment 用户路径后指针与键盘均成功，因此不将该自动化定位失败归类为产品缺陷。

### 最终回归、构建与隔离

- README 安全入口：102 个文件、524 tests，524 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules，2.65 s。
- `ArtifactDialog-CAmsnwz4.js` 为 172.91 kB / gzip 51.72 kB；`ArtifactDialog-Dbn2o-L_.css` 为 125.98 kB / gzip 19.95 kB。
- `index-1cjME8LG.js` 为 371.00 kB / gzip 111.53 kB；`index-B5p_-6Hy.css` 为 191.64 kB / gzip 35.65 kB。
- 最终 readiness HTTP requests、service attempts、service successes 均为 1；Provider、市场、业务写入和 outbound connect attempt 均为 0。
- `/__qa/stop` 正常退出；随后 63482、8770、11111、18787 均无 listener，且无 Python/Node QA 残留进程。

### 剩余边界

- `hidden` 组体在 DOM 中保留但不会作为可见内容暴露；本轮没有 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 的真实展开/收起朗读验证。
- 浏览器页面查找通常不会命中 hidden 文本；如果未来要求跨折叠组全文搜索，需要设计显式只读筛选或搜索入口，不能把 DOM 保留等同于可搜索。
- 默认首个缺口组展开是按现有“blocker -> structural -> evidence”流程合同推导，不是用户研究证明的唯一最佳展开策略。

## 2026-08-23 Readiness 跨组只读缺口定位器

### 实现

- 在 readiness 摘要与闸门之间增加 `role=search` 的只读定位器，可匹配规范化投影中的 `message`、`itemKey` 和 `code`；查询最多 120 字符，大小写不敏感。
- 定位只构造展示副本，不改变 projection、rows 顺序、原始总数、metrics、闸门状态或请求身份，也不会触发新的 API、Provider、市场读取或写入。
- 查询非空时，各组 header 使用 `MATCH` 和当前命中数；有命中的组自动展开，无命中的组自动收起。原始三步 metrics 始终显示真实 `01 / 81 / 01`。
- 零命中状态通过 `role=status`、`aria-live=polite` 和 `data-empty=true` 明确显示 `0 / total`；不是把原始缺口改为空。
- 清除按钮只在有草稿时出现，尺寸 44 x 44 px；点击前先把焦点移回 search input，再清空草稿与已提交查询，避免按钮卸载造成焦点丢失。
- 输入区分 IME composition 草稿和已提交查询：composition 期间只更新可见输入值，compositionend 后才提交筛选；普通 onChange 立即提交。该行为目前只有代码级合同证据。

### 真实 Chromium 移动端证据

- `320x568` 初始：输入宽 188.63 px、高 44 px；状态为“可定位 83 项合同内缺口；原始闸门计数保持不变”，默认 blocker 展开，面板高度 1598.92 px。
- 搜索结构代码 `REQUIREMENT_NOT_CONFIRMED`：命中 40 / 83，仅结构组自动展开；group header 为 `MATCH 00 / MATCH 40 / MATCH 00`，原始 metrics 仍为 `01 / 81 / 01`。
- 搜索共享对象键 `risk_capacity_blocker`：实际命中 blocker、structural、evidence 各 1 项，共 3 / 83，三个组均自动展开。
- 搜索 `no_such_contract_gap`：命中 0 / 83，三个组均收起，`data-empty=true`；原始 metrics 不变。
- 清除后 clear button 卸载，焦点实际回到 input；默认 blocker 展开、原始 HOLD/OPEN 与 01/81/01 恢复。
- 有查询时输入宽 138.23 px，清除按钮 44 x 44 px；所有状态根元素为 `320/320` px，console 为空。

### 真实 Chromium 桌面证据

- `1440x900` 定位器总宽 841 px，grid 两列约 123.97 / 679.86 px；输入为 629.47 x 44 px，清除按钮为 44 x 44 px。
- 共享对象键命中 3 / 83 并展开三组时，面板高度 814.61 px；输入保持焦点，原始 metrics 仍为 01/81/01。
- 根元素为 `1440/1440` px，console 为空。

### 最终回归、构建与隔离

- README 安全入口：102 个文件、524 tests，524 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules，2.88 s。
- `ArtifactDialog-Dg5Ip6pv.js` 为 175.28 kB / gzip 52.46 kB；`ArtifactDialog-B-WPrLij.css` 为 128.07 kB / gzip 20.20 kB。
- `index-DlIHA_-4.js` 为 371.00 kB / gzip 111.54 kB；`index-B5p_-6Hy.css` 为 191.64 kB / gzip 35.65 kB。
- 筛选全过程 readiness HTTP requests、service attempts、service successes 均保持 1；Provider、市场、业务写入和 outbound connect attempt 均为 0。
- `/__qa/stop` 正常退出；随后 52990、8770、11111、18787 均无 listener，且无 Python/Node QA 残留进程。

### 剩余边界

- 本轮没有真实中文、日文或韩文操作系统 IME composition 输入；composition 分流是代码级证据，不能表述为真实 IME 已验收。
- 定位采用直接子串和简单小写归一化，不提供拼音、同义词、模糊匹配、正则表达式或跨语言语义搜索。
- 本轮没有屏幕阅读器实际验证动态 MATCH 计数、自动展开和 live-region 的朗读顺序。
- 筛选结果仍受 16/40 前缀分页约束；命中总数与当前渲染行数是不同概念，状态栏报告前者，组内分页报告后者。

## 2026-08-23 ProjectReadinessPanel 嵌套 lazy chunk 与复核回归

### 实施与代码级证据

- `ArtifactDialog.jsx` 不再同步导入 `ProjectReadinessPanel`；项目实施前结构复核改为 ArtifactDialog 内部的嵌套 `React.lazy`，没有恢复 App 顶层旧 Vite/JSDOM 未决 lazy gate。App 既有 lazy 边界数量仍由契约测试固定为 16。
- 新增显式 `Suspense` fallback：`aria-busy="true"`，文案限定为“只读结构复核模块”，并明确不产生 Provider 调用、市场读取、业务写入或授权结论。这是代码级与契约测试证据；本机加载过快，未形成可稳定观察的真实 fallback 停留态。
- `ProjectReadinessPanel` 的 refinement CSS 随模块独立拆分；契约测试固定无同步导入、精确动态导入、只读 fallback 和模块自有 CSS，避免后续无意重新并入 ArtifactDialog 首屏负载。

### 安全回归与 production build

- 仅通过 README 约定的 `frontend/scripts/run-tests-safe.ps1` 安全入口执行，环境使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`；没有直接执行 `node --test`。
- 完整结果：102 个测试文件，525 个测试，525 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules，2.51 s。
- 拆分后主要产物：`ArtifactDialog-DV727yYR.js` 149.44 kB / gzip 45.30 kB；`ProjectReadinessPanel-BCWA5kzK.js` 26.97 kB / gzip 8.92 kB；`ArtifactDialog-Dge5js2u.css` 116.05 kB / gzip 18.53 kB；`ProjectReadinessPanel-CCxJWsh-.css` 12.03 kB / gzip 2.25 kB；`index` JS 371.00 kB / gzip 111.53 kB，CSS 191.64 kB / gzip 35.65 kB。
- 与拆分前相比，未打开项目复核时 ArtifactDialog 自身 JS 从 175.28 kB / gzip 52.46 kB 降至 149.44 kB / gzip 45.30 kB，CSS 从 128.07 kB / gzip 20.20 kB 降至 116.05 kB / gzip 18.53 kB。
- 打开已确认项目复核后会额外加载 readiness chunk；两块合计 JS 为 176.41 kB / gzip 54.22 kB，CSS 为 128.08 kB / gzip 20.78 kB。因 chunk 包装开销，总已加载负载略高于拆分前，不能表述为“所有路径总体积下降”；收益仅是延后未使用的结构复核代码与样式。

### 隔离浏览器证据

- 专用 runner 使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 `64068`；正式 8770 未使用，未连接 Futu/OpenD、Provider 或外部网络。
- `320x568` 下 readiness 面板可见且 `aria-busy="false"`；根节点 `clientWidth=320`、`scrollWidth=320`，无横向溢出。定位输入框为 188.63 x 44 px，面板宽 243 px。
- 关闭并重新打开产物工作区后，焦点落在“关闭会议产物工作区”按钮，且仍位于最上层产物模态框内；readiness 面板继续可见。
- 多次关闭/重开期间，隔离 runner 最终计数仍为 readiness HTTP request 1、service attempt 1、success 1；Provider calls、market reads、business writes、outbound connect attempts 均为 0。fixture 写入只发生在服务启动前的临时 SQLite。
- 本批次受控浏览器 DOM 求值器不暴露原生 `performance`、console 监听或 DOM 创建接口，因此没有把 Resource Timing 文件序列或一条新的完整 console 流写成已验证证据。chunk 文件名和尺寸来自真实 production build，运行时证据限定为面板实际加载、交互、布局、焦点和服务端请求计数。
- 本批次未覆盖真实操作系统 IME 候选窗、组合输入法人工输入、NVDA/JAWS/VoiceOver 实机朗读，也未稳定捕获瞬时 lazy fallback；现有 composition、ARIA、键盘与焦点结论仍分别属于代码/自动化证据和浏览器 DOM 行为证据，不等同于真实 IME 或屏幕阅读器验收。

### 清理

- 浏览器 QA 标签已关闭并恢复视口；runner 已通过 `/__qa/stop` 退出。`64068`、`8770`、`11111`、`18787` 均无 listener，且无项目相关 Python/Node QA 残留进程。

## 2026-08-23 全局导航与房间侧栏可达性/视觉收敛

### 实施

- IconRail 保留 `section` 作为稳定列表键，并采用当前 section 单一 `tabIndex=0` 的 roving focus；除上下/Home/End 外，补齐左右方向键。方向键只移动焦点，不调用导航。
- 小于等于 760 px 的激活指示改为横向渐变边，不再沿用桌面右侧竖线；移动端 hover 不产生位移。当前 320 px 应用布局会隐藏 IconRail，因此该规则属于中间紧凑布局的代码级防回归证据。
- RoomSidebar 搜索输入新增 `aria-controls` 与 `aria-describedby`，结果摘要使用 `role=status`、`aria-live=polite`、`aria-atomic=true`，无检索时显示房间总数，检索时显示“可见/总数”。
- 房间时间由每行两次日期解析收敛为一次 `roomTimeDetails` 投影，同时保留 ISO `dateTime`、短标签和完整中文时间可访问名称。
- 当前房间增加克制的“当前”视觉徽标，已有 `aria-current=page` 仍是语义来源；分组标题在长列表内部粘性停靠，空结果增加解释和可恢复的“清除搜索”操作。
- 搜索框外框提升到 44 px；清除与空状态恢复按钮为 40 px。这里不把 40 px 表述为 44 px 触控目标，后续若项目统一采用更严格的 44 px 组件门槛仍可继续收敛。

### 安全回归与 production build

- 首次安全回归发现 `roomSidebarFocus.test.js` 仍绑定旧内联表达式和旧“个可见”文案；组件行为未失败。只更新两条过期源码契约后，重新从 README 约定的 `frontend/scripts/run-tests-safe.ps1` 入口执行。
- 最终结果：102 个测试文件，525 个测试，525 pass，0 fail，0 cancelled，0 skipped，0 todo；没有直接执行 `node --test`。
- production build：1743 modules，2.37 s。
- 当前 `index` JS 为 372.17 kB / gzip 111.91 kB，CSS 为 193.07 kB / gzip 35.88 kB；相对上一批分别增加约 1.17/0.38 kB JS 和 1.43/0.23 kB CSS。ArtifactDialog 与 ProjectReadinessPanel 的独立 chunk 边界保持不变。

### 隔离浏览器证据

- runner 使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 `55093`；未使用正式数据库或 8770。
- `1440x900`：根节点 `clientWidth=1440`、`scrollWidth=1440`，最终 `clientHeight=900`、`scrollHeight=900`；无横向或持续纵向页面溢出。4 个 rail 按钮中仅当前项 `tabIndex=0` 且仅一项 `aria-current=page`。
- 桌面方向键：从“讨论房间”按 ArrowRight 后焦点到“成员身份”，当前页面仍为“讨论房间”；ArrowLeft 返回，证明焦点浏览不触发导航。
- 桌面搜索：初始摘要“6 个房间”；未命中时为“0/6 个匹配”、列表标签变为“房间搜索结果”，显示解释与清除操作；清除后恢复 6 行并把焦点送回搜索输入。
- `320x568`：根节点 `clientWidth=320`、`scrollWidth=320`。房间侧栏为 `281.59x568` 的 `aria-modal=true` dialog，打开后焦点在关闭按钮且位于 dialog 内；搜索框外框 44 px，当前房间徽标唯一。
- 窄屏未命中时，搜索清除按钮和空状态恢复按钮均为 40 px；清除后焦点回到输入。空检索时按 Escape 关闭侧栏，焦点恢复到“打开房间列表”，根节点仍为 320/320。
- 视觉截图检查确认品牌、主操作、搜索、分组标题、当前房间徽标和房间卡片层级没有遮挡；这属于本机浏览器的视觉检查，不等同于跨浏览器截图基线。
- 本批浏览器操作没有打开 readiness 或触发业务动作：readiness HTTP/service 计数均为 0；Provider calls、market reads、business writes、outbound connects 均为 0。fixture 仅在启动前写入临时 SQLite。
- 本批未使用真实 IME 候选窗、NVDA/JAWS/VoiceOver，也没有取得新的完整 console 事件流；源码契约和 DOM 行为不能替代真实辅助技术验收。

### 清理

- QA 标签已关闭并恢复视口；runner 已通过 `/__qa/stop` 退出。`55093`、`8770`、`11111`、`18787` 均无 listener，且无项目相关 Python/Node QA 残留。

## 2026-08-23 讨论时间线返回路径与搜索焦点

### 实施

- `ChatTimeline` 将“用户已离开底部”与“底部出现新消息”拆成独立状态。非搜索视图中，用户即使没有收到新消息，主动上滚后也会得到“回到最新”入口；只有新消息到达时才显示“有新消息”语义和 `data-new-messages=true`。
- 返回按钮采用 44 px 最小高度、safe-area 定位、普通返回/新消息两种克制视觉状态，并保留 forced-colors 与 reduced-motion 边界。
- 搜索输入增加 `aria-controls=chat-timeline-log`、Escape 清除和清除成功后的输入焦点恢复；桌面清除控件为 34 px，`<=430px` 时输入与清除控件均为 44 px。
- 浏览器首次验证发现平滑滚动中间事件会重新显示返回按钮，并在按钮卸载后把焦点落到 `BODY`。第一轮改为直接设置 `scrollTop` 后，仍受全局 `scroll-behavior: smooth` 影响。
- 最终实现仅在“回到底部”这一次操作内临时把容器内联 `scrollBehavior` 设为 `auto`，同步定位并聚焦时间线，下一动画帧恢复原内联值。消息引用跳转仍使用 `preferredScrollBehavior()`，没有永久关闭其他滚动动画。

### 安全回归与 production build

- 最终仅从 README 约定的 `frontend/scripts/run-tests-safe.ps1` 安全入口执行；没有直接运行 `node --test`。
- 一次中间回归发现 `liveRegionAnnouncements.test.js` 仍硬编码两处 `preferredScrollBehavior()`；更新为“一处引用跳转偏好 + 一条确定性返回路径”的真实契约后，最终 102 个测试文件、525 个测试全部通过，0 fail/cancelled/skipped/todo。
- 最终 production build：1743 modules，2.47 s。
- 当前 `index` JS 为 373.07 kB / gzip 112.22 kB，CSS 为 193.81 kB / gzip 36.02 kB；相对上一批约增加 0.90/0.31 kB JS 和 0.74/0.14 kB CSS。既有 lazy chunk 边界保持不变。

### 隔离浏览器证据

- 验收使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口。
- 初始项目房间为空时间线；直接进入无市场能力的“方案共创会”后，得到 4 条本地 fixture 消息，时间线 `scrollHeight=850`、`clientHeight=493`，最大滚动距离 357 px。
- 将第一条消息滚入视口后，普通返回按钮显示“回到最新”、`aria-label=回到时间线底部`、`data-new-messages=false`。最终点击后 100 ms 内从 `0/357` 到 `357/357`，底部距离 0，按钮消失，焦点位于 `aria-label=讨论消息时间线` 的滚动容器，临时内联 scroll behavior 已恢复为空值。
- `1440x900` 根节点为 `1440/1440`，无横向溢出。
- `320x568` 下搜索表单宽 292 px，输入和清除按钮均为 44 px；输入未提交关键词后按 Escape，值清空、清除按钮消失、焦点仍在搜索输入。根节点 `320/320`、`568/568`，无页面溢出。
- 视觉截图检查确认窄屏 masthead、搜索、投影账本、消息时间线与 Composer 没有遮挡，滚动条和信息层级可辨。该结论不等同于跨浏览器截图基线。

### 安全路径偏差记录

- 一次探索性运行先误入“美国存储产业投资委员会”，触发隔离 backend 的 `STORAGE_MARKET.status()` 与 `snapshot()` 路由各一次。保护 stub 在依赖访问点抛出 AssertionError，runner 最终记录 `market_reads=2` 并以 RuntimeError、退出码 1 失败关闭。
- 该无效运行没有连接 Futu/OpenD，没有 outbound connect，没有业务写入，也没有使用正式数据库；但因 market read 计数非零，明确不作为安全验收通过证据。
- 后续两次只从初始项目房间直接进入“方案共创会”；最终端口 `58688` 的计数为 market reads 0、Provider calls 0、business writes 0、outbound connects 0、readiness HTTP/service 0，`zero_external_and_business_use_verified=true`。

### 未覆盖边界与清理

- 本批未模拟真实新消息从服务端到达，因此“有新消息”变体具备状态与源码契约证据，但浏览器实测覆盖的是普通主动上滚返回变体。未做真实 IME、屏幕阅读器或新的完整 console 流采集。
- QA 标签均已关闭并恢复视口；所有 runner 已退出。`57343`、`54486`、`58688`、`8770`、`11111`、`18787` 均无 listener，且无项目相关 Python/Node QA 残留。

## 2026-08-23 收敛卡技术门证据进度

### 实施

- `ConvergenceCard` 删除手工维护的 `4 + 可选项` 门禁总数公式，改为由与实际 `GateRow` 相同顺序的 `technicalGateStates` 投影统一派生总数、满足数和剩余数，降低可选门禁变化时的计数漂移风险。
- 卡片在总览与首要阻断之间新增原生 `<progress>` 技术门证据摘要，显示“已满足 / 总数”和剩余项。进度文案明确“仅表示证据条件”；全部技术门满足时仍显示“需用户复核，不产生执行授权”。
- 进度条使用蓝灰渐变而非通过绿；blocked/review/candidate 的原有状态色、首要阻断、下一步、用户最终决定门和底部授权边界保持独立。
- candidate badge 从“候选可展示”校准为“条件候选可展示”，避免把可展示误读为推荐或批准。
- 其他阻断行的 React key 从单独 `blocker.code` 改为 `code + 当前投影索引`，即使后端重复 code 也不会产生同级重复键；这些行是非交互展示，重排时允许重新挂载。

### 安全回归与 production build

- 仅从 README 约定的 `frontend/scripts/run-tests-safe.ps1` 安全入口执行；没有直接运行 `node --test`。
- 完整结果：102 个测试文件，525 个测试全部通过，0 fail/cancelled/skipped/todo。
- production build：1743 modules，2.22 s。
- 收敛卡属于既有 RoomInspector lazy chunk：该 chunk CSS 为 52.43 kB / gzip 8.73 kB，JS 为 90.29 kB / gzip 27.49 kB；相对上一批约增加 1.47/0.21 kB CSS 和 0.67/0.21 kB JS。主 `index` JS 保持 373.07 kB，gzip 112.20 kB；CSS 保持 193.81 kB / gzip 36.02 kB。

### 隔离浏览器证据

- runner 使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 `60979`；只停留在初始项目房间。
- `1440x900` 阻断态卡片显示 `1 / 6`，progress 的可访问名称为“已满足 1 / 6 项技术门禁”，说明为“尚有 5 项技术门未满足；进度仅表示证据条件。”实际渲染的技术 GateRow 数为 6，与分母一致。
- 首要阻断“尚未开始讨论”、下一步“先定义本轮目标并发起一轮”和“最终决定属于用户，禁止真实下单”边界均保持在折叠明细之外。技术明细默认关闭；summary 高 48 px，展开后仍为 6 个技术门和 7 个其他阻断。
- 桌面根节点 `1440/1440`、`900/900`，无页面溢出。视觉检查确认蓝灰细进度条与红色阻断、琥珀下一步、用户决定门没有混色。
- `320x568` 通过“门禁”分区导航后，焦点位于 `#inspector-convergence`；卡片宽 270.59 px，进度面板宽 238.59 px，进度条宽 216.59 px。标题与 `1 / 6` 在小容器内纵向排列，根节点 `320/320`、`568/568`，无页面溢出。
- 最终 runner 计数：market reads 0、Provider calls 0、business writes 0、outbound connects 0、readiness HTTP/service 0，`zero_external_and_business_use_verified=true`。

### 未覆盖边界与清理

- 浏览器 fixture 只覆盖 blocked 状态；“技术门全部满足”文案、review/candidate 色调、forced-colors 和 progress 的屏幕阅读器朗读属于源码/CSS 契约证据，尚未做对应真实状态与辅助技术实机验收。
- QA 标签已关闭并恢复视口；runner 已退出。`60979`、`8770`、`11111`、`18787` 均无 listener，且无项目相关 Python/Node QA 残留。

## 2026-08-23 下一轮项目焦点结构语义与范围提示

### 实现

- `ProjectRoundFocusCard` 的宿主 `section` 现由真实 `h3` 通过 `aria-labelledby` 命名；冻结记录与只读预览仍保留各自可见模式标签，未改变加载、封印或授权语义。
- 焦点列表在存在合同焦点项时显示 `已显示 / 总数` 数据，并由可见标题命名；最多展示前三条的原有审阅边界保持不变。
- “填入下一轮目标”显式通过 `aria-describedby` 关联“只预填 / 不自动开始 / 用户最终决定”边界。
- 目标能力标签使用 `capability + sibling index` 组合键，重复能力值不会产生 React 兄弟键冲突。
- 新增中性范围胶囊、窄容器堆叠和 forced-colors 合同；模式标签提升到 10px，操作边界主/辅文案提升到 10px / 9px。

### 安全回归与 production build

- 只通过 `frontend/scripts/run-tests-safe.ps1` 对应的 npm 安全入口执行；未直接调用顶层 `node --test`。
- 中间回归捕获两条仍要求旧硬编码 `aria-label` 的静态契约，已改为匹配真实标题关系；最终权威结果为 102 个测试文件、526 tests、526 pass、0 fail、0 cancelled、0 skipped、0 todo。
- production build 成功：Vite 6.4.3，1743 modules，2.57s。
- `RoomInspector` CSS 53.40 kB / gzip 8.85 kB；JS 90.80 kB / gzip 27.64 kB。
- `index` CSS 193.81 kB / gzip 36.02 kB；JS 373.07 kB / gzip 112.20 kB。
- `ArtifactDialog` JS 149.44 kB / gzip 45.30 kB；`ProjectReadinessPanel` JS 26.97 kB / gzip 8.91 kB。

### 隔离浏览器证据

- 运行约束：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口；一次性临时房间仅启用 `project_round_focus` capability pack。
- 1440x900：真实 bootstrap 卡片 329 x 468.05px；`section[aria-labelledby]` 精确解析到可见 `h3`“下一轮项目焦点”；预填按钮和操作边界 ID 精确一致；页面 1440 / 1440，无横向溢出。
- 320x568：分区链接把焦点移动到 `#inspector-project-focus`；卡片 270.59 x 500.14px，卡片 271 / 271、页面 320 / 320，无横向溢出；标题改为纵向布局，模式标签 10px，边界主/辅文案 10px / 9px。
- 有效 200% CSS 视口 720x450：卡片 329 / 329、页面 720 / 720，无横向溢出，分区定位后目标卡持有焦点。
- 点击“填入下一轮目标”后检查器关闭，Composer 获得真实焦点并仅填入临时房间目标；未发起写请求或自动开始讨论。
- 浏览器 console 为 `[]`。
- 最终隔离状态：临时数据库 true；正式数据库 false；8770 false；Futu/OpenD/SEC/IR false；Provider calls 0；external market reads 0；outbound attempts 0；post-start business writes 0；数据库自服务启动后未变化。随机端口及 8770、11111、18787 均无 listener，无项目 QA 残留进程。

### 证据边界

- 真实浏览器状态是无确认产物的 bootstrap，因此合同焦点项为 0，“已显示 / 总数”范围标识按设计不渲染。该标识本轮由源码结构与专属契约测试覆盖，不能声称已完成 populated 焦点状态的真实浏览器验收。
- `aria-labelledby`、`aria-describedby`、容器查询和 forced-colors 均有代码级证据；本轮没有真实屏幕阅读器、forced-colors 实机或 IME 输入验收，不能将代码合同等同于辅助技术实测。

## 2026-08-23 决策研究谱系审阅信号与决策包语义

### 实现

- `DecisionPackageCard` 现在以 `article[aria-labelledby]` 关联真实 `h4`；可见标题仍显示链状态，屏幕阅读器文本追加候选快照标题，多个决策包不再是无名文章。
- 顶部四项摘要改为带真实 `data[value]` 的当前、历史、异常、未关联计数，并增加独立审阅信号：输入阻断、需要复核或暂无结构待复核项三态互斥。
- “暂无待复核项”明确限定为没有异常链或未关联资源，并写明不代表研究结论获批；attention 计数也明确不用于排序或批准。
- AI 原提案下拉项使用 `id + sibling index` 组合键；采纳说明通过 `aria-describedby` 关联“至少 3 字”说明和 `length / 1000` 可见反馈。
- 新增审阅信号的桌面三列、440px 以下两列布局，以及 forced-colors 边框与文字合同；不改变决策链计算、模拟派生门、写入请求或最终用户决定边界。

### 安全回归与 production build

- 只通过 `frontend/scripts/run-tests-safe.ps1` 对应 npm 安全入口执行；未直接调用顶层 `node --test`。
- 最终结果：102 个测试文件、527 tests、527 pass、0 fail、0 cancelled、0 skipped、0 todo。
- production build：Vite 6.4.3，1743 modules，2.42s。
- `DecisionLineagePanel` CSS 12.31 kB / gzip 3.06 kB；JS 15.63 kB / gzip 5.83 kB。
- `decisionLineageView` JS 6.71 kB / gzip 2.68 kB；`RoomInspector` JS 90.80 kB / gzip 27.64 kB；`index` JS 373.07 kB / gzip 112.20 kB。

### 隔离 production 浏览器证据

- 运行约束：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口 50477；正式数据库、8770、Futu/OpenD/SEC/IR 均未使用。
- 临时 bootstrap 及全部 6 个房间的 `decision_packages`、`paper_portfolios`、`observations` 均为 0；`DecisionLineagePanel` 按条件未挂载，不能声称完成 populated 面板浏览器验收。
- 1440x900：production 检查器已打开，页面 1440 / 1440，无横向溢出。
- 320x568：页面 320 / 320，检查器 271 / 271，无横向溢出；“产物”分区把真实焦点移到 `#inspector-artifacts`。
- 有效 200% CSS 视口 720x450：页面 720 / 720，检查器 329 / 329，无横向溢出。
- 新建浏览器标签的 production console 为 `[]`。
- 最终隔离状态：Provider calls 0；external market reads 0；outbound attempts 0；post-start business writes 0；数据库自服务启动后未变化；50477、55096、8770、11111、18787 均无 listener。

### 未形成证据的尝试与边界

- 在另一份系统临时 SQLite 中尝试用正式 `StudioStore` 流程构造最小决策链；草稿因“确认前必须通过证据包含检查：决策摘要缺少证据”被门禁拒绝。未绕过该合同，也未生成决策包。
- 随后尝试在系统临时目录建立明确标注的 synthetic frontend fixture，仅用于运行时结构检查；临时 Vite 与项目 production JSX 转换不一致，依次暴露 CommonJS React 导入和 `React is not defined`，面板始终未成功挂载。该尝试不构成项目 runtime 缺陷证据，也不构成视觉验收。
- synthetic fixture 的 Vite 父进程退出后仍有一个精确识别的子 `node.exe`；最终已停止，55096 无 listener，无 QA 运行进程。
- 本地安全策略拒绝删除含 junction 的临时 fixture 目录，因此 `C:\Users\Administrator\AppData\Local\Temp\ai-studio-lineage-fixture-c306dc336bbd4c1fb01f20ed436ca35c` 仍作为非运行临时文件残留；它位于项目树外，不进入源码备份。
- 决策包标题、审阅信号、字符反馈、440px 容器规则与 forced-colors 本轮只有源码和契约测试证据。真实 populated 决策包、AI 提案采纳表单、屏幕阅读器、forced-colors 实机及 IME 输入均未覆盖。

## 2026-08-23 纸面组合工作台结构与渐进展示

### 实现

- `PaperPortfolioPanel` 根容器改为由可见 `h3` 命名的 `section`，工具栏新增 `SIMULATION CONTROL / LOCAL ONLY` 和“纸面组合与历史验证”工作台标题。
- 新增组合总数、用户已确认、风险门内、当前展示四项 `data[value]` 摘要；明确“风险门内”只表示本地纸面状态，不代表推荐、批准或可执行。
- 原先固定 `slice(0, 4)` 的静默截断改为每次 4 条渐进展示；组合集合身份变化时重置到首批，按钮同时说明本次新增数量和仍未显示数量。
- 每张组合卡通过 `article[aria-labelledby]` 关联真实 `h4`；组合名经过长度边界处理，空白名称使用“未命名模拟组合”。
- 空状态升级为可读的模拟台账说明，明确可从用户支持候选建立精确关联组合或创建独立纸面方案，并持续显示“不连接账户 / 不产生订单 / 不替代用户决定”。
- 520px 以下摘要由四列变两列，空状态边界占满整行，渐进按钮拉伸为 44px 触控目标；forced-colors 覆盖新增容器。

### 安全回归与 production build

- 只通过 `frontend/scripts/run-tests-safe.ps1` 对应 npm 安全入口执行；未直接调用顶层 `node --test`。
- 最终结果：102 个测试文件、529 tests、529 pass、0 fail、0 cancelled、0 skipped、0 todo。
- production build：Vite 6.4.3，1743 modules，2.59s。
- `PaperPortfolioPanel` CSS 19.01 kB / gzip 4.20 kB；JS 48.37 kB / gzip 15.80 kB。
- `DecisionLineagePanel` JS 15.63 kB / gzip 5.83 kB；`RoomInspector` JS 90.80 kB / gzip 27.64 kB；`index` JS 373.07 kB / gzip 112.20 kB。

### 隔离浏览器证据

- 运行约束：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口 51250；切换到同一临时库中的“美国存储产业投资委员会”房间，只读打开真实纸面组合宿主。
- 1440x900：工作台真实挂载，`section[aria-labelledby]` 精确解析到可见 `h3`；面板 297 / 297，无自身溢出；页面 1440 / 1440，无横向溢出。
- 桌面真实空状态：四项摘要均为 0，当前展示为 0 / 0；297px 容器按查询规则呈两列；空状态 297 x 153.94px，三项操作边界完整。
- 320x568：面板 238.59px、239 / 239，无自身溢出；页面 320 / 320；摘要为两列约 105.3px；空状态边界跨整行；“新建方案”按钮 211.59 x 44px。
- 有效 200% CSS 视口 720x450：面板 297 / 297，页面 720 / 720，无横向溢出。
- 从真实空状态点击“新建方案”只打开本地草稿对话框；720x450 下对话框为 720 x 450px、内容 705 / 705；初始焦点位于“关闭模拟组合设置”，关闭后精确返回“新建方案”入口。
- production console 为 `[]`。
- 最终隔离状态：Provider calls 0；fixture market status / snapshot reads 各 1，但 external market reads 0；outbound attempts 0；post-start business writes 0；数据库自服务启动后未变化。51250、55096、8770、11111、18787 均无 listener，无 QA 进程。

### 证据边界

- 本轮真实浏览器数据为 0 个纸面组合，因此工作台标题、摘要、空状态、边界标签和新建对话框焦点取得真实 runtime 证据。
- 超过 4 条组合的渐进展开、集合变化后的窗口重置、命名组合卡和 populated 风险指标只有源码及契约测试证据，未声称真实浏览器覆盖。
- 本轮未执行真实屏幕阅读器、forced-colors 实机或 IME 输入验收；也未确认、复算或创建任何组合，没有业务写入。

## 2026-08-23 候选回放比较范围与数量门

### 实施结果

- `CandidateComparisonPanel` 把实施前比较约束固定为三个中性范围：输入仅限已验证历史回放，共同口径为同数据/同窗口/同摩擦，不产生排名/赢家/授权。
- 增加显式数量门：当前合同完整性异常时显示 `BLOCKED`；完整性通过但不足两条时显示 `n / 2`；满足两条后才进入可比较状态。原有候选资格筛选、完整性校验、用户确认和比较请求逻辑未放宽。
- 面板标题改为关联 section 的 `h4`，结果证明改为 `h5`；空态不再只有一句提示，而是列出“确认组合版本 / 完整性通过 / 同类候选合同”三项前置条件。
- 狭窄检查器内，范围、数量门和空态按单列收敛；高对比模式仍由既有 `forced-colors` 契约覆盖。

### 代码级证据

- `frontend/tests/candidateComparisonView.test.js` 新增静态契约，覆盖标题关联、`data` 数量值、中性范围、最小两条数量门、无授权措辞、结构化空态、移动布局及 `forced-colors` 选择器。
- 静态契约能证明源码结构与 CSS 接线存在，不能替代真实辅助技术、真实输入法或带刘海设备的操作验收。

### 安全回归与 production build

- 入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际由 README 指定的 `frontend/scripts/run-tests-safe.ps1` 分文件执行；未直接运行 `node --test`，未恢复顶层 Vite/JSDOM lazy gate。
- 结果：102 个测试文件，530 tests，530 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：1743 modules transformed，2.55 s。
- 主入口：`index-eGaYtn8j.js` 373.07 kB / gzip 112.21 kB；`index-CnQsKIag.css` 193.81 kB / gzip 36.02 kB。
- 所在懒加载工作台：`PaperPortfolioPanel-DNS7fXqS.js` 50.21 kB / gzip 16.22 kB；`PaperPortfolioPanel-qw_ubYA3.css` 22.01 kB / gzip 4.66 kB。

### 隔离浏览器验收

- 仅使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 与随机回环端口 `55441`；12 名成员夹具在服务启动前写入，启动后所有写接口锁定。
- `1440x900`：检查器内面板宽 297 px；section 与 `h4` 的 `aria-labelledby` 一致，范围三项、`role="note"` 数量门、`0 / 2` 和结构化空态均可见；document/body/面板均无横向溢出。
- `320x568`：面板宽约 238.59 px，范围与数量门收敛为单列，空态宽约 218.59 px；无横向溢出，焦点位于“关闭房间信息”。
- 有效 `200%` 视口 `720x450`：面板宽 297 px，无横向溢出，焦点位于“关闭房间信息”。这是将 `1440x900` 的 CSS 可用视口减半后的等效检查，不等同于真实浏览器缩放手势或操作系统缩放。
- 三档 `visualViewport` 分别为 `1440x900`、`320x568`、`720x450`，scale 均为 1，offset 均为 0；当前 Chromium 环境没有暴露安全区 inset。safe-area 的 `env(...)` 接线属于代码级证据，本批没有在真实刘海设备上复测。
- 桌面宽度下检查器为常驻侧栏，焦点保留在“房间信息”触发器；320 与 720 宽度下为覆盖层，焦点进入“关闭房间信息”。console 全程为空。
- runtime 遥测：`provider_calls=0`、`external_market_reads=0`、`outbound_connect_attempts=0`、`post_start_business_writes=0`、`blocked_post_start_write_attempts=0`；临时库在启动后未变化，Futu/OpenD/SEC/IR 均未连接。结束后随机端口及 `8770/11111/18787` 均无 listener，无 QA 残留进程。

### 尚未覆盖的边界

- 真实浏览器只覆盖了 `0 / 2` 的空候选历史状态；两条以上候选的选择器、确认流程、结果表和已就绪数量门仍只有代码与自动化契约证据。
- 本批没有使用真实中文 IME 合成事件，没有用 NVDA/JAWS/VoiceOver 操作，也没有实际启用 Windows 高对比模式。
- 本批没有重新展开 12 人 `@` 提及菜单；12 名成员仅作为隔离房间基线被核验，不能把成员数量等同于本批的提及交互证据。
- 以上通过项仅代表当前前端工程与隔离浏览器证据，不代表真实市场数据、Provider 可用性、投资结论、交易权限、正式迁移或公开发布授权。

## 2026-08-23 启动确认决策摘要与 modal 焦点守卫

### 实施结果

- `RoundLaunchDialog` 首屏增加三项中性摘要：技术许可进度、Provider 全局绝对硬上限、只读执行边界。摘要明确“不是预计用量”且“无下单、实盘或钱包权限”，不会以绿色 READY 或赢家措辞暗示授权。
- 标题由视觉 `strong` 改为真实 `h2`，关键分区使用 `h3`；dialog 的 `aria-labelledby` 继续绑定标题。
- footer 增加粘性许可状态和禁用原因，用户滚动到任意位置仍能看到为何不能提交；提交按钮仍完全由原有 `roundLaunchSubmitControl` 控制。
- 12 人成员路由默认收起，保留按需展开说明，降低首次打开的滚动负担；Provider 分摊、成员路由和 payload 均未删减。
- blocker 列表键不再混入普通数组索引，改为稳定内容身份加同内容重复序号。
- 浏览器验收发现：从桌面切到窄视口时，底层 `RoomInspector` 会把焦点抢回“关闭房间信息”。现增加 modal `focusin` containment：空闲态回到“关闭启动前确认”，忙碌态回到 dialog 容器。

### 安全回归与 production build

- 入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + README 指定的 `frontend/scripts/run-tests-safe.ps1`；未直接运行 `node --test`，未恢复顶层 Vite/JSDOM lazy gate。
- 最终结果：102 个测试文件，531 tests，531 pass，0 fail。
- production build：1743 modules transformed，2.29 s。
- `RoundLaunchDialog-CuYi_4A6.js` 20.96 kB / gzip 7.26 kB；`RoundLaunchDialog-DITsPlwE.css` 12.10 kB / gzip 3.04 kB。
- 主入口 `index-C7GfFDsf.js` 373.07 kB / gzip 112.21 kB；`index-CnQsKIag.css` 193.81 kB / gzip 36.02 kB。

### 隔离浏览器验收

- 仅使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 `49596`。一般 POST/PUT/PATCH/DELETE 继续锁定；仅白名单放行只读 `round-launch-plan` POST，并以数据库启动前后哈希证明零业务写入。
- 存储房间在无真实行情快照时正确 fail-closed，提示行情快照没有明确保持只读边界；没有绕过该门。随后使用通用/临时房间读取冻结计划，仅打开确认单，未点击“确认并启动”。
- 真实状态为 `4 / 5`：提交按钮禁用，粘性 footer 持续显示“尚不能提交启动确认”及许可原因；这证明 blocked 呈现，不证明可提交路径。
- `1440x900`：三项摘要同排，dialog、document、body 和摘要均无横向溢出；初始焦点为“关闭启动前确认”。
- `320x568`：摘要收敛为单列，footer 保持可见；焦点仍在 dialog 内的“关闭启动前确认”，不再逃到底层 RoomInspector。
- 有效 `200%` 视口 `720x450`：摘要三列、footer 可见，无横向溢出；焦点继续位于 dialog 内。
- 键盘首尾循环：从关闭按钮 `Shift+Tab` 到“取消”，再 `Tab` 回到关闭按钮，两次均保持在 modal 内。console 全程为空。
- 三档 `visualViewport` 分别为 `1440x900`、`320x568`、`720x450`，scale=1、offset=0；这是 CSS 可用视口等效检查，不是浏览器真实缩放手势。
- runtime 遥测：白名单只读计划 POST 共 3 次，`provider_calls=0`、`external_market_reads=0`、`outbound_connect_attempts=0`、`post_start_business_writes=0`、`blocked_post_start_write_attempts=0`；临时数据库自服务启动后未变化。结束后随机端口及 `8770/11111/18787` 均无 listener，无 QA 残留进程。

### 尚未覆盖的边界

- 本批真实浏览器只覆盖 blocked 的 `4 / 5` 状态；`5 / 5`、可用确认按钮、忙碌提交、服务端漂移拒绝和成功启动均未点击，仍只有代码与自动化契约证据。
- 未使用 NVDA/JAWS/VoiceOver，也未在真实刘海设备或 Windows 高对比模式下操作；`forced-colors` 与 safe-area 仍属于代码级接线证据。
- 本批没有真实 Provider、Futu/OpenD、SEC/IR 或外部市场连接，不代表这些系统可用；也不代表投资结论、交易权限、正式迁移或公开发布授权。

## 2026-08-23 讨论流程保存门与下一轮成员门分离

### 实施结果

- `WorkflowPolicyDialog` 将“草稿结构能否保存”和“当前成员能否满足下一轮流程”拆为两条独立证据：保存仍由 `workflowPolicySaveControl` 控制，成员门仍由 `workflowConfigurationGate` 控制。
- 新增 `NEXT ROUND MEMBER GATE` 面板，直接展示启用成员容量、阶段覆盖、立场和能力缺口的标题与明细；前三项直接可见，其余缺口按需展开。
- 文案明确“可保存不等于可启动”，即使保存一份成员暂时无法满足的合法策略，下一轮门禁也不会自动放宽；Provider、数据和用户确认继续独立核验。
- header 使用真实 `h2`，房间标题使用 `h3`；ledger 只保留阶段、专业覆盖、成员门禁和草稿变化四项短证据，不再把长保存原因截断在单行格子中。
- footer 持续显示草稿保存状态和具体原因；移动端保持在可视区域。新增 modal `focusin` containment，避免响应式 RoomInspector 把焦点抢到底层。
- 原有标准化提交、重复请求防护、模板恢复、阶段/覆盖编辑及不可变研究安全边界均未放宽。

### 安全回归与 production build

- 入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + README 指定的 `frontend/scripts/run-tests-safe.ps1`；未直接执行 `node --test`，未恢复顶层 Vite/JSDOM lazy gate。
- 结果：102 个测试文件，532 tests，532 pass，0 fail。
- production build：1743 modules transformed，2.26 s。
- `WorkflowPolicyDialog-BC5o0Iov.js` 21.81 kB / gzip 7.25 kB；`WorkflowPolicyDialog-BA9f4NbG.css` 15.93 kB / gzip 3.91 kB。
- 主入口 `index-kUQ372Na.js` 373.07 kB / gzip 112.20 kB；`index-CnQsKIag.css` 193.81 kB / gzip 36.02 kB。

### 隔离浏览器验收

- 仅使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 `62548`；打开真实 12 人临时房间的结构页与讨论流程设置，全程不保存。
- 基线状态：6 个阶段、11 项专业覆盖、12 位启用成员，成员门禁 0 项缺口；草稿无变化，因此保存按钮禁用。该状态只证明成员配置满足当前流程，不证明下一轮整体可启动。
- 本地草稿状态：将第一阶段最低覆盖临时改为合法值 50，成员门禁立即显示 1 项缺口，具体为“主持开场已配置 1 位、流程要求 50 位”；同时草稿结构通过本地检查，保存按钮可用。未点击保存，关闭标签后草稿丢弃。
- 上述状态真实证明“草稿可保存”与“下一轮成员门 blocked”能够同时成立，且门禁不会因保存许可而被隐藏。
- `1440x900`、`320x568`、有效 `200%` 的 `720x450` 均无 document/body/dialog/门禁面板横向溢出。320 宽度下缺口列表为单列；桌面为两列。
- 三档焦点都位于 modal 内；窄视口焦点为“关闭讨论流程设置”。从关闭按钮 `Shift+Tab` 到“保存流程”，再 `Tab` 回到关闭按钮，首尾循环未逃到底层页面。console 全程为空。
- 三档 `visualViewport` 分别为 `1440x900`、`320x568`、`720x450`，scale=1、offset=0；属于 CSS 可用视口等效检查，不是浏览器真实缩放手势。
- runtime 遥测：无 POST，`provider_calls=0`、`external_market_reads=0`、`outbound_connect_attempts=0`、`post_start_business_writes=0`、`blocked_post_start_write_attempts=0`；临时数据库自服务启动后未变化。结束后随机端口及 `8770/11111/18787` 均无 listener，无 QA 残留进程。

### 尚未覆盖的边界

- 本批没有真实保存策略，也没有随后发起下一轮；服务端持久化失败、并发漂移、保存成功后的重新 bootstrap 仍只有既有代码与自动化契约证据。
- 真实浏览器只产生 1 项缺口，未操作“其余缺口”折叠区，也未真实注入来源损坏策略；这些分支只有代码级证据。
- 未使用 NVDA/JAWS/VoiceOver、真实中文 IME、真实刘海设备或 Windows 高对比模式；`forced-colors`、safe-area 与屏幕阅读器播报仍不能宣称真实设备覆盖。
- 本批没有连接 Provider、Futu/OpenD、SEC/IR 或外部市场，不代表其可用，也不代表投资结论、交易权限、正式迁移或公开发布授权。

## 2026-08-23 成员身份下一轮贡献摘要与修复路径

### 实施结果

- `MemberDialog` 将流程阶段、研究立场与专业能力从长职责文本之后前移，组成独立的 `NEXT ROUND CONTRIBUTION` 分区，使讨论流程缺口可以直接在成员身份中修复。
- 顶部增加下一轮贡献摘要：流程阶段、研究立场、能力标签数量和后续参与状态随本地草稿实时更新。
- 文案明确“保存身份不等于下一轮已获启动授权”；保存只生成新身份版本，实际 Provider 路由与流程门仍在下一轮开始时重新核验。
- header 改为真实 `h2`；footer 增加粘性保存状态与阻断原因，区分未分配执行器、宿主未提供保存处理器、请求进行中和可由用户保存。
- 新增 modal `focusin` containment，响应式 RoomInspector 不再能把焦点抢到底层。原有单一 `onSubmit(form)` 路径、归档确认、请求 epoch、Provider 选择和历史版本语义均未改变。
- 已核验 `RoomSettingsDialog` 只管理房间元数据与能力包，不承担成员阶段/立场/能力编辑；本批没有错误地把成员修复入口塞入房间设置。

### 安全回归与 production build

- 入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + README 指定的 `frontend/scripts/run-tests-safe.ps1`；未直接执行 `node --test`，未恢复顶层 Vite/JSDOM lazy gate。
- 结果：102 个测试文件，533 tests，533 pass，0 fail。
- production build：1743 modules transformed，2.25 s。
- `Dialogs-Dxfc5d-h.js` 54.35 kB / gzip 17.87 kB；`Dialogs--aAzdFyP.css` 17.09 kB / gzip 3.17 kB。
- 主入口 `index-DVB7-iOz.js` 373.07 kB / gzip 112.21 kB；`index-CnQsKIag.css` 193.81 kB / gzip 36.02 kB。

### 隔离浏览器验收

- 仅使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite 和随机回环端口 `62745`；打开真实 12 人临时房间中的“投资委员会主持人”身份，不保存、不归档。
- 基线摘要：流程阶段“主持开场”、研究立场 `facilitator`、能力标签 0 项、后续参与“参与”；流程贡献分区位于“核心职责”等长文本之前。
- 本地修复演练：将流程阶段改为“风险复核”、研究立场改为 `risk`，并勾选“风险复核”能力；顶部摘要实时变为“风险复核 / risk / 1 项能力 / 参与”。保存按钮保持可用，但未点击，关闭标签后草稿丢弃。
- `1440x900`：摘要四列；`320x568`：摘要收敛为两列两行；有效 `200%` 的 `720x450`：摘要四列。三档 document/body/dialog/摘要/流程贡献分区均无横向溢出，footer 始终可见。
- 三档焦点均位于 modal 内；窄视口焦点回到“关闭成员设置”。从关闭按钮 `Shift+Tab` 到“保存身份”，再 `Tab` 返回关闭按钮，首尾循环未逃到底层页面。console 全程为空。
- 三档 `visualViewport` 分别为 `1440x900`、`320x568`、`720x450`，scale=1、offset=0；属于 CSS 可用视口等效检查，不是浏览器真实缩放手势。
- runtime 遥测：无 POST，`provider_calls=0`、`external_market_reads=0`、`outbound_connect_attempts=0`、`post_start_business_writes=0`、`blocked_post_start_write_attempts=0`；临时数据库自服务启动后未变化。结束后随机端口及 `8770/11111/18787` 均无 listener，无 QA 残留进程。

### 尚未覆盖的边界

- 本批没有真实保存成员，因此服务端创建新身份版本、并发版本冲突、保存失败和重新 bootstrap 后的持久状态仍只有既有代码与自动化契约证据。
- 未验收“添加 AI 成员”、未分配/策略禁用 Provider、归档确认和显式主持人不可暂停等真实浏览器分支。
- 未使用 NVDA/JAWS/VoiceOver、真实中文 IME、真实刘海设备或 Windows 高对比模式；屏幕阅读器播报、`forced-colors` 与 safe-area 仍不能宣称真实设备覆盖。
- 本批没有连接 Provider、Futu/OpenD、SEC/IR 或外部市场，不代表其可用，也不代表投资结论、交易权限、正式迁移或公开发布授权。

## 2026-08-23 房间设置版本影响与下一轮边界

### 本批实施

- `RoomSettingsDialog` 恢复明确的语义标题层级：对话框名称由 `h2` 提供；保存前状态继续作为短状态账本，不再用第三格混入操作说明。
- 新增 `NEXT ROUND BOUNDARY` 影响面板，分别展示能力包选择、股票池合同和主持选择；明确保存只生成下一轮设置版本，不回写历史消息、已冻结流程、证据或执行路由。
- footer 将“是否可保存”与“下一轮是否可运行”分离：保存状态通过 `role="status"` / `aria-live="polite"` 暴露，按钮区保持独立，busy 状态使用可见加载图标。
- 增加 dialog 外部 `focusin` containment，并保留共享 modal Tab 环；窄屏将影响摘要改为单列，footer 改为双按钮栅格并继续使用 `visualViewport` / safe-area 约束。
- `roomSettingsUi.test.js` 增补版本影响、保存许可、语义标题和焦点 containment 的静态合同；这些断言证明源码结构存在，不等同于辅助技术实机验收。

### 受控回归与 production build

- 命令入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`；实际经过 `frontend/scripts/run-tests-safe.ps1`，未直接执行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 完整回归：`102` 个测试文件，`534` tests，`534` pass，`0` fail。
- production build：Vite `6.4.3`，`1743` modules transformed，`2.06s`。
- 本对话框产物：`RoomSettingsDialog` JS `49.01 kB`（gzip `15.26 kB`），CSS `35.10 kB`（gzip `7.03 kB`）。主入口 JS `373.07 kB`（gzip `112.20 kB`），主入口 CSS `193.81 kB`（gzip `36.02 kB`）。这些是本次真实构建尺寸，不代表运行时性能预算已经长期达标。

### 隔离浏览器证据

- 运行边界：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口 `64773`；fixture 在服务启动前完成，验收过程未提交保存请求。
- `1440x900`：根节点与 `body` 均为 `clientWidth=scrollWidth=1440`；dialog `clientWidth=scrollWidth=803`。影响面板完整可见，footer 计算样式为 `position: sticky`，保存许可与按钮保持可见。
- `320x568`：根节点与 `body` 均为 `clientWidth=scrollWidth=320`；dialog `clientWidth=scrollWidth=263`。影响摘要按单列进入内部纵向滚动，footer 仍为 sticky，计算底部内边距 `16px`，页面无横向溢出。
- 有效 `200%` CSS 视口 `720x450`：根节点与 `body` 均为 `clientWidth=scrollWidth=720`；dialog `clientWidth=scrollWidth=663`，footer 仍为 sticky。`visualViewport` 实测为 `720x450`、offset `0/0`、scale `1`。
- 键盘焦点链实测为标题输入框 → 关闭 → 保存 → 关闭 → 标题输入框，四次检查均保持在 dialog 内；视口切换后活动元素仍在 dialog 内。
- 浏览器 console 为 `[]`。运行器只记录 `/api/bootstrap` 和房间设置版本 GET；`blocked_post_start_write_attempts=0`、`post_start_business_writes=0`、Provider 调用 `0`、外部连接尝试 `0`、正式库未使用、临时库自服务启动后未变化。停止后 `64773`、`8770`、`11111`、`18787` 均无 listener。

### 证据边界

- 已覆盖的是源码合同、受控自动化回归、production build、Chromium 语义树/键盘链和三档 CSS 视口的布局测量。
- 未覆盖真实中文/日文 IME composition 输入；本批没有把普通键盘输入观察外推为 IME 兼容结论。
- 未使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack；`aria-labelledby`、`role`、`aria-live` 和焦点顺序只是代码级与浏览器语义树证据，不证明真实屏幕阅读器播报质量。
- safe-area 仅验证了 CSS 约束和零 inset 环境下的计算值，没有覆盖带刘海、圆角或系统手势区的真机非零 inset。
- `720x450` 是把 `1440x900` 折半后的有效 CSS 视口，不是 Windows 显示缩放或浏览器原生 zoom=200% 的实机结论；本批也未实测 forced-colors、高对比度或 reduced-motion。
- 为保持零写边界，本批未点击保存，因此没有验证服务端新版本持久化、busy/error 返回、冲突重试或保存后的重新读取链；这些仍需单独的临时库写入验收。

## 2026-08-23 生命周期目录渐进披露与深层移动端收敛

### 本批实施

- `CapabilityPackLifecyclePanel` 将 7 个精确版本从“身份、head、全部动作同时展开”改为渐进披露：状态、描述、精确版本和动作数量始终可见；来源封印、生命周期 head 与动作按钮只在显式展开后出现。
- 每个目录项使用 `kind + id + version + target SHA256` 组成的 `pluginLifecycleTargetKey` 作为 React key，不再只使用 `pack id + version`。受限版本和当前审阅目标会自动展开，普通可用版本默认收起。
- 新增 `精确版本目录` 语义 section、动作数量摘要、`aria-controls` / `aria-expanded`、独立详情 region；展开按钮保持 44px 触控高度，展开后 Tab 的下一站是该精确版本的首个动作。
- 浏览器深层滚动暴露并修复了已有的移动端 min-content 链：`room-settings-editable`、能力包 picker 改用 `minmax(0, 1fr)`；直接子项允许收缩；冻结合同与生命周期的组件 header 提高自有 grid specificity，并让窄屏 container query 使用同等 specificity；冻结合同 trust 文案允许窄屏换行。
- 修复没有使用 `overflow-x: hidden` 掩盖内容，也没有改变生命周期 preview/transition API、提交许可、历史只读或用户最终决定边界。

### 受控回归与 production build

- 最终入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际经过 `frontend/scripts/run-tests-safe.ps1`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 最终完整回归：`102` 个测试文件，`536` tests，`536` pass，`0` fail。
- production build：Vite `6.4.3`，`1743` modules transformed，`2.09s`。
- 最终产物：`RoomSettingsDialog` JS `50.67 kB`（gzip `15.66 kB`），CSS `38.30 kB`（gzip `7.48 kB`）；主入口 JS `373.07 kB`（gzip `112.19 kB`），主入口 CSS `193.81 kB`（gzip `36.02 kB`）。主入口未因本批生命周期目录改造产生可见体积增长。

### 隔离浏览器证据

- 运行边界：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口 `63117`；12 人 fixture，未点击任何生命周期动作或保存按钮。
- 默认目录：`7` 个卡片全部收起，`7` 个详情区隐藏，生命周期动作按钮可见数为 `0`。鼠标展开足球研究精确版本后只出现该项 `4` 个动作；Tab 从展开按钮进入首个动作“停用”，Shift+Tab 返回展开按钮，焦点始终在 dialog 内。
- `1440x900`：根节点和 `body` 均为 `1440/1440`，dialog 为 `803/803`；生命周期 masthead 与目录栏计算为 grid，footer 保持 sticky。
- `320x568`：在生命周期目录实际展开/收起并让下半部内容完成布局后，根节点和 `body` 均为 `320/320`，dialog 为 `263/263`，编辑 fieldset 为 `263/263`，内核 picker 为 `193/193`，内核卡片为 `171/171`，冻结合同 header 为 `173/173`，生命周期 masthead 与目录栏均为 `169/169`；无内部或页面横向滚动。
- 有效 `200%` CSS 视口 `720x450`：根节点和 `body` 均为 `720/720`，dialog 为 `663/663`；`visualViewport=720x450`、offset `0/0`、scale `1`，footer 保持 sticky，计算底部内边距 `16px`。
- 浏览器 console 为 `[]`。运行器最终只记录 bootstrap 与房间设置版本 GET；`blocked_post_start_write_attempts=0`、`post_start_business_writes=0`、Provider 调用 `0`、外连尝试 `0`、正式库未使用、临时库自服务启动后未变化。停止后 `63117`、`8770`、`11111`、`18787` 均无 listener。

### 证据边界

- 浏览器控制工具的 `press("Enter")` 与 `press("Space")` 没有为该按钮合成 click，因此本批没有把键盘激活宣称为真实浏览器通过；已证明的是原生 `button` 源码合同、鼠标展开/收起、可访问名称切换、`aria-expanded` 状态与展开后的 Tab 顺序。真实键盘 Enter/Space 仍需人工或另一套输入驱动验证。
- 未点击“停用”等动作，因而未发起生命周期影响预览，也未覆盖 preview busy/error、许可填写、transition 提交或持久化；这些仍是临时库写入验收的独立范围。
- 未使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack；语义 section、region、`aria-controls` 和 `aria-expanded` 只是代码级与 Chromium 可访问树证据。
- 未覆盖真实 IME、硬件非零 safe-area、Windows 原生 200% 显示缩放、浏览器 zoom=200%、forced-colors 或 reduced-motion 实机行为。

## 2026-08-23 冻结合同审计摘要与精确绑定渐进披露

### 本批实施

- `CapabilityRegistrySnapshot` 保留冻结合同 masthead、版本/封印、四项统计、选择/生命周期告警和无执行安全说明常驻；能力包、领域适配器、精确端口与宿主贡献移入显式“查看冻结绑定”只读区域。
- 审计摘要直接显示冻结绑定总数，展开按钮使用 `aria-controls`、`aria-describedby` 与 `aria-expanded`；房间冻结 hash 变化时本地展开状态自动复位，避免把上一房间的阅读状态带入新合同。
- React key 覆盖完整精确身份：能力包包含 manifest hash，适配器/端口/宿主贡献包含 contract hash，端口还绑定所属适配器，宿主贡献还绑定 slot；完整性错误行也使用错误文本与序号的显式复合 key。
- 新增后加载的 `capability-registry-refinement.css`，为审计摘要、展开区、44px 控制、forced-colors 与 reduced-motion 提供组件自有样式；`320px` 下 masthead mark、标题、trust 与审计按钮均使用可收缩单列，不再形成逐词竖排或横向撑宽。
- 解析、冻结信任状态、生命周期可用性、保存行为和无执行边界均未改变。

### 受控回归与 production build

- 最终入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际经过 `frontend/scripts/run-tests-safe.ps1`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 完整回归：`102` 个测试文件，`537` tests，`537` pass，`0` fail。
- production build：Vite `6.4.3`，`1744` modules transformed，`2.03s`。
- `RoomSettingsDialog` JS `51.82 kB`（gzip `15.96 kB`），CSS `40.68 kB`（gzip `7.77 kB`）；主入口 JS `373.07 kB`（gzip `112.20 kB`），主入口 CSS `193.81 kB`（gzip `36.02 kB`）。新增专属 CSS 作为 lazy RoomSettings 依赖，没有进入主入口增量。

### 隔离浏览器证据

- 运行边界：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口 `65383`；12 人 fixture，未点击保存、生命周期动作或任何业务写入口。
- `1440x900` 默认状态：冻结合同摘要高度约 `413.52px`，显示“2 项精确冻结绑定”；详情 `hidden=true`，按钮 `aria-expanded=false`，dialog 为 `803/803`。展开后按钮名称切换为“收起冻结绑定”，详情可见，包含 `1` 个能力包行与 `2` 个绑定分组，焦点保持在 dialog 内，dialog 仍为 `803/803`。
- `320x568`：根节点与 `body` 均为 `320/320`，dialog `263/263`，snapshot `193/193`，header `173/173`，title `145/145`，trust `143/143`，audit `159/159`，按钮 `129/129`；详情收起后安全说明保持可见，无页面或 dialog 横向溢出。
- 有效 `200%` CSS 视口 `720x450`：根节点与 `body` 均为 `720/720`，dialog `663/663`，snapshot `555/555`；`visualViewport=720x450`、offset `0/0`、scale `1`，footer 仍为 sticky，底部计算内边距 `16px`。
- 浏览器 console 为 `[]`。运行器只记录 bootstrap 与房间设置版本 GET；`blocked_post_start_write_attempts=0`、`post_start_business_writes=0`、Provider 调用 `0`、外连尝试 `0`、正式库未使用、临时库自启动后未变化。停止后 `65383`、`8770`、`11111`、`18787` 均无 listener。

### 证据边界

- 桌面展开区与移动审计摘要已截图检查；浏览器控制层不允许直接设置 dialog `scrollTop`，元素裁剪接口又不能截取视口外节点，因此移动 masthead 没有独立截图。其 grid、title、trust 的等宽几何与无溢出已实测，但不把这替代为完整视觉截图结论。
- 展开/收起使用鼠标 click 验证；本批未重复声称 Enter/Space 键盘激活通过。原生 button 和 ARIA 状态属于代码级证据，真实键盘激活仍需人工或另一套输入驱动。
- 未使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack；详情 region、描述关联和状态切换不等同于真实屏幕阅读器播报质量。
- 未覆盖真实 IME、硬件非零 safe-area、Windows 原生 200% 显示缩放、浏览器 zoom=200%、forced-colors 或 reduced-motion 实机行为。

## 2026-08-23 新建房间实施预览与创建前复核

### 本批实施

- `CreateRoomDialog` header 改为 `h2` 语义层级，并增加 `NEW ROOM / REVIEW FIRST` 与“创建不替代正式轮启动确认”的边界说明；移动端 copy 使用明确单列和深色 masthead 上的浅色层级。
- 模板描述、阵容、领域能力和默认工作流从表单两端聚合为 `实施结构`：模板成员数、流程阶段数和当前可选领域包常驻，逐成员职责/边界、能力标签与流程细节由本地只读按钮展开。
- footer 前新增 `CREATE PERMIT / LOCAL DRAFT` 创建前复核卡，实时区分 draft/ready/blocked/submit error，并展示 `3` 个必填字段完成数、模板、模板成员和可选领域包；它不替代原生 required 校验或既有 lifecycle/股票池 fail-closed gate。
- 模板、内核协议、可选能力包和能力标签 React key 不再依赖数组 index；能力包 key 包含 id、精确版本与 manifest SHA256。
- 新增后加载的 `create-room-review-summary.css`，为实施预览、复核状态、44px 展开按钮、safe-area、forced-colors 与 reduced-motion 提供组件自有样式；左右 safe-area 分别引用对应 inset。
- `stockRoomFormSubmission(form)` 与 `onSubmit(payload)` 保持原调用链，未改变创建 payload 或服务端确认语义。

### 受控回归与 production build

- 最终入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际经过 `frontend/scripts/run-tests-safe.ps1`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 完整回归：`102` 个测试文件，`538` tests，`538` pass，`0` fail。
- production build：Vite `6.4.3`，`1745` modules transformed，`2.09s`。
- `Dialogs` JS `57.32 kB`（gzip `18.69 kB`），CSS `22.48 kB`（gzip `4.12 kB`）；主入口 JS `373.07 kB`（gzip `112.21 kB`），主入口 CSS `193.81 kB`（gzip `36.02 kB`）。新增 UI 仍跟随 lazy Dialogs chunk，没有进入主入口模块。

### 隔离浏览器证据

- 运行边界：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口 `51093`；12 人 fixture，只做本地表单填写与实施结构展开/收起，未点击“创建房间”。
- 默认状态：模板为“开放共创”，实施摘要为 `4 位成员 / 3 段流程 / 0 项领域包`，详情 `hidden=true` / `display:none`；创建复核为 `draft`、`1 / 3 必填`。房间名称初始获得焦点。
- 本地填写名称和长期目标后，复核实时变为 `ready`、`3 / 3 必填`。展开实施结构后按钮名称变为“收起实施结构”，详情包含“模板阵容，共 4 位成员”、领域能力和默认流程，焦点保持在 dialog 内；未触发网络提交。
- `1440x900`：根节点和 `body` 均为 `1440/1440`，dialog `843/843`，header copy `757/757`，实施摘要 `793/793`，复核卡 `790/790`，footer `843/843` 且 sticky。
- `320x568`：根节点和 `body` 均为 `320/320`，dialog `305/305`；header copy 为单列 `213/213`，标题宽 `213px`，header 高约 `102.69px`，浅色标题和说明可读；实施摘要 `267/267`，展开详情 `267/267`，复核卡 `264/264`，footer `305/305` 且底部计算内边距 `14px`。
- 有效 `200%` CSS 视口 `720x450`：根节点与 `body` 均为 `720/720`，dialog `705/705`，实施摘要 `667/667`，复核卡 `664/664`，footer `705/705`；`visualViewport=720x450`、offset `0/0`、scale `1`。
- 浏览器 console 为 `[]`。运行器只记录 `2` 次 `/api/bootstrap`；`blocked_post_start_write_attempts=0`、`post_start_business_writes=0`、Provider 调用 `0`、外连尝试 `0`、正式库未使用、临时库自启动后未变化。停止后 `51093`、`8770`、`11111`、`18787` 均无 listener。

### 证据边界

- 本批没有提交创建请求，因此没有覆盖服务端必填错误、创建幂等、busy/error 响应、成功后房间切换或数据库持久化；submit error 对复核卡的优先级是代码级与测试合同证据。
- 未切换全部 7 个模板，也未选择股票能力包或输入股票池；模板重置、生命周期阻断和 stock_room_scope_v1 的真实浏览器分支仍需单独临时库验收。
- 没有点击提交按钮验证浏览器原生 required 提示；`1/3` 到 `3/3` 是本地复核状态，不等同于服务端允许创建。
- 未使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack；`h2/h3`、region、`aria-controls` 和 live footer 只是源码与 Chromium 语义树证据。
- 未覆盖真实 IME、硬件非零 safe-area、Windows 原生 200% 显示缩放、浏览器 zoom=200%、forced-colors 或 reduced-motion 实机行为。

## 2026-08-23 新建房间可选能力包目录渐进披露

### 本批实现

- `CreateRoomDialog` 新增本地 `capabilityPickerExpanded` 状态；每次重新打开或切换模板时收起目录，不写入房间表单与提交载荷。
- 折叠态先保留全部已选能力包，再用未选项补足到 2 项；因此已选项不会因收起而卸载。展开态才挂载完整目录，能力包生命周期、`canToggle`、股票池与创建阻断 gate 未改。
- 目录复核条明确显示“已选 / 目录项”、未挂载数量和“展开不会自动选择”；按钮使用真实 `button`、`aria-controls`、`aria-expanded`，并保留原有版本化精确 React key。
- 新样式把桌面摘要组织为“实施说明 + 目录动作”，在组件容器小于 560px 时切为单列；按钮最小高度 44px，并覆盖 reduced-motion 与 forced-colors。
- 新增静态契约测试 `createRoomCapabilityPicker.test.js`，覆盖 2 项预览、已选集合优先、显式展开、重置、ARIA 与窄容器样式。

### 安全回归与 production build

- 入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + README 指定的 `npm.cmd --prefix frontend test`，实际进入 `frontend/scripts/run-tests-safe.ps1`；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 回归：103 个测试文件，539 tests，539 pass，0 fail。
- production build：Vite 6.4.3，1746 modules，2.10s。
- 关键产物：`Dialogs` JS 58.37 kB / gzip 18.97 kB；`Dialogs` CSS 24.43 kB / gzip 4.46 kB；主 JS 373.07 kB / gzip 112.20 kB；主 CSS 193.81 kB / gzip 36.02 kB。

### 隔离浏览器实测

- 环境：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式系统临时 SQLite、随机回环 `127.0.0.1:59050`；测试房间 12 位启用成员。
- `1440x900`：初始挂载 2/6，展开后 6/6；选中第 3 项“美国存储产业只读研究”后收起，仍挂载 2 项且该已选项保留，摘要变为 1/6，未挂载提示为 4 项。页面 `scrollWidth/clientWidth=1440/1440`；目录、摘要与 sticky footer 均无内部横向溢出。
- `320x568`：页面 `320/320`；dialog `305/305`、能力包 fieldset `283/283`、摘要 `258/258`、首卡 `261/261`，摘要切为单列，按钮实际宽 230px；footer 保持 sticky 且底部 padding 14px。`visualViewport=320x568`、offset 为 0、scale 1。
- 有效 200% CSS 视口 `720x450`：页面 `720/720`；dialog `705/705`、fieldset `683/683`、摘要 `658/658`、首卡 `661/661`，footer 保持 sticky；`visualViewport=720x450`。
- modal：真实 `role=dialog` 存在；从 footer 末端继续 Tab 后焦点仍被包含在 dialog 内。关闭后因 `720px` 断点下原“新建房间”触发点不可见，焦点安全回退到“打开房间列表”。
- Composer：真实入口为“提及成员”按钮；菜单包含 12 个 `menuitem`，`ArrowDown` 把焦点移入首项。`320x568` 下菜单宽 248px、内容 `scrollWidth/clientWidth=236/236`，页面仍为 `320/320`。
- console：空数组，无 error/warning。
- runner 账本：仅 1 次 `GET /api/bootstrap`；blocked post-start writes 0、business writes 0、Provider calls 0、市场/SEC/IR 外部读取 0、outbound connect 0；正式数据库未使用，临时数据库启动后未变化。停止后 59050、8770、11111、18787 均无 listener。

### 证据边界

- 上述 ARIA、focus containment、forced-colors、safe-area/visualViewport 适配属于代码级契约和 Chromium 自动化证据；本机模拟环境的 safe-area inset 为 0，不能替代带刘海/圆角设备上的非零 inset 实机验证。
- 本批没有执行真实中文/日文 IME composition sequence、移动系统软键盘开合或 `visualViewport` 动态 offset 变化；因此不能宣称真实 IME 与软键盘行为已覆盖。
- 本批没有使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 实际朗读；`role=menu`、12 个 `menuitem`、`aria-controls`、`aria-expanded` 和焦点路径已通过 DOM/键盘检查，但不能宣称屏幕阅读器播报质量已验收。
- 未点击“创建房间”，未覆盖真实提交 busy/error、后端持久化或创建后导航；本批证据不改变正式迁移、Provider、Futu/OpenD、SEC/IR、交易执行或公开发布授权边界。
## 2026-08-23 跨房间行动总览精确键与渐进挂载

### 本批实现

- `ActionOverviewDrawer` 的行动卡 React key 改为归一化层已去重并保证全局唯一的 `item.overviewKey`；失败房间警告改为只使用归一化层已保证唯一的 `roomId`，不再把数组下标混入身份。
- 本地筛选结果保持后端原顺序，但初始只挂载 6 项；“再显示”每次最多增加 6 项，并以 `Math.min` 封顶。搜索、状态切换、抽屉重新打开都会把挂载窗口重置为 6 项。
- 新增“已挂载 / 匹配 / 总计”复核语义、原生 `progress` 与 `aria-controls`；只有仍有未挂载匹配项时才显示继续按钮。该路径不产生排名、不修改状态、不请求更多后端数据。
- 进度区使用独立纸面式层级、44px 操作高度和窄屏单列布局；forced-colors 延续统一边框契约。
- `actionOverview.test.js` 新增静态契约，覆盖 6 项窗口、精确 key、移除下标、展开封顶、窗口重置、ARIA 与移动布局。

### 安全回归与 production build

- 入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际进入 `frontend/scripts/run-tests-safe.ps1`；未直接执行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 回归：103 个测试文件，540 tests，540 pass，0 fail。
- production build：Vite 6.4.3，1746 modules，2.06s。
- 行动总览 lazy chunk：JS 16.49 kB / gzip 5.92 kB；CSS 13.23 kB / gzip 3.13 kB。
- 主 JS 373.07 kB / gzip 112.20 kB；主 CSS 193.81 kB / gzip 36.02 kB。

### 隔离浏览器实测

- 真实临时库：系统临时 runtime、显式临时 SQLite、随机回环 `127.0.0.1:55932`。真实行动总览是合法空集，抽屉显示 `0 已挂载 / 0 匹配 / 0 总计` 与明确空态，不显示伪造进度按钮。
- 合成 UI fixture：在系统临时 runner 副本中只覆盖 `/api/action-desk/overview` 的只读 GET，返回 8 条通过同一前端归一化契约的只读行动；项目源码没有测试后门，正式数据库和正式服务均未使用。
- `1440x900`：默认只挂载前 6/8 项，按钮显示“再显示 2 项”；点击后为 8/8 且进度区移除。筛选“受阻”仅显示原顺序中的第 3、7 项；切回全部重置为 6/8。搜索“第 8 项”只显示 1 项，真实 `Ctrl+A`/Backspace 清空后恢复 6/8。
- 桌面几何：页面 `1440/1440`；drawer `719/719`、body `704/704`、filters `668/668`、首卡 `655/655`、进度区 `655/655`，无横向溢出。
- `320x568`：页面 `320/320`；drawer `319/319`、body `304/304`、filters `288/288`、首卡 `279/279`、来源区 `253/253`、进度区 `279/279`。进度动作切为单列，按钮内容宽 249px；`visualViewport=320x568`、offset 0、scale 1。
- 有效 200% CSS 视口 `720x450`：页面 `720/720`；drawer `719/719`、body `704/704`、filters `688/688`、首卡与进度区均 `679/679`；`visualViewport=720x450`。
- modal focus：打开后初始焦点为“关闭行动总览”；从挂载窗口末端“再显示 2 项”继续 Tab，焦点回到关闭按钮且始终包含在真实 dialog 内。
- console：空数组，无 error/warning。
- 合成 runner 账本：仅 `GET /api/bootstrap` 与 `GET /api/action-desk/overview` 各 1 次；blocked post-start writes 0、business writes 0、Provider calls 0、市场/SEC/IR 外部读取 0、outbound connect 0，临时数据库启动后未变化。停止后 54970、55932、8770、11111、18787 均无 listener。

### 证据边界

- 静态契约证明精确 key、窗口算法与样式存在；8 项 synthetic fixture 浏览器实测证明前端在契约合法响应下实际执行 6→8、筛选与重置，但不证明当前真实临时库或正式库已有已采纳行动。
- 本批没有点击“打开对应行动台”，因此没有把合成 room id 交给真实导航处理器，也未覆盖成功导航后的 lazy inspector 对齐。
- DOM/键盘焦点和 visualViewport 已自动化检查，但没有使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 实际朗读；不能把 ARIA 存在等同于屏幕阅读器播报质量验收。
- 本批不改变后端顺序、行动状态、用户决定、讨论启动、正式迁移、Provider、Futu/OpenD、SEC/IR、交易执行或公开发布授权边界。
## 2026-08-23 产物版本差异精确身份与渐进挂载

### 本批实现

- `ArtifactVersionHistory` 新增 `artifactVersionListKey`，标量变化使用 `change.key`，结构段使用 `section.key + 基准版本 + 对比版本`，条目使用 `section.key + item.id + 两端版本`，字段变化使用 `section.key + item.id + field.field`；四层 React key 均不再附加数组下标。
- 每个结构段把新增、删除、修改条目合并为保持原 diff 顺序的本地行窗口，默认只挂载 4 条；有剩余项时显示显式展开按钮，展开状态与具体比较版本绑定。
- 结构段切换版本后会重建组件，因此旧比较的展开状态不会泄漏到新比较；diff 算法、完整性筛选、证据计数和版本 GET 契约未改。
- 新增独立 refinement CSS：变化行、修改详情、44px 展开动作、窄容器单列和 forced-colors。浏览器首次验收发现旧 ArtifactDialog 规则覆盖新标题 `display:grid`；已用局部子选择器修正并重新执行完整回归、build 和浏览器复测。
- 新增 `artifactVersionHistoryUi.test.js`，覆盖四层精确 identity、无 index、4 条窗口、ARIA 与窄容器样式。

### 修正后安全回归与 production build

- 入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际进入 `frontend/scripts/run-tests-safe.ps1`；未直接执行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 回归：104 个测试文件，541 tests，541 pass，0 fail。
- production build：Vite 6.4.3，1747 modules，2.05s。
- ArtifactDialog lazy chunk：JS 150.12 kB / gzip 45.55 kB；CSS 117.85 kB / gzip 18.85 kB。
- 主 JS 373.07 kB / gzip 112.20 kB；主 CSS 193.81 kB / gzip 36.02 kB。

### 隔离浏览器实测

- 标准临时库首先验证了合法空产物台账；随后使用系统临时 runner 副本，在服务启动前通过正式 `StudioStore.create_artifact` 和 `update_artifact` 路径写入一个临时产物 v1/v2。两版含 6 条相同稳定 ID 的需求，v2 逐条修改状态与验收标准；启动后的数据库哈希以 seed 完成状态为基线。
- 最终复测随机回环：`127.0.0.1:53579`，临时 artifact `artifact_fdbbdcd6211c`，2 个真实临时快照、6 条结构变化。项目源码没有 fixture 后门，正式数据库未使用。
- 真实 UI/API：ArtifactPanel 显示 1/1 产物，打开 ArtifactDialog 后版本列表为 v2、v1，默认选择基准 v1、对比 v2；对比摘要为字段 3、条目 6、证据关系 0。
- 默认只挂载需求修改 4/6，按钮为“再显示 2 条变化”、`aria-expanded=false`；展开后 6/6，按钮变为“收起至前 4 条”；收起恢复 4/6，焦点始终包含在 modal dialog 内。
- 版本状态绑定：展开后把对比版本切到 v1，显示结构一致且无变化段；切回 v2 后恢复默认 4/6 和 `aria-expanded=false`，旧展开状态未泄漏。
- `1440x900`：修正后标题实际 `display:grid`，列宽约 684.63px / 118.38px；页面 `1440/1440`，dialog `903/903`、history body `869/869`、section/rows/首详情均无横向溢出。
- `320x568`：页面 `320/320`；dialog `263/263`、history body `241/241`、section/rows/标题均 `189/189`，标题实际单列，展开按钮与内容区同宽。`visualViewport=320x568`、offset 0、scale 1。
- 有效 200% CSS 视口 `720x450`：页面 `720/720`；dialog `663/663`、history body `641/641`、section/rows/标题均 `589/589`，标题恢复双列；`visualViewport=720x450`。
- console：空数组，无 React duplicate-key warning、error 或 warning。
- 最终 runner 账本：仅 bootstrap、行动台只读 GET、证据图 GET、版本列表 GET 与 v1/v2 精确版本 GET；blocked post-start writes 0、business writes 0、Provider calls 0、市场/SEC/IR 外部读取 0、outbound connect 0，临时数据库启动后未变化。停止后 49780、51954、53579、8770、11111、18787 均无 listener。

### 证据边界

- 临时产物内容是浏览器验收 fixture，但版本快照、列表和精确详情均由真实 Store 与 HTTP GET 路径生成；这证明当前组件处理真实契约的行为，不证明正式数据库已有该产物或正式历史数据已经迁移。
- 本批没有点击保存、确认、导出、证据审核、候选治理或用户最终决定；启动后没有任何写请求。
- DOM、真实键盘焦点和 visualViewport 已自动化检查，但没有使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 实际朗读，不能把 ARIA 和角色存在等同于屏幕阅读器播报质量验收。
- 本批不改变正式迁移、Provider、Futu/OpenD、SEC/IR、交易执行或公开发布授权边界。
## 2026-08-23 候选治理稳定投影键、分页进度与末页焦点

### 实施

- `candidateGovernance.js` 为候选和精确版本风控意见生成完整身份投影键。正常唯一记录在重排后保持稳定；仅对完整身份完全相同的异常诊断重复行追加同桶 occurrence，避免把数组位置伪装成业务身份。
- `ArtifactCandidateGovernance.jsx` 不再使用数组 index 作为候选、意见或问题列表键。问题先去重并直接以问题文本为键；候选和意见使用模型层 `projectionKey`。
- 冻结候选默认挂载 40 行、精确版本意见默认挂载 60 行；原生 `progress` 显示已挂载数量，并由分页按钮的 `aria-controls` 精确绑定对应列表。
- 浏览器验收发现最后一页按钮卸载后焦点会退回页面主体。修正后，按钮在 click capture 阶段保存同层进度节点，React 完成挂载后将焦点转移到带 `tabIndex={-1}` 的可朗读 `progress`；两个进度节点均有可见焦点环。
- 治理台视觉层继续使用证据台账语言：桌面双列、窄容器单列、原生进度条、44px 移动触控目标、forced-colors 边界，不改变治理状态、用户决定或写入权限。

### 代码级与自动化证据

- README 安全入口：`AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test`。结果为 105 个测试文件、543 tests、543 pass、0 fail；未直接执行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- Production build：Vite 6.4.3，1747 modules，2.14s。`ArtifactDialog` JS 150.73 kB，gzip 45.76 kB；CSS 118.98 kB，gzip 18.99 kB；主 JS 373.52 kB，gzip 112.34 kB；主 CSS 193.81 kB，gzip 36.02 kB。
- 模型回归覆盖重排稳定键及完全重复诊断行的唯一键；UI 契约覆盖无 index 键、分页上限、进度语义、列表绑定和末页焦点转移。

### 隔离浏览器证据

- 运行边界：`AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式临时 SQLite、随机回环端口 53649。临时库通过真实 Store 在服务启动前建立同一产物 v1/v2；61 个候选和 61 条当前意见仅投影到只读 bootstrap 响应，不是持久化治理证明。
- 1440x900：默认候选 40/61、意见 60/61；展开后分别为 61/61，按钮卸载。两条末页路径焦点分别落在“冻结候选挂载进度”和“精确版本意见挂载进度”，且仍位于产物模态框。治理台双列，页面 `scrollWidth=clientWidth=1440`。
- 320x568：重新打开后分页重置为 40/61 与 60/61；治理台、两个治理层和进度状态均为单列，分页按钮最小高度 44px。页面 `scrollWidth=clientWidth=320`；`visualViewport=320x568`，根变量 `--visual-viewport-height=568px`、`--keyboard-inset-bottom=0px`。
- 320x568 的提及入口呈现 12 个 `menuitem`；菜单边界为 left 20、right 268、top 127、bottom 505，未越出视觉视口。
- 720x450 用作 1440x900 的有效 200% CSS 视口：治理台保持单列，页面 `scrollWidth=clientWidth=720`，分页按钮仍为 44px 最小高度。
- 浏览器 console 共 0 条日志，warning/error 为 0。QA 状态记录启动后业务写入 0、Provider 调用 0、外联尝试 0、正式库使用 false、数据库服务期内未变化。停止后 53649、8770、11111、18787 listener 均为 0。

### 尚未覆盖的边界

- 真实 OS 中文 IME composition、候选窗遮挡和不同输入法行为未执行；现有证据仅覆盖 `visualViewport`/键盘 inset 代码契约与无软件键盘时的浏览器运行值。
- 未使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 做真实朗读、虚拟光标和菜单导航验收；当前证据是 DOM 角色、名称、`aria-controls`、焦点落点及自动化契约。
- 未在带刘海或 Home Indicator 的实体设备验证非零 `env(safe-area-inset-*)`；本次运行的安全区 inset 为 0，代码级 safe-area 规则与视觉视口变量不能替代实体设备结论。
- 720x450 是等效 CSS 视口，不等同于浏览器 UI 真实 200% zoom 的所有字体栅格化、操作系统缩放和辅助技术组合。
- 只读 bootstrap 治理投影只证明前端模型、稳定键、渐进挂载、响应式布局与焦点行为，不证明正式治理快照、attestation、用户决定、外部 Provider、市场数据或执行权限。
## 2026-08-23 动态讨论审计稳定投影与有界证据窗口

### 实施

- `discussionAudit.js` 为主持选择、回应关系边、候选检查点和审计发现生成完整规范化身份投影键。正常唯一记录在重排后保持同一 key；仅对完整身份完全相同的重复诊断记录追加同桶 occurrence，避免以数组位置伪装业务身份。
- `DiscussionAuditSection.jsx` 移除上述四类列表的 index key，并把服务端允许的最坏 1000 / 2000 / 100 / 200 条明细收敛为 12 / 16 / 8 / 12 的初始挂载窗口。初始 React 明细节点上限由 3300 降为 48，同时保留完整总数和按需展开。
- 四个窗口均使用原生 `progress`、独立 `useId` 列表标识、精确 `aria-controls`、封顶 `Math.min` 增量和末页焦点转移。按钮卸载后焦点落在同一窗口的可朗读进度节点，不离开对话框。
- 浏览器验收发现原有 `<details defaultOpen>` 会触发 React 未识别属性 warning。现改为显式 `open` 状态和 `onToggle`，保留“小于等于 4 条默认展开”及用户开合能力。
- `round-execution-trace.css` 为审计窗口增加证据台账式进度条、焦点环、窄容器单列、44px 移动触控目标和 forced-colors 边界，不改变只读、语义未知或无执行权限的安全语义。

### 自动化与构建证据

- README 安全入口：`AI_STUDIO_SKIP_LOCAL_ENV=1 npm.cmd --prefix frontend test`。最终结果为 105 个测试文件、545 tests、545 pass、0 fail；未直接运行 `node --test`，未恢复旧顶层 Vite/JSDOM lazy gate。
- 新增模型回归覆盖四类投影键的重排稳定性和完全重复记录唯一性；UI 契约覆盖四类窗口、进度、列表绑定、焦点保留及 `<details>` 受控开合。
- Production build：Vite 6.4.3，1747 modules，2.08s。`RoundExecutionTraceDialog` JS 32.13 kB，gzip 9.31 kB；CSS 36.87 kB，gzip 6.86 kB。主 JS 373.85 kB，gzip 112.43 kB；主 CSS 193.81 kB，gzip 36.02 kB。

### 隔离浏览器证据

- 使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 Vite root、显式临时 `qa.sqlite3`、随机回环端口 53340。验收台直接导入生产 `DiscussionAuditSection.jsx`、`discussionAudit.js` 和 `round-execution-trace.css`，不启动后端、不连接 SQLite、不调用 Provider、行情、外部网络或正式资产。
- 验收数据为 25 次主持选择、33 条回应边、17 个候选和 25 项发现，全部通过真实 `discussionAuditViewModel` 规范化，而不是手写最终 DOM。
- 默认挂载准确为 12 / 16 / 8 / 12；第一次展开为 24 / 32 / 16 / 24；末页为 25 / 33 / 17 / 25。四个进度值与 max 一致，末页按钮全部卸载。
- 四条末页焦点轨迹分别落到“主持选择挂载进度”“回应关系边挂载进度”“候选检查点挂载进度”“审计发现挂载进度”，每次均仍在 `role=dialog` 内。
- 两个详情区默认关闭，可打开、关闭并再次打开；修正后的全新浏览器标签及最终视口重载均为 0 条 warning/error。
- 1440x900：汇总四列、核心证据双列、候选双列，页面 `scrollWidth=clientWidth=1440`。
- 320x568：核心证据、候选和窗口状态均为单列，分页按钮最小高度 44px；`visualViewport=320x568`，页面 `scrollWidth=clientWidth=320`。
- 720x450 作为 1440x900 的有效 200% CSS 视口：核心证据单列、分页按钮最小高度 44px，页面 `scrollWidth=clientWidth=720`。
- 验收结束后临时 Vite 已停止，53340、8770、11111、18787 listener 均为 0。

### 证据边界

- 本次浏览器证据是生产 React 组件、模型和样式的隔离运行验收，不是完整 App 路由、真实 `/discussion-audit` API、Store 持久化或历史轮次回放验收。
- 临时 SQLite 仅作为明确的隔离资产创建，组件验收台没有连接或写入它；因此不能用本次结果证明后端数据库路径。
- 自动化证明 DOM 角色、列表绑定、焦点落点和响应式几何；未使用 NVDA、JAWS、Narrator、VoiceOver 或 TalkBack 验证真实朗读与虚拟光标。
- 720x450 是有效 CSS 视口，不等同于所有浏览器真实 200% zoom、操作系统缩放和辅助技术组合。
- 这些结果不证明语义因果、正式审计 attestation、用户最终决定、Provider 可用性、市场数据、交易能力或公开发布授权。
## 2026-08-23 四类 disclosure 受控状态与隔离浏览器验收

### 代码级证据

- `EvidenceSourcePreview` 的来源选中只负责在新选中时展开；用户随后仍可用鼠标、Enter 或 Space 收起和重新展开。来源身份变化会按新来源重置，不再直接写 `details.open`。
- `TeamResearchCard` 默认展开后保留用户选择；主客队视觉边界由 `data-team-side` 驱动，不再依赖 `:first-child` 或 `:nth-child` 的结构位置。
- `ResearchAnalyticsSummary` 将 `complete`、`partial`、`empty` 覆盖阶段与展开状态分开；阶段从可用切为空时关闭，恢复可用时重新呈现，同时不会把历史统计升级为预测或授权。
- `StorageDataReadinessPanel` 的前置条件 disclosure 随 `view.checked` 阶段给出默认状态，不再通过 React `key` 重挂载 DOM。
- 四类 summary 均具有显式 `aria-controls`、同步的 `aria-expanded`、可解析目标节点和 reduced-motion 下静止的方向指示。代码未改变 API、Store、数据模型、准入规则或执行边界。

### 受保护回归与 production build

- 安全入口：`AI_STUDIO_SKIP_LOCAL_ENV=1; npm.cmd --prefix frontend test`。结果为 105 个测试文件、545 tests、545 pass、0 fail；未直接运行 `node --test`，也未恢复旧的顶层 Vite/JSDOM lazy gate。
- Production build：Vite 6.4.3，1747 modules，9.18s。主 JS 为 373.85 kB（gzip 112.42 kB），主 CSS 为 193.81 kB（gzip 36.02 kB）。
- 直接相关 lazy chunk：FootballResearchPanel JS 15.39 kB（gzip 5.36 kB）、CSS 13.91 kB（gzip 3.28 kB）；MarketSnapshotCard JS 34.96 kB（gzip 11.57 kB）、CSS 22.02 kB（gzip 4.19 kB）。

### 隔离浏览器证据

- 环境仅使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime、显式 0 B 临时 SQLite 和随机回环端口 54624。harness 导入真实生产组件与样式；球队内部卡片仅由临时 Vite transform 暴露，不修改项目源码。验收后标签页、viewport override、listener、目录联接和整个临时 runtime 均已清理。
- 1440x900：两列各 559px，document/body 横向溢出均为 0；四类 `aria-controls` 均解析到真实节点，summary 高度为 45/44/63/44px。
- 交互：来源选中后自动展开，Enter/Space 收起与展开时焦点留在 summary；统计 complete -> empty 自动关闭、恢复 complete 后重新展开；球队与准入 disclosure 的 `open`、`aria-expanded` 和 summary 焦点一致。
- 320x568：单列 285px，document/body 横向溢出均为 0；两项 QA 控制均为 44px；四类 summary 为 74/68/63/44px；历史基准表转为块布局。
- 720x450 仅作为 1440x900 的有效 200% CSS 视口：两列各 321.5px，横向溢出为 0，四类 summary 仍不低于 44px。该结果不是浏览器原生 200% zoom 的等价证明。
- 最终新标签页 console 为 0 warning、0 error。准备阶段曾出现三类 harness 问题：长内联命令被策略拒绝、缺少 React JSX 插件、临时根无法解析 `react-refresh`；它们均发生在临时验收层，修正后使用新标签页重新采证，不计作项目运行错误。

### 仍未覆盖的边界

- 本批浏览器证据不证明完整 App 的 lazy host、后端/API/Store、真实 Provider、Futu/OpenD、SEC/IR 或正式 SQLite 行为。
- 未执行真实 IME 组合输入、实体屏幕阅读器导航、设备 safe-area inset、浏览器原生 200% zoom 或不同系统字体渲染；不得据此声称这些边界已验收。
- 本批保持研究只读和 fail-closed 语义，不构成正式迁移、Provider 调用、市场数据真实性、投资有效性或任何执行授权。
## 2026-08-23：ProjectReadiness 原生按钮语义与 1,500 行隔离验收

### 代码级证据

- 闸门折叠控件继续使用原生 `type="button"`，保留 `aria-controls`、`aria-expanded` 与单一 `onClick` 状态路径；组件不再实现 Enter/Space `onKeyDown` 分支，避免与浏览器原生激活重复触发。
- 折叠闸门的行 DOM 只在 `expanded` 时创建；三组上限输入不会同时把 1,500 行挂入 DOM。
- locator 使用 `useDeferredValue` 并显式公开 pending 状态；搜索只改变只读投影，不改变原始闸门计数或权限边界。
- 闸门跳转在展开目标后通过 `requestAnimationFrame` 聚焦带 `tabIndex={-1}` 的目标 section；行键继续绑定无歧义的 `[code, itemKey]` 序列化 identity。

### 安全回归与 production build

- 安全入口：`AI_STUDIO_SKIP_LOCAL_ENV=1` + `npm.cmd --prefix frontend test`，实际调用 `frontend/scripts/run-tests-safe.ps1`；未直接运行 `node --test`，也未恢复旧顶层 Vite/JSDOM lazy gate。
- 结果：105 个测试文件，545 tests，545 pass，0 fail，0 cancelled，0 skipped，0 todo。
- production build：Vite 6.4.3，1,747 modules transformed，2.10 s。
- 主入口：`index-C1AEgPkv.js` 373.85 kB / gzip 112.42 kB；`index-CnQsKIag.css` 193.81 kB / gzip 36.02 kB。
- ProjectReadiness：`ProjectReadinessPanel-CWqGIp92.js` 27.20 kB / gzip 9.02 kB；`ProjectReadinessPanel-DQWwlTJ6.css` 12.65 kB / gzip 2.33 kB。

### 隔离真实组件浏览器证据

- 运行边界：仅使用系统临时 runtime、`AI_STUDIO_SKIP_LOCAL_ENV=1`、显式 0 B 临时 SQLite、随机回环端口 60494；挂载真实 `ProjectReadinessPanel` 与完整应用基础 CSS，仅将 API/投影输入替换为三组各 500 条的只读上限夹具。未启动项目后端，未连接 Provider、Futu/OpenD、SEC/IR 或正式数据库。
- 1440x900：初始展开状态为 `true/false/false`，挂载行为 `40/0/0`；“再显示”后为 `80/0/0`。结构闸门精确跳转后 hash 与 active element 均落到 structural section。
- 1440x900 locator：查询“共同定位”显示 `命中 1500 / 1500`，三组展开但仅挂载 `40/40/40 = 120` 行，`aria-busy=false`；清空后恢复 `40/0/0`。document/body 均无横向溢出，闸门跳转与折叠按钮最小尺寸均为 44x44 px。
- 320x568：单列 metrics，初始只挂载 `16/0/0`，“再显示”后 `32/0/0`；1,500 命中时仅挂载 `16/16/16 = 48` 行。无横向溢出，跳转、折叠与搜索输入高度均不小于 44 px。
- 720x450：作为 1440x900 的有效 200% CSS 视口等价检查，初始挂载 `40/0/0`，无横向溢出，交互控件保持 44 px。该项不是浏览器原生 zoom 测试。
- visualViewport：三个视口均存在，offset 为 0、scale 为 1；宽度分别因纵向滚动条为 1425、305、705 px。浏览器支持 `env(safe-area-inset-bottom)` 语法，但本批没有真实刘海/圆角设备 inset 证据。
- 最终 clean tab console：0 warning、0 error；仅 Vite 连接 debug 与 React DevTools info。
- 清理：临时 runtime 已删除，60494、8770、11111、18787 最终均为 0 listener。

### 明确未覆盖的边界

- 原生 `<button>` 语义由源码契约与 545/545 安全回归支持；本次 Browser 驱动对正确签名的 `keypress` 连 Tab 焦点都未移动，因此不把真实 Space/Enter 键盘激活写成浏览器通过。
- locator 通过普通 `fill` 验证，不等同于真实中文 IME composition 会话；未使用真实输入法候选窗。
- 未执行 NVDA、Narrator、VoiceOver 等真实屏幕阅读器；ARIA 与 focus 结论仅限代码契约及可观测 DOM。
- 未执行浏览器原生 200% zoom，也未在真实 safe-area 设备上验证 inset；720x450 仅为有效 CSS 视口等价场景。
- 本隔离组件不包含 modal，因此本批没有新增 modal focus trap/restore 的运行时证据；完整回归通过不替代真实辅助技术验收。
- 上述结果不构成正式迁移 apply、Provider/市场连接、部署、发布、业务执行或用户批准。
