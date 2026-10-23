# Reachy core safety audit

Date: 2026-10-23. Repository: `waldiez/wactorz`; existing draft PR #200, targeting `dev`.

**Final recommendation: NO-GO for merge. Publication to the existing draft is authorized
for CI and human review only.** The PR-introduced Starlette metadata/security conflict
is resolved with upstream SDK 1.12.0. The dashboard upload/inference surface and Greek
entity-selection expansion have been removed. The inherited Python 3.10 Pillow audit
failure remains, including a critical advisory. A separately confirmed inherited
trusted-interface forwarding/admission flaw also remains; hardware, platform and deployment
validation is incomplete. Tests do not establish safety by themselves.

## Exact reviewed inputs

| Input | Commit |
| --- | --- |
| Updated `origin/dev`, fetched and verified remotely | `37cec13f406ba5a3f1b9d9022c0f9f96b98fc65e` |
| Complete reviewed source/test head | `eaefc2297800e576916c9e7fb71f9ab28efc3e33` |
| Preserved local integration merge | `1fbf808435581f260cb5835e0aae57138483eb2a` |
| Published Reachy head before this remediation | `b4228a2aedc8937777e8c6c25179e5869428f74b` |
| Original common ancestor | `09595dcee2be5c2f8c5019ba85c796383c1139ba` |

The final audit update follows that reviewed source/test head; it does not change
runtime code. Runtime changes were complete at 9a1515bc; the later test-only
commit eaefc229 corrects platform assumptions without changing production guards. These explicit SHAs avoid
a self-referential report commit hash. The integration has parents `b4228a2a...` and
`37cec13f...`, bringing 227 incoming dev commits into the feature branch, including
package approval, node admission/signatures, artifacts and repair safeguards. It does
not bring Reachy into dev. No published history was rewritten.

Local protected refs remain `dev=a720ad8a1340adb89329542d21cb8583923c99a1` and
`main=2d5a2419f5ba11b7800c1151e9eecfdefb8b66d2`. Existing untracked image assets and
`assets-export/` remain untouched and excluded. `.local/` evidence is excluded.
No root/nested CLAUDE.md existed; AGENTS.md and frontend/CONTRIBUTING.md were followed.

## Every earlier finding and its disposition

| Earlier finding | Current disposition / evidence |
| --- | --- |
| High: pip installed vulnerable Starlette under SDK 1.8.4 | **Resolved.** SDK 1.12.0 publishes `starlette>=1.3.1,<2`. No Starlette override or falsified SDK metadata. Actual pip extra installs Starlette 1.9.0; actual locked uv extra installs 1.7.0; both dependency checks and isolated SDK checks pass. |
| High: inherited Pillow 12.3.0 advisories | **Open, inherited; critical maximum severity.** The same 11 affect dev's Python 3.10 projection. All published fixes require Pillow 13 / Python 3.11+. Details and a separate core remediation plan below. No audit exclusions added. |
| Medium: SDK/Starlette dependency inconsistency | **Resolved in clean environments.** Both clean extra environments have compatible metadata. The working development venv still has an unrelated ambient virtualenv/filelock inconsistency; it is not evidence about clean installs. |
| Medium: unbounded new dashboard STT buffering/inference | **Removed from PR scope.** `wactorz/ext/stt`, its new endpoint, new browser/server engine selection and corresponding feature-specific tests are removed. Existing dev frontend source, default-off flag and tests are restored; assets rebuilt through `make build-frontend`. Robot-side STT remains opt-in. |
| Medium: universal catalogue recipe refresh | **Narrowed and guarded.** Only `refresh_on_restore` recipes refresh reviewed code; only Reachy opts in. Saved settings/state/placement survive. Changed dependency lists stop that restoration before local/node dispatch and require a fresh approved catalogue spawn. Repaired/untrusted entries are never upgraded to trusted. |
| Medium: shared websockets downgrade | **Remains a compatibility limitation.** SDK 1.12.0 still requires `<16`; universal lock selects 15.0.1 instead of dev's 16.1.1. Real local HA protocol sessions and unit regressions pass. Live HA/broker/node/other extra deployment validation remains needed. |
| Low: generic missing-module repair behavior | **Scoped.** Only still-trusted programs stop and report missing setup modules. Untrusted programs retain dev's validated model repair; imports/capabilities are checked against original source, and repaired code loses trust. |
| Low: Greek HA vocabulary broadens sensitive selection | **Removed from PR scope.** The actuator implementation and tests match dev, including its existing entity/domain and social actuation controls. No new Greek generic word can select all locks/covers through this PR. This does not certify dev's existing matching policy. |
| Stored delegation text may replay | **Strengthened.** Main sanitizes complete/incomplete blocks and executable mentions before chat/task/stream/interrupted/external history storage, interface-history ingestion and delivered chat logging. Prose mentions and user text remain. Tests exercise actual persistence paths. |

