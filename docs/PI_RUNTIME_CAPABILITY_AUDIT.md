# Pi runtime capability audit

**Status:** bounded documentation audit; no adapter implementation

**Reviewed:** 2026-08-14

**Scope:** the Pi slice of [Durex #25][durex-25], constrained by the intended
adapter contract in [Durex #30][durex-30]

This note distinguishes ACP, Pi RPC, and the Pi SDK. It does not select a model
provider, change Durex runtime ownership, or claim that source review replaces
the conformance probes below.

## Audited pin set

| Component | Audited pin | Evidence |
| --- | --- | --- |
| Pi | `@earendil-works/pi-coding-agent@0.83.0`; commit `845d6ff1`; npm integrity `sha512-uYhF+FsZxogoSX/AxBcUdiY+ZklubwaXyAoEGA2eQwsHcyEAhUYIKh/WLXe/a8+k8eTCmxb+ZN2Zo9mzQtzbWw==` | [npm metadata][pi-npm], [source][pi-source] |
| Pi ACP adapter | `pi-acp@0.0.33`; tag/commit `1bfcb394`; npm integrity `sha512-vX9kY1tK14E72G4dBAx+RGCk/k7XPjTHls6dLUxA8WSkBav6B6JHuSBv3eusp50LCR/GTRsR2kIKsG0Z5jANzw==` | [npm metadata][pi-acp-npm], [tag][pi-acp-tag] |
| ACP TypeScript SDK used by `pi-acp` | resolved `@agentclientprotocol/sdk@0.26.0`; commit `73bc3064`; ACP schema `v1.13.7` | [`pi-acp` lock][pi-acp-lock], [SDK changelog][acp-sdk-changelog] |
| ACP wire contract | protocol version `1`, schema tag `schema-v1.13.7`; commit `1b8e7985` | [schema release][acp-schema-release], [schema][acp-schema] |

These are audit pins, not an instruction to use `npx` with an unbounded latest
version. An implementation must verify the package integrity, retain the exact
version pair, and reject an incompatible ACP initialization result.

## Surface distinction

| Surface | Process and ownership boundary | Verified capability | Relevant gaps | Durex disposition |
| --- | --- | --- | --- | --- |
| **ACP through `pi-acp`** | Durex owns and supervises one `pi-acp` subprocess. The adapter speaks ACP JSON-RPC 2.0 over stdio and owns the child `pi --mode rpc` process. | ACP initialization/capability negotiation; new and loaded sessions; streamed assistant and thought chunks, tool calls, tool updates, locations, and structured edit diffs; cancellation; model/thinking configuration; terminal authentication; `select`/`confirm` extension UI mapped to ACP permission requests. [Adapter overview][pi-acp-readme] [capabilities][pi-acp-agent] [event mapping][pi-acp-session] | MVP stability; local stdio only; no delegated ACP filesystem/terminal; MCP params are not wired to Pi; no advertised ACP resume/close; no translated `usage_update`; `input`/`editor` interactions are cancelled; child RPC framing and structured diff paths have compliance gaps; Pi turn errors lose fidelity and a child crash can leave a prompt unresolved; concurrent ACP prompts are queued as later turns rather than preserving Pi steer/follow-up semantics. [Limitations][pi-acp-limitations] [RPC wrapper][pi-acp-process] [error mapping][pi-acp-error] [interaction mapping][pi-acp-interactions] | Primary integration surface. Do not infer unsupported capabilities from Pi RPC features hidden behind the adapter. |
| **Pi RPC** | A language-neutral controller owns a Pi subprocess directly over strict LF-delimited JSONL. It is an alternative ownership boundary, not a second control channel into the Pi process already owned by `pi-acp`. | New/switch/fork session commands; prompt, steer, follow-up, abort; model state; streamed message/tool/queue/retry events; final `agent_settled`; extension UI requests; session usage and cost snapshots. [RPC protocol][pi-rpc] [settlement][pi-rpc-settled] [usage][pi-rpc-usage] | Pi-native command/event schema with no documented ACP-style version/capability handshake; clients must implement framing, process supervision, backpressure, normalization, and unsupported-feature detection. | Bounded native extension only for a required guarantee that ACP cannot preserve. Never dual-own one session. |
| **Pi SDK** | Pi is embedded in a Node.js process through `AgentSession` or `AgentSessionRuntime`; the host owns runtime replacement, resources, credentials, events, and disposal. | Direct typed lifecycle and event access, including session replacement and abort. [SDK API][pi-sdk] | Couples Durex to an in-process Node runtime and Pi internals; reduces subprocess isolation and conflicts with #30's explicit Python-process boundary. | Rejected for the Durex Python runtime. It remains reference evidence for Pi semantics, not an adapter path. |
| **PTY/interactive CLI** | Durex owns a terminal process and parses presentation output. | Emergency compatibility with an interactive Pi installation. | No stable structured contract for events, tools, interactions, usage, or completion. | `pty_degraded` fallback only; not a substitute for ACP or RPC conformance. |

## Capability result

| Contract area | ACP/`pi-acp` result at the audited pins | Native Pi result | Required Durex behavior |
| --- | --- | --- | --- |
| Start and identity | `session/new` validates an absolute `cwd` and starts Pi. The adapter normally adopts Pi's `sessionId`, but falls back to a random UUID if its initial state query fails; it intentionally keeps one live Pi child per connection. [ACP sessions][acp-sessions] [adapter identity][pi-acp-identity] [adapter load policy][pi-acp-load] | RPC `get_state` exposes `sessionId`, `sessionFile`, and streaming state. | Durex run/session IDs remain authoritative and map to the returned opaque adapter ID; do not assume it is Pi's native ID. One supervisor owns each process tree. |
| Resume | The adapter advertises `loadSession`, persists an adapter-ID-to-file map, replays history, and then returns. It does not advertise the no-replay ACP `resume` capability. [ACP load contract][acp-sessions] [adapter implementation][pi-acp-load] | RPC can switch to a session file and can expose append-only entries and a leaf cursor. [RPC sessions][pi-rpc-sessions] | Normalize Pi `load` as Durex resume only after cwd, replay ordering, missing-file, and duplicate-delivery probes pass. Do not call unadvertised ACP methods. |
| Follow-up input | ACP defines another `session/prompt` after a completed turn. The adapter accepts concurrent prompts but queues them client-side until `agent_settled`. [ACP prompt turn][acp-prompt] [adapter queue][pi-acp-queue] | RPC distinguishes `steer` from `follow_up` and emits queue updates. [RPC prompting][pi-rpc-prompting] | Baseline contract is sequential follow-up. Expose mid-run steering only as a separately declared native capability. |
| Events, tools, and diffs | Standard `session/update` covers messages and tool lifecycle; the adapter emits text, thought chunks, tool status, locations, output, and structured edit diffs. Its child reader uses Node `readline`, which Pi explicitly excludes for strict LF framing, and a structured diff reuses the raw Pi tool path, which can violate ACP's absolute-path requirement. [ACP updates][acp-overview] [ACP tools][acp-tools] [adapter mapping][pi-acp-session] [RPC wrapper][pi-acp-process] | RPC exposes finer message, turn, retry, compaction, tool, queue, and settlement events. [RPC events][pi-rpc-events] | Preserve per-session order and stable tool-call correlation; require protocol-compliant framing and absolute normalized paths; bound buffered output. Preserve thought only when emitted; do not synthesize plan or usage events. |
| Interaction and approval | ACP has `session/request_permission`. The adapter uses it only for Pi extension UI `select` and `confirm`; it cancels `input` and `editor`. Pi has no built-in tool-permission popups. [ACP permission][acp-tools] [adapter interactions][pi-acp-interactions] [Pi philosophy][pi-readme-philosophy] | RPC exposes the complete extension UI request/response subprotocol. [RPC UI][pi-rpc-ui] | Durex retains approval policy and deduplication. Treat only advertised/mapped interactions as supported; never interpret normal tool events as approval requests. |
| Cancellation | ACP requires cancellation to stop model/tool work, flush preceding updates, and finish the pending prompt with `cancelled`. [ACP cancellation][acp-prompt] The adapter clears queued turns, sends Pi `abort`, waits for settlement, and maps the result to `cancelled`. [adapter cancellation][pi-acp-cancel] | RPC provides `abort`, `abort_bash`, and aborted/error event reasons. [RPC abort][pi-rpc-abort] | Cancellation is not complete until the prompt terminal response and owned process cleanup are observed. Ignore neither late pre-response updates nor child-process leakage. |
| Errors and settlement | **Blocking gap:** the pinned adapter ignores Pi's streamed error reason and resolves settlement as ACP `end_turn`, so success and runtime failure are not reliably distinguishable. Its RPC wrapper only rejects currently pending command requests on child exit; after prompt acceptance, a Pi crash can leave the ACP prompt unresolved instead of producing a terminal failure. [adapter event mapping][pi-acp-session] [adapter error mapping][pi-acp-error] [RPC wrapper][pi-acp-process] | RPC reports error/aborted message events and emits `agent_settled` only after retry, compaction retry, and queued continuations finish. [RPC settlement][pi-rpc-settled] | A production adapter must recover a truthful, bounded normalized failure without opening a second controller. Prefer an upstream/adapter correction; otherwise use one exclusive RPC transport for that session. |
| Health and crash | ACP v1 defines lifecycle methods but no health method; stdio process state is the observable boundary. [ACP overview][acp-overview] [ACP transport][acp-transport] | RPC likewise relies on subprocess liveness plus command/event progress. | Durex owns startup timeout, heartbeat/watchdog policy, stderr capture bounds, exit classification, restart, and cleanup. |
| Usage and provider metadata | ACP schema supports optional `usage_update`, but the pinned adapter does not translate Pi usage. Model configuration is exposed, not an authoritative cumulative usage stream. [ACP usage][acp-prompt] [adapter event mapping][pi-acp-session] | `get_session_stats` returns token, cost, and context usage; assistant messages include provider/model and usage. [RPC usage][pi-rpc-usage] | Report usage as unsupported on the ACP-only path. If budgets require it, this is a justified native gap, but values must remain provider-reported rather than fabricated. |
| Authentication and project trust | The adapter advertises Terminal Auth and launches Pi login out of band; Pi resolves runtime overrides, stored credentials, environment variables, then provider fallback. The adapter starts non-interactive RPC without an explicit project-trust flag, so Pi applies saved trust or `defaultProjectTrust`; the default `ask` ignores project-local resources because RPC cannot prompt. [adapter auth][pi-acp-auth] [Pi auth][pi-sdk-auth] [Pi project trust][pi-readme-trust] [RPC wrapper][pi-acp-process] | RPC inherits the separately configured Pi credential store, environment, and project-trust policy. | Operators own Pi/provider configuration and an isolated Pi config directory. Durex must choose project trust explicitly, pass only an allowlisted environment, and never persist provider tokens in its queue, logs, or session mapping. |
| Local/remote and stability | The pinned path is local ACP stdio. ACP remote transport remains outside the stable audited contract, and `pi-acp` warns of minor breaking changes. [ACP transport][acp-transport] [adapter status][pi-acp-status] | RPC is also local stdio. | Remote execution requires a later authenticated transport design. Pin all packages and fail closed on protocol/capability mismatch. |

## Recommendation

Classify Pi as **`acp_plus_native`**, with these boundaries:

1. **ACP-first:** `Durex supervisor -> pi-acp@0.0.33 -> pi@0.83.0` is the
   normal path. Durex owns the adapter process; `pi-acp` alone owns its Pi child.
2. **Native only for declared gaps:** error fidelity is required before
   production enablement. Usage is required only if the common runtime contract
   enforces budgets. Steering/follow-up remains optional unless promoted into
   that contract.
3. **One transport owner per session:** satisfy a native gap by improving or
   replacing the adapter for that session, never by attaching ACP and RPC
   controllers to one Pi child.
4. **No SDK in Durex:** do not embed the Node SDK in the Python process.
5. **Degraded fallback is explicit:** PTY may preserve emergency execution, but
   must advertise no structured resume, approval, usage, or event guarantees.

This preserves #30's ACP preference while making #25's exceptions measurable.
No Pi adapter should ship until the conformance scenarios below pass; in
particular, failures and cancellations must produce bounded normalized outcomes
rather than optimistic `end_turn` results or hanging prompts.

## Required conformance scenarios

1. **Pin and initialize:** verify package integrity, ACP protocol `1`, exact
   advertised capabilities, unknown fields, unsupported protocol versions, and
   stdout free of non-ACP data.
2. **Authentication and trust:** test configured and unconfigured providers,
   terminal-auth recovery, redacted stderr/logs, an isolated Pi config directory,
   an allowlisted child environment, and fail-closed project-resource trust.
3. **Start and ownership:** create with valid/invalid/relative cwd; correlate
   Durex, adapter, and Pi IDs, including failed initial state lookup; prove one
   adapter and one Pi child; bound startup timeout.
4. **Streaming and backpressure:** exercise fragmented JSON, `U+2028`/`U+2029`
   inside JSON strings, large text, tool deltas, absolute normalized edit-diff
   paths, stderr floods, and a slow consumer while preserving order and bounded
   memory/output.
5. **Interactions:** accept, reject, and cancel `select`/`confirm`; verify pending
   permission cancellation; assert `input`/`editor` and ordinary tool calls are
   not misreported as supported approvals.
6. **Cancellation:** cancel during model streaming, a long tool, retry, queued
   follow-up, and pending permission; require queue clearance, `cancelled`, no
   updates after the prompt response, and no surviving child work.
7. **Load/resume:** restart the adapter, load by opaque ID, replay once in order,
   continue the conversation, and reject unknown IDs, missing files, and cwd
   mismatches without duplicate processing.
8. **Failure truthfulness:** inject Pi crash before and after prompt acceptance,
   adapter crash, malformed/truncated RPC events, malformed ACP, and provider
   failure; require a deadline-bounded normalized failure rather than `end_turn`
   or a hanging prompt, with deterministic cleanup and retry ownership.
9. **Usage and metadata:** compare model/provider/token/cost data with Pi RPC;
   ACP-only mode must report unsupported when no `usage_update` exists.
10. **Compatibility:** run shared fixtures against Pi and OpenCode for start,
    output, tool result, interaction, cancel, failure, and load; equivalent
    semantics must produce equivalent normalized outcomes.

## Non-goals and residual evidence gap

No adapter, extension, package upgrade, remote transport, or runtime code is
implemented here. This pass is based on pinned primary documentation and source;
it does not claim live ACP smoke coverage. ITEM-2 independently reverified the
pins, principal capability and absence claims, and recommendation; the corrected
open gaps remain conformance requirements rather than implementation claims.

[durex-25]: https://github.com/kinderp/durex/issues/25
[durex-30]: https://github.com/kinderp/durex/issues/30
[pi-npm]: https://registry.npmjs.org/@earendil-works/pi-coding-agent/0.83.0
[pi-source]: https://github.com/earendil-works/pi/tree/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent
[pi-readme-philosophy]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/README.md#philosophy
[pi-readme-trust]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/README.md#project-trust
[pi-sdk]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/sdk.md#createagentsessionruntime-and-agentsessionruntime
[pi-sdk-auth]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/sdk.md#api-keys-and-oauth
[pi-rpc]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md
[pi-rpc-prompting]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md#prompting
[pi-rpc-abort]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md#abort
[pi-rpc-usage]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md#get_session_stats
[pi-rpc-sessions]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md#session
[pi-rpc-events]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md#events
[pi-rpc-settled]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md#agent_settled
[pi-rpc-ui]: https://github.com/earendil-works/pi/blob/845d6ff1f6643aba440341cce877ce1c43ebbc39/packages/coding-agent/docs/rpc.md#extension-ui-protocol
[pi-acp-npm]: https://registry.npmjs.org/pi-acp/0.0.33
[pi-acp-tag]: https://github.com/svkozak/pi-acp/tree/1bfcb394088ed879db8fd936b570bb626017f878
[pi-acp-readme]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/README.md
[pi-acp-status]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/README.md#status
[pi-acp-limitations]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/README.md#limitations
[pi-acp-lock]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/package-lock.json#L33-L40
[pi-acp-agent]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/agent.ts#L237-L269
[pi-acp-auth]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/agent.ts#L237-L253
[pi-acp-error]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/agent.ts#L885-L892
[pi-acp-load]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/agent.ts#L930-L1108
[pi-acp-identity]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/session.ts#L190-L232
[pi-acp-session]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/session.ts#L520-L868
[pi-acp-process]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/pi-rpc/process.ts#L88-L145
[pi-acp-interactions]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/session.ts#L871-L967
[pi-acp-cancel]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/session.ts#L337-L398
[pi-acp-queue]: https://github.com/svkozak/pi-acp/blob/1bfcb394088ed879db8fd936b570bb626017f878/src/acp/session.ts#L337-L369
[acp-sdk-changelog]: https://github.com/agentclientprotocol/typescript-sdk/blob/73bc30649b650de320340c782733bf69a545bd28/CHANGELOG.md#0260-2026-06-16
[acp-schema-release]: https://github.com/agentclientprotocol/agent-client-protocol/releases/tag/schema-v1.13.7
[acp-schema]: https://github.com/agentclientprotocol/agent-client-protocol/blob/1b8e79850c8d007caaf2e8cc928e7ea6b2a75685/schema/v1/schema.json
[acp-overview]: https://github.com/agentclientprotocol/agent-client-protocol/blob/schema-v1.13.7/docs/protocol/v1/overview.mdx
[acp-sessions]: https://github.com/agentclientprotocol/agent-client-protocol/blob/schema-v1.13.7/docs/protocol/v1/session-setup.mdx
[acp-prompt]: https://github.com/agentclientprotocol/agent-client-protocol/blob/schema-v1.13.7/docs/protocol/v1/prompt-turn.mdx
[acp-tools]: https://github.com/agentclientprotocol/agent-client-protocol/blob/schema-v1.13.7/docs/protocol/v1/tool-calls.mdx
[acp-transport]: https://github.com/agentclientprotocol/agent-client-protocol/blob/schema-v1.13.7/docs/protocol/v1/transports.mdx
