## EAA owner addendum

This applies only when Runtime State says `orchestration_owner: eaa`.
It overrides all Host-only requirements in the scenario stage prompt, format
spec, delegation-tool descriptions and child-context introductions above.
The EAA internal Agent reads and acts on the body. The Host invokes the named
scenario and relays questions/results; it does NOT orchestrate individual children.

### PREPARE and delegation

- Confirm which existing executable inline/script children are needed, including
  necessary direct dependencies. No instruction-only or nested scenario child.
- Read each child's contract and confirm operations, required data, quality
  checks, questions, consent boundaries, stopping conditions and ambiguous outcomes.
  `record_delegation` still records these facts; `sections` names contract
  headings, not calls to a public MCP tool. Copy explicit input bindings only
  when the child publishes them; do not invent a Host credentials wrapper.
- Leave `host_capabilities` empty. If the scenario needs Host-only tools, explain
  the mismatch and ask the author to change the scope or choose Host. Apply an
  explicit owner choice through `set_orchestration_owner` under the shared
  informed-consent rules; do not require a second UI action.
  Do not invent Host tasks just to satisfy a checkpoint or switch without consent.
- Confirm the dependency whitelist and `delegation_ok` through the ordinary
  PREPARE checkpoints. EAA ownership does not bypass author confirmation.

### DRAFT and REFINE

Write a lightweight natural-language SKILL.md. Use useful headings such as
Purpose, Scope, Instructions and Result handling; there is no required Host
step table, request-type table, fetch-authorization block or return-branch table.
Instructions must preserve the confirmed business rules and child boundaries.
No flow configuration or fixed-step schema is required.

```yaml
---
name: procurement-workflow
description: "Coordinate procurement analysis and author-approved deliverables."
metadata:
  skill_type: scenario-orchestration
  orchestration_owner: eaa
  children:
    - procurement-sharepoint-ocr
    - procurement-requirements-analysis
    - procurement-deliverables-generator
---
```

Use the actual confirmed names, not these example names. In the execution body,
you may identify declared children as the capabilities to dispatch. Do not name
skills in the description, routing alternatives, or outside the whitelist.

Tell the EAA Agent to read child contracts through its available INTERNAL skill
tools. Do not assume the public MCP `fetch_skill` exists internally, do not emit
`fetch_skill(...)` calls, and do not invent names or signatures for internal tools.
Do not instruct the Host to fetch or chain children, or to assemble their payloads.

### Interaction and continuation requirements

Generate a business-specific interaction and continuation section in the parent
body, in the skill's language. Specify where THIS business pauses, what it asks,
and which evidence allows it to continue; do not merely copy generic runtime
documentation or impose an OCR/procurement workflow on unrelated scenarios.
Preserve the child's own Quality Gates and output validation.

- Define missing-input and required-consent conditions. At an interaction point,
  instruct the Agent to return a short single-line `[NEEDS_INFO]: ...` question
  and end the current invocation. Do not perform actions that depend on the
  answer before it arrives. The public response uses `needs_input`; this is not
  a separate paused-state protocol.
- Make confirmation questions self-contained: identify the case or target, the
  work already completed, and the specific operation awaiting approval. Do not
  ask only "Continue?" or claim work completed without evidence.
- Before continuing, reconcile the original request, earlier responses, pending
  question and workspace evidence against the child contracts. Reuse valid
  completed results; do not unconditionally restart from the first action.
  File existence alone is not proof of success.
- Match the user's answer to the pending question for the same case and specific
  operation. Continue that operation only on explicit approval. On refusal, stop
  and summarize completed work. For ambiguous or conditional answers, an unclear
  confirmation target, or a changed operation scope, ask again. Never reuse
  another case's consent.
- Distinguish waiting for input from uncertain execution: a publication timeout,
  partial write or interruption does not mean the operation never ran. Check
  available local and remote evidence using the child's published verification
  procedure before deciding what to do. Renewed approval alone must not trigger
  resubmission. Do not invent a manifest, verification API or tool.
- If conversation or artifact evidence is insufficient to establish progress or
  authorization, state the uncertainty and ask for the necessary clarification
  or decision; do not pretend recovery is established.

The shared generation rules define these principles; the parent body supplies
business-specific stop points, questions and continuation criteria. Child
contracts remain authoritative for inputs, success evidence, errors and checks
for uncertain outcomes. The Host relays the user's answer and resends the same
`session_id` and `scenario` on every continuation call; it does not chain children.

This is Agent-driven continuation from saved conversation and workspace evidence,
not recovery from a fixed step ID. Do not generate resume tokens, stage-state
JSON, workflow YAML, custom persistence, a new continue tool, operation IDs or
confirmation tokens. These are Agent instructions, not programmatically enforced
consent gates, no-rerun guarantees or exactly-once execution. Summarize only
evidenced results and actual artifact links.

### TEST and deployment boundary

The runner always uses `route_only`, never real execution. EAA L1 checks static
contracts; L2 checks that the named EAA scenario resolves (the parent appears in
`skills_referenced`). Host parent-absence and child-reachability expectations do
not apply. EAA L3 child execution is explicitly unverified, not a pass.

A passing resolution does not prove child execution, authorization for another
user, adherence to consent, publication, or exactly-once behavior. Host selection
of the scenario is still not measured.

EAA execution requires Mode B and interactive delegated/OBO identity. App-only
Routine and static fallback are unsupported. Never substitute Managed Identity
for the user or for approval. Actual multi-turn execution remains deployment
acceptance, not an automatic Generator test.