The original integration regression fixes remain: generic catalogue best effort after
busy/timeouts, strict default-off installation policy, package refusal in both modes,
actor ask protocol reconciliation, version metadata checks without heavy imports,
conservative fallback without optional packaging, artifacts, and unchanged frontend
privacy defaults. Removed tests cover removed features; no retained feature's tests
were deleted, weakened or skipped to obtain passing results.

## Conflict-resolution decisions retained from the integration

The original 16 conflicted files were compared at merge-base, Reachy and dev stages.
The table records the intentional merge decisions; the remediation above supersedes
its universal refresh, dashboard STT and Starlette override choices.

| Conflicted files | Resolution and preserved invariant |
| --- | --- |
| `wactorz/core/pip.py` | Combined requirement metadata, user-site and stale-module reporting with dev's validated package syntax, configured index, and existing 180-second generic pip limit. A range that cannot be checked without optional `packaging` is no longer silently accepted. |
| `wactorz/agents/installer_agent.py` | Kept dev's approval list, operator questions, main-only node and restore actions, refusal handling, and configured index. Reachy progress and stale-package reporting run after approval. Only strict requests use the longer pip limit. |
| `wactorz/agents/catalog_agent.py` | Kept generic best-effort spawning and dev's 120-second catalogue wait. A declarative `require_install_success: true` recipe policy enables strict behavior; only the Reachy recipe declares it. Both policies stop on package refusal. |
| `wactorz/agents/mixins/spawning.py` | Used dev's `ask_through` protocol and main-only restore action, retained refusal cleanup and artifact checks, and added strict outcomes without imposing strict failure/restart behavior on generic agents. |
| `wactorz/agents/main/spawns.py` | Retained dev's placement, artifact, plugin, node and approval machinery; retained the catalogue recipe refresh import and call, reviewed separately as a broader behavior change below. |
| `wactorz/agents/main/delegation.py` | Kept dev's request/reply API, node admission and signed reply validation, and model installer-action allowlist. Structured payloads are copied and retain explicit text; generated JSON text is supplied only when text is absent. |
| `wactorz/agents/dynamic/agent.py` | Kept artifacts, original-source tracking, repair capability restrictions, validation of every model repair, and loss of trusted status after repair. Added recipe task timeout and missing-module reporting. |
| `tests/test_catalog_agent.py`, `tests/test_installer_agent_actions.py`, `tests/test_spawning.py` | Reconciled test doubles with dev's actor reply protocol. Contradictory generic versus strict expectations were separated under the human-approved policy decision. Generic anomaly-detector busy/timeout behavior and strict Reachy failure/restart behavior remain covered. `test_spawning.py` also had automatically merged edits requiring semantic reconciliation. |
| `tests/test_delegate_blocks.py`, `tests/test_dynamic_agent_self_repair.py`, `tests/test_spawn_restore.py` | Retained both branches' relevant test cases, including installer command restrictions, repair security checks, history cleanup, and both restoration fixture attributes. |
| `frontend/src/events.ts` | Kept voice-mode events together with dev's route and view-change events. |
| `uv.lock` | Regenerated with dev's versions as the solver seed and the combined `pyproject.toml`. Kept Starlette `>=1.3.1,<2`, the PyGObject metadata declaration, CPU torch sources, and dev's security-release exceptions. No new advisory exclusions were added. |
| `static/app/index.html` | Rebuilt through `make build-frontend`; bundle names were generated, not manually selected. |
| `CHANGELOG.md` | Retained both sets of entries and documented the opt-in install policy. |

## Independent shared-core classification

A = necessary general fix; B = robot-specific; C = compatibility risk requiring evidence;
D = separable scope removed or deferred. Classification covers the entire PR relative
to the updated dev baseline, not just remediation commits.

| File / behavior | Class | Assessment |
| --- | --- | --- |
| `core/pip.py` metadata, user-site and stale-distribution checks | A/C | Avoids heavy imports and detects restart needs; configured indexes and validated package syntax remain. Cross-platform live package replacement still warrants operator testing. |
| `core/packages.py` Deepgram reviewed dependency | B | Optional recipe package allowlist addition; arbitrary package approval remains required. |
| `agents/installer_agent.py` | A/B/C | Keeps dev approval/main-only controls. Strict progress/longer timeout is opt-in; generic timeout remains. Runtime installs can replace shared dependencies and require restart. |
| `agents/dependency_install.py` | B | Strict outcome adapter through the existing ask API, no authorization grant. |
| `agents/catalog_agent.py` | A/B/C | Readable replies and metadata scans; Reachy-only strict install, Python minimum and opt-in refresh. Dependency migration refuses safely; no automatic configuration/state schema migration is promised. |
| `agents/mixins/spawning.py` | A/B/C | Artifact and approval checks retained; generic best effort retained; strict failures/restarts stop opted-in recipes. Private outcome type changes can affect external custom mixins. |
| `agents/main/spawns.py` | B/C | Calls the guarded recipe refresh; restore failure is logged and does not strand other agents. |
| `agents/dynamic/agent.py` | B/C | Recipe timeout defaults unchanged; missing-module stop is limited to trusted code. Every model repair is validated and loses trust. |
| `agents/main/actor.py`, `agents/llm_agent.py` | A/C | History hook defaults to identity outside main. Main stores conversational content through all paths, while returning raw output for the current dispatcher to process. Voice OTHER stale HA block guard remains; live noisy speech is unverified. |
| `agents/main/delegation.py` | A | Copies structured payloads, retains explicit text and existing actor correlation, admission and signed node replies. |
| `catalogue_agents/reachy_mini_agent.py`, `reachy_stt.py`, `reachy_vad.py` | B | Optional robot program, motion/audio lifecycle and lazy SDK/cloud imports. No default construction requires a robot or key. |
| `pyproject.toml`, `uv.lock` | B/C | Robot extra is not in all. SDK requires Python 3.11+. Universal lock remains affected by robot constraints. Native dependency metadata and targeted unusable-wheel exclusions need platform validation. |
| Dashboard STT/browser engines, Greek actuator expansion | D | Deferred; frontend source/assets and actuator code/tests now have no delta from dev. |

There is no diff against dev in Dockerfile, Home Assistant add-on files, node code,
web core, config.py or app.py. This is source evidence, not deployment validation.

## Security invariants and independent runtime review

- **Package approval:** installer actions remain restricted by `_asked_by_main` for
  NODE_ACTIONS/PACKAGE_ACTIONS (`installer_agent.py:369,374`); `_cleared` still obtains
  operator approval (`:488`). Both policies reject refusal; strict mode cannot grant
  allowlist entries. Package syntax remains an allowlist, not shell/options/URLs.
  Configured index arguments survive. Tests cover refusal, main-only restore actions,
  busy/timeouts and stale-module restart behavior with Reachy and anomaly-detector.
- **Restoration approval:** `catalog_agent.py:295-319` checks dependency equality before
  refresh. It fails closed instead of treating new pins as previously approved.
  `main/spawns.py:416` runs this before remote/local dispatch; its per-agent exception
  boundary keeps unrelated agents restoring. `test_spawn_restore.py` covers dependency
  changes locally/remotely, instance settings/state, unrelated agents and untrusted
  entries. Equal dependencies permit code refresh only for a recipe opting in; these
  recipes must maintain saved configuration/state compatibility. Future incompatible
  schema changes need a separately reviewed migration, not this helper.
- **Repair/trust:** `dynamic/agent.py:621-646` retains original-source capability/import
  checks and loss of trust. Missing modules stop only trusted source (`:744`).
  Trusted code is a project-reviewed boundary; model-repaired code never inherits it.
- **Delegation:** `main/delegation.py:130-203` subscribes before publishing, admits only
  replies signed by the target node, and cleans futures. Local requests still use
  ask_through correlation. Payload copying does not authorize model installer node
  commands; the named-operation allowlist remains. `test_ask`, node signing, node LLM
  round trip and delegation tests pass without changing those protections.
- **History:** `llm_agent.py:241,354,420,495,514` routes assistant persistence through a
  default identity hook. Main's override (`main/actor.py:314`) strips transport syntax
  before summaries and persistence, including cancelled streams; interface history
  (`:489`), external exchanges (`:588`) and delivered chat log (`:624`) also sanitize.
  Final reply replacement matches the sanitized stored representation. User content
  remains data. Tests do not prove a model cannot infer actions from ordinary prose;
  execution authorization still depends on the unchanged runtime controls.
- **HA and privacy:** new Greek domain expansion is absent. Social domain restrictions,
  entity guards and existing validation remain dev's. Reachy's microphone is explicitly
  started; Deepgram is cloud processing and needs the operator's key. No new dashboard
  endpoint/browser speech fallback or browser-facing secret config is added.

## Ranked remaining risks

| Severity | Finding / evidence | Required mitigation |
| --- | --- | --- |
| **Critical, inherited dependency exposure** | Pillow 12.3.0 on Python 3.10 remains subject to GHSA-v9pv-5gc8-w2m6 and other advisories. Optional ML/dev/document/image consumers can introduce untrusted data; no Wactorz exploit demonstrated. | Resolve upstream compatible fixes or explicitly restrict affected features in a separately approved core security change. Do not interpret inheritance as safety. |
| **High, inherited trust boundary** | Actor task text can pass through trusted Reachy into main's person-command route (`reachy_mini_agent.py:2104,2998`; `main/actor.py:457-469,509`). Forwarding/admission confirmed without executing the operator command. | Authenticate original input provenance and block operator commands from actor text; compositional regression required. See the finding below. |
| **Medium, shared lock compatibility** | SDK `<16` forces websockets 15.0.1 in universal locked environments. | Keep real HA protocol checks; run configured HA and optional-extra deployment checks. Prefer upstream widened support over a metadata override or isolated environment architecture change. |
| **Medium, upstream Windows subprocess compatibility** | Bundled GStreamer mutates PYTHONPATH/GST_PYTHONPATH_1_0 at Python startup. A Python child inheriting the latter can append a path list as one sys.path entry and fail to import gi; inherited PYTHONPATH can also make evidence use the parent venv. | SDK tests now use isolated Python and clear those two inherited path variables to measure the intended environment. Ordinary clean shell startup works; arbitrary embedded/subprocess SDK launches need upstream guidance and deployment testing. No production SDK patch is introduced. |
| **Medium, installation lifecycle** | Preinstalled extras pass; in-process installation can replace loaded shared distributions. Legacy repaired/untrusted saved recipes intentionally are not silently replaced and may still request an older SDK. | Prefer preinstall, obey strict restart reports, review/recreate legacy repaired recipes. Do not edit registry trust or bypass approvals. |
| **Medium, unverified hardware/native platforms** | SDK 1.12.0 adds mandatory onnxruntime and changed media internals; contract attributes do not establish motion/media correctness. Linux GI/Cairo/GStreamer builds and 3.14 wheel support are not exercised. | Supervised hardware and supported environment validation before merge. |
| **Low, explicit feature compatibility limit** | Core supports Python 3.10; Reachy requires 3.11+. On 3.10 the extra's markers install base only; catalogue refuses the robot before installing. | Documentation is explicit; do not advertise Reachy support on 3.10 or install insecure older SDKs to work around it. |

### Additional independent finding: inherited trusted-interface proxy

**High, conditional on Reachy running as a still-trusted local catalogue agent.**
`reachy_mini_agent.py:2068-2104` accepts actor task text and strips an explicit
"ask wactorz" prefix (`:1584`). `_bridge_to_main` (`:2981-2999`) forwards it with
`_via_interface=True`. Main checks the *proxy's* trusted status
(`main/actor.py:457-469`), then passes the text into `process_user_input` (`:509`),
which includes operator slash commands. Original caller provenance is lost.

A non-destructive interception probe sent the actor-shaped task text
`ask wactorz /packages allow reachy-audit-probe` to the catalogue program.
Both `origin/dev` and the reviewed HEAD forwarded `/packages allow ...` with the
interface flag, and the real main admission predicate accepted the trusted proxy.
The command was intercepted; **no package approval, node command or other operator
side effect was executed**. Evidence: `.local/reachy-remediation/trusted-proxy-probe.log`.
This confirms forwarding/admission composition; the destructive end effect was not
tested. It is inherited from dev, not introduced by the remediation, and is not
reachable merely by installing default Wactorz without running this optional agent.

The direct caller/trust tests pass because they check each boundary separately;
they do not establish that trusted catalogue code authenticates the original source.
This is why successful tests are insufficient. Existing installer restrictions,
node signatures and repair guards have not been weakened, but they cannot establish
a human origin after main itself has been made to issue an operator command.

Smallest next security scope: reject operator-only command forwarding for actor/MQTT
text and design authenticated provenance outside caller-controlled payloads for any
human interface bridge; retain a separately authenticated typed/voice human path.
Do not treat a task's `_via_interface`, source string or JSON boolean as proof of a
person. Add a compositional untrusted-actor -> Reachy -> main regression covering
package allow/node commands, ordinary robot tasks and real human input. Do not
silently disable or grant a whole class of existing interface behavior without
reviewing that provenance contract. Implementation expansion stops here, per the
instruction to report an uncertain security boundary instead of guessing. Publication
is still permitted by the explicit later authorization for unresolved findings.

No confirmed ordinary default-install/chat/HA regression was found in the final
mocked/local-server evidence. That statement does not cover live deployments.

## Pillow: every advisory, paths, reachability and remediation

Primary sources are the linked upstream GitHub advisories; severities/ranges are
from their published structured records, not inferred CVSS scores. **Every row is
fixed in Pillow 13.0.0; every row affects the locked Python 3.10 version 12.3.0.**
Pillow 13 requires Python 3.11+, confirmed by
[Pillow's supported-Python table](https://pillow.readthedocs.io/en/stable/installation/python-support.html)
and [published release metadata](https://pypi.org/pypi/pillow/13.0.0/json).
No published compatible fixed 12.x release was found at review time.

| Advisory / identifier | Published severity | Affected range | Static reachability assessment |
| --- | --- | --- | --- |
| [GHSA-4rm8-3hmm-39fg](https://github.com/python-pillow/Pillow/security/advisories/GHSA-4rm8-3hmm-39fg) / CVE-2026-96641 | Medium | `< 13.0.0` | PILfont integer underflow. No application bitmap-font sink found; approved dynamic programs/plugins can call Pillow. Native DoS/integrity risk remains. |
| [GHSA-9v9v-9qvf-h2hj](https://github.com/python-pillow/Pillow/security/advisories/GHSA-9v9v-9qvf-h2hj) / CVE-2026-96644 | High | `< 13.0.0` | Windows WMF/EMF rendering. Document images reach python-pptx/Pillow headers; no direct WMF rendering sink confirmed. Optional image consumers can render supplied images. |
| [GHSA-g4qv-2jxx-769h](https://github.com/python-pillow/Pillow/security/advisories/GHSA-g4qv-2jxx-769h) / CVE-2026-96643 | High | `>= 11.0.0, < 13.0.0` | PNG EXIF parsing. Document images enter Pillow; no direct getexif call found in Wactorz. Transitive/plugin EXIF processing remains possible. |
| [GHSA-p8hx-v2p6-rp84](https://github.com/python-pillow/Pillow/security/advisories/GHSA-p8hx-v2p6-rp84) / CVE-2026-96636 | Medium | `< 13.0.0` | MPO/DCX later-frame seek bypasses bomb checks. No direct later-frame Pillow seek sink found; arbitrary approved image consumers remain exposed. |
| [GHSA-3j72-p6qg-6xp7](https://github.com/python-pillow/Pillow/security/advisories/GHSA-3j72-p6qg-6xp7) / CVE-2026-96642 | High | `< 13.0.0` | TarIO negative-size CPU loop. No TarIO/TarImageFile call found in packaged application paths. |
| [GHSA-g842-9rc4-69pw](https://github.com/python-pillow/Pillow/security/advisories/GHSA-g842-9rc4-69pw) / CVE-2026-96637 | High | `< 13.0.0` | Spider multi-frame seek allocation. No direct Spider/seek sink found in application code. |
| [GHSA-78g4-xqwv-q3g8](https://github.com/python-pillow/Pillow/security/advisories/GHSA-78g4-xqwv-q3g8) / OSV-2025-219 (bundled OpenJPEG) | High | `>= 11.1.0, < 13.0.0` | Bundled JPEG2000 decoder heap write on open/load. Optional ML image decoding and approved dynamic image consumers can reach load/convert; no exploit demonstrated in Wactorz. |
| [GHSA-24rc-8hj2-v98c](https://github.com/python-pillow/Pillow/security/advisories/GHSA-24rc-8hj2-v98c) / CVE-2026-96638 | Medium | `>= 9.1.0, < 13.0.0` | BMP RLE memory amplification on decode. Optional ML/dynamic image load/convert is a potential sink; document headers alone do not establish decoder reachability. |
| [GHSA-g7cr-3p24-7wf7](https://github.com/python-pillow/Pillow/security/advisories/GHSA-g7cr-3p24-7wf7) / CVE-2026-96778 | High | `>= 9.4.0, < 13.0.0` | JPEG comment/ICC output overflow. Reachy encodes from arrays without attacker-supplied comment/ICC metadata, so that path lacks the documented precondition. No other matching output sink confirmed. |
| [GHSA-mrfp-p49p-3hf2](https://github.com/python-pillow/Pillow/security/advisories/GHSA-mrfp-p49p-3hf2) / CVE-2026-96640 | High | `>= 5.1.0, < 13.0.0` | Pillow PDF append page-tree resource exhaustion. No Pillow PDF append call found; document PDF parsing uses PyMuPDF/pdfplumber instead. |
| [GHSA-v9pv-5gc8-w2m6](https://github.com/python-pillow/Pillow/security/advisories/GHSA-v9pv-5gc8-w2m6) / CVE-2026-96639 | Critical | `>= 11.0.0, < 13.0.0` | Multi-frame native storage mismatch, including reported code execution in matching runtime profiles. No ImageSequence/later-frame Pillow load sink found in Wactorz; plugins and approved dynamic programs can expose it. |

Dependency paths, common to these advisories:

- Python 3.10 `[dev] -> Pillow>=10` directly and `[dev] -> python-pptx -> Pillow`.
- `[ml] -> ultralytics -> Pillow`, also matplotlib/torchvision paths. The universal
  reverse tree confirms both platform/version branches. `[all]` excludes ml.
- Runtime `doc-to-pptx-agent` requests Pillow/python-pptx; its `add_picture`
  (`catalogue_agents/doc_to_pptx_agent.py:469`) reaches python-pptx's PIL Image.open
  for metadata. Header parsing is confirmed; later-frame decode/EXIF/PDF append
  is not confirmed. Arbitrary approved programs/plugins can add image sinks.
- Reachy's explicit extra/recipe now requires `pillow>=13` on Python 3.11+, so it
  does not add a vulnerable 3.10 Pillow path. Base Wactorz installs no Pillow.

Reachy's camera encoder (`reachy_mini_agent.py:6581-6587`) builds an image from a
camera array and writes JPEG/PNG without external ICC/comment metadata. This does
not match every vulnerable decoder/write precondition. The web upload store does
not itself prove Pillow decoding. No exploit fixtures were run, and absence of a
matching application call is not proof that all optional consumers are unreachable.

### Smallest actionable separate core security plan

There is **no valid one-line version bump that both fixes all 11 and preserves
Pillow-backed features on Python 3.10**. Setting Pillow>=13 globally would make the
supported 3.10 dev/ML environments unsatisfiable. Downgrading to an older Pillow
retains advisories whose ranges have no lower bound. No exclusions were added.

1. Ask upstream for a Python-3.10-compatible 12.x security release covering all
   listed advisories. Once available, the minimal core patch is to raise the Pillow
   floor in pyproject's dev dependency and document catalogue install requirement,
   add the corresponding optional ML constraint, regenerate uv.lock with the
   existing security-release policy, then run exact audit plus 3.10 document/ML
   regressions. A private fork/backport requires owner approval and is not prepared.
2. Until such a release exists, a separate owner-approved core change can fail
   closed for affected optional image/ML features on Python 3.10 while retaining
   base Wactorz support: guard `catalogue_agents/doc_to_pptx_agent.py` before importing python-pptx,
   review a guard at the dynamic-program/package admission boundary for ML imports
   (ML image consumers are generated programs, not a single ML initialization module),
   require Pillow>=13 on supported feature
   environments, and make errors explicit. Test both refusal and modern-feature
   success. This restricts feature support and needs a product decision; it is
   not silently implemented in the robot PR.
3. If the owner accepts that feature restriction, dependency declarations must
   also stop selecting vulnerable Pillow through *every* dev/python-pptx/ML
   transitive path on 3.10; use explicit environment markers and matching feature
   guards, not an audit ignore. Otherwise the universal audit correctly stays red.
   Existing 3.10 image-dependent tests require a conscious supported-feature policy,
   not blanket skips to make CI green. Format allowlists/upload size caps alone do
   not repair the native issues and are not a substitute for those decisions.

This is separate scope because dev and ML/document users without robots are
already affected. No core security PR was opened or branch created by this task.

## Dependency compatibility and isolation verdict

The verified floor is Starlette 1.3.1 for
[GHSA-82w8-qh3p-5jfq / CVE-2026-54283](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq),
a high-severity form-parsing resource-exhaustion issue affecting >=0.4.1,<1.3.1.
[Upstream SDK 1.12.0](https://github.com/pollen-robotics/reachy_mini/releases/tag/v1.12.0)
and [its metadata](https://pypi.org/pypi/reachy-mini/1.12.0/json) now declare
`>=1.3.1,<2`. This PR uses that supported range, not patched/falsified SDK metadata.
The full daemon/dashboard/network security boundary is not validated here.

The universal resolver selected GStreamer plugin 1.28.8, whose published files are
only Windows cp313t/cp314t wheels (no sdist). An actual locked install failed on
ordinary cp313. `pyproject.toml:199` excludes that single version for the five
plugin packages; the solver selects upstream-compatible 1.28.7 and actual locked
installation passes. Other GStreamer components remain at their resolved versions.
This is a compatibility constraint, not a security exception. Primary wheel evidence:
[plugins 1.28.8](https://pypi.org/pypi/gstreamer-plugins/1.28.8/json),
[plugin libraries](https://pypi.org/pypi/gstreamer-plugins-libs/1.28.8/json).
Existing PyGObject 3.46.0 published dependency metadata remains to avoid native
metadata builds while locking; it does not remove Linux runtime/build requirements.

Only Wactorz's HA integration directly imports websockets. Core dashboard WS uses
aiohttp; MQTT uses aiomqtt/paho rather than this WebSocket client. The HA client
uses stable connect/send/recv/close and authentication/request IDs/subscriptions.
`test_ha_websocket_session.py` runs actual loopback WebSocket sessions with
websockets 15, covering token acceptance/rejection, command results, service failure,
interleaved subscription events and cleanup; timeout tests exercise deadlines.
The full suite covers node/delegation paths. This supports compatibility but does
not replace a real HA instance or all optional providers. No unsupported override
was used to force websockets 16+ against the SDK.

**Default-install isolation: PASS for tested base wheel environments.** No SDK,
Deepgram, NumPy or Pillow is installed/required, no robot/key is needed to construct
main/catalogue/web, and no `/api/stt` endpoint exists. `[all]` stays robot-free.
**Shared-lock isolation: PARTIAL.** Shared locked websockets and other resolution
branches remain influenced by the optional SDK. Do not describe it as fully isolated.

## Validation actually executed after remediation

Evidence lives under `.local/reachy-remediation/` and is deliberately not committed.

| Check | Actual result |
| --- | --- |
| Complete Python suite through `make coverage-py` | **9,567 passed, 134 skipped**, 109 warnings, **92.52%**, unchanged 92% floor. Windows Python 3.13.9. Includes installation/refusal, generic/strict policy, repairs, artifacts, node signing, chat, HA, delegation and Reachy tests. |
| Final full Python suite after SDK harness change | **9,567 passed, 134 skipped, 145 subtests passed**, Windows Python 3.13.9. `test-py-publish.log`. |
| Complete frontend suite through `make coverage-frontend` | **1,338 passed, 79 files**; statements **98.37%**, branches **91.79%**, functions **97.14%**, lines **98.54%**. Floors unchanged; fewer tests because the independent STT feature was removed. |
| Explicit Reachy suite | **992 passed, 92 subtests passed**; hardware-free program behavior plus installed-SDK checks. |
| Explicit core security list | **319 passed**: installer, requirements, strict helper/policy, restore, repair, delegation, ask/correlation, node signing, node LLM roundtrip and real HA WS. |
| Raw/task/external/stream/cancelled history + main dispatch + HA WS checks | **156 passed**; additional block/mention/history run **59 passed**. Final SDK subprocess check **2 passed** after harness isolation fix. |
| Python gated lint/format/type | **Pass**: Ruff and formatter; basedpyright **0 errors, 0 warnings**. Existing advisory Ruff families remain nonblocking under Makefile policy; no gate disabled. |
| Frontend `make lint` | **Pass**: TypeScript, Prettier, ESLint, markdownlint. |
| Frontend build | **Pass** via `make build-frontend` and package asset sync. Source and committed bundles match dev again; no manual hash selection. |
| Full docs + wheel | **Pass** `make docs-build`, frontend TypeDoc and Python docs, `uv build --wheel`, `twine check`. |
| Additional Windows Python 3.10.0 full suite | **FAIL: 1 failed, 9,553 passed, 146 skipped, 145 subtests passed**, using the exact all/dev projection and installed wheel, four workers. The remaining event-loop lag assertion is in unchanged dev code/tests; see analysis below. Catalogue/policy cases pass. |
| Final lock validation | **Pass**, CI-pinned uv **0.13.3**, 263 packages. |
| Exact CI audit | **FAIL**, exit 1: the same **11 Pillow 12.3.0 advisories**, 262 audited packages. Existing Makefile ignores unchanged. No new vulnerability suppression. |
| Clean pip base wheel | **Pass**, Python **3.10.0**: imports main/catalogue and constructs web app, no robot SDK/NumPy/Pillow/Deepgram or API key; dependency check passes. |
| Clean uv base wheel | **Pass**, Python **3.13.5**, same SDK-free construction and dependency consistency. |
| Actual clean pip Reachy extra | **Pass**, Python **3.13.5**: SDK **1.12.0**, Starlette **1.9.0**, Pillow **13.0.0**, websockets **15.0.1**. pip check passes. |
| Actual clean uv Reachy extra | **Pass** unlocked installation, then **actual exact locked dependency projection installed successfully** after unusable wheel fix: SDK **1.12.0**, Starlette **1.7.0**, Pillow **13.0.0**, websockets **15.0.1**. Dependency check passes. |
| Isolated real SDK checks in both clean environments | **Pass**, actual client import/pose/middleware body limit and robot/client attribute contract. No physical connection. Starlette 1.9 emits a TestClient deprecation warning, not a failed check. |
| Merge/diff integrity | No unresolved index paths/markers; diff check passes; protected refs unchanged. |

Full coverage preceded the final test-harness-only change; both that SDK test and
the full Python suite were rerun afterward and passed. Runtime code was unchanged afterward. Documentation-only changes
do not imply another runtime validation. Initial failed attempts are not counted as
passes: one mistyped test filename collected no tests; new history fake imports and
streaming support were corrected; a first locked Windows install failed on the
free-threaded-only wheels; isolated SDK subprocesses exposed inherited GStreamer
path contamination. The final tests explicitly avoid ambient Python-path leakage.

## CI, remaining evidence and human stopping point

The first published remediation head was `fbce6a14693aa157149f01b0273bae175ceae050`.
[CI run 39900989765](https://github.com/waldiez/wactorz/actions/runs/39900989765)
checks GitHub's temporary PR merge ref `bad0c67042dff508edcbc6f4f65a115a196678b3`
against dev `37cec13f...`; this is not a merge into the protected branch.

Available results inspected for that first head: Python 3.11/3.12/3.13/3.14,
macOS, plain pip, Python/frontend coverage, frontend build/freshness, broker/node,
default image, workflow/shell checks, CodeQL and up-to-date-branch check passed.
Windows and the ultra image also completed successfully in the final snapshot of that first run. The Python lint
job failed specifically at the exact audit with the same 11 Pillow advisories;
lock freshness passed and subsequent lint/docs steps were skipped. Local lint/docs
passed separately. These are results for that first head, not claims about a later
head's rerun.

Python 3.10 had **10 failed, 9,633 passed, 57 skipped** on Linux. Each failure
was a mocked Reachy installation-policy test hitting the intentional Python 3.11
preflight before exercising its installer fake. The follow-up test-only commit
models a supported runtime in those unit fixtures without editing any recipe,
production guard, security assertion or skip. The separate real-recipe 3.10 refusal
test remains and explicitly checks the 3.11 minimum. The cross-policy tests now
also model a supported runtime, so they cannot pass for the wrong preflight reason.
Actual local Python 3.10 catalogue/policy tests pass **83 tests**, plus the final
floor/refusal/policy rerun passes **14 tests**. The same 83 pass on Python 3.13;
Ruff/format and basedpyright remain clean. The final publication triggers a fresh
CI run; pending checks must not be represented as passes.

The additional Windows Python 3.10.0 run initially used dev-only dependencies
without installed project metadata: it had five failures (9,477 passed, 173 skipped).
Installing CI's all/dev projection and the built wheel resolved the console-script
and missing-provider fixture failures; the affected-file rerun had 96 passed,
3 skipped and one event-loop assertion failure. The full corrected-environment
run had **9,553 passed, 146 skipped, 145 subtests passed, one failure**.

The remaining failure is
`test_stuck_and_blocked.py::TestTheEventLoop::test_a_block_is_still_known_after_the_loop_has_answered_again`: it waits for a strictly positive sub-0.2s measured
lag and times out. Both that test and `monitoring/loop_lag.py` have no diff against
dev. This old Windows 3.10.0 interpreter reports a 0.015625-second GetTickCount64
monotonic clock; zero-quantized fast replies are consistent with the assertion
failure, but the exact platform cause has not been conclusively isolated. No test
or product threshold was changed or skipped to hide it. Linux 3.10.22 CI reached
all other tests successfully on the prior head; the final fixture-only correction
needs its fresh CI rerun. This local Windows 3.10 full suite is not a passing result.

The original PR description's older green CI and physical robot claims do not
validate this synchronized SDK 1.12 integration. Existing core Python/OS CI uses
all/dev, not a real Reachy SDK extra installation on each platform.

Docker is installed but its Linux engine pipe is absent. Broker/node Docker tests,
soak, browser E2E, image builds/smoke/Trivy and container workflow lint were therefore
not executed. No configured live HA server or physical Reachy was available. No real
speech/cloud credentials/audio/motor test was performed. Full Python 3.10-3.14 and
Linux/macOS execution remains for GitHub Actions/current CI; only the listed local
Python/OS combinations and clean base installation were actually exercised.

Before human merge: resolve the inherited trusted-proxy flaw and the Pillow support/security decision; inspect
fresh CI on the exact published head (Python/OS/install/coverage/lock/audit/build jobs);
validate Linux native dependency setup, a configured non-Reachy HA deployment, real
broker/node signatures/round trips, and supervised robot install/restart, connection,
audio/motion and speech privacy. Check existing configured add-on values remain a
no-op. Recreate old dependency-mismatched or repaired recipes through approved flows.

**NO-GO remains.** The feature branch may be pushed for CI/review and the existing
PR body updated, as explicitly authorized. PR #200 must stay draft, target dev,
without auto-merge. Only the human owner approves and performs a merge.
