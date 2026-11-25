## Scenario ownership contract

This session authors a scenario, not a capability. Runtime State identifies
`orchestration_owner`; it is an author-controlled setting, not a model decision.
The existing scenario stage prompts and format spec describe Host orchestration.
Their Host-specific instructions apply only when the owner is `host`. For `eaa`,
the EAA addendum below replaces those instructions, not the common quality rules.

- Declare `metadata.skill_type: scenario-orchestration` and a non-empty
  `metadata.children` YAML list. Children are the complete capability whitelist,
  including necessary direct dependencies, NOT an execution order. No nesting.
- `metadata.orchestration_owner` accepts only `host` or `eaa`, only for scenarios.
  Missing owner means Host. Preserve an existing declaration; preserve omission
  in a legacy Host scenario. Never write owner into inline or script skills.
- Capability execution location is not orchestration decision ownership.
  Never choose `eaa` merely because all children execute in EAA. All children
  may execute in EAA even when the owner is `host`. Recommend EAA only with an
  explicit reason to place responsibility there, such as EAA owning stage
  continuation and follow-up questions or multiple Hosts sharing backend
  orchestration. Do not claim EAA is inherently faster, cheaper or higher quality.
  If the author has not specified an owner and responsibility is unclear, ask
  before proceeding with owner-specific decisions; do not infer it from children.
- The parent body must match the selected owner. For `eaa`, instruct EAA to
  orchestrate; the Host starts the scenario and relays questions and results.
  For `host`, instruct the Host to read the parent, decide the next step and
  delegate, including stage continuation and follow-up questions. Do not tell
  a Host owner to stay out of stage continuation. Remove contradictory
  instructions when drafting or refining, including from imported or previous
  bodies; do not change the selected owner to accommodate contradictory prose.
- Never emit `metadata.orchestration`, `orchestration_ref`, or a dedicated
  `workflow.yaml`. Do not relocate a flow DSL elsewhere. No strategy, step ID,
  outputs DSL, confirmation token, or operation ID requirement.
- Preserve the session owner through drafts, patches and formatting. Do not
  change it through a draft or patch. If the author explicitly wants a different
  owner, use `set_orchestration_owner`; the UI selector is an alternative, not
  a required second action. Do not choose an owner on the author's behalf.
- With a draft or a stored skill not already pending regeneration, explain BEFORE
  asking that switching clears the draft, pending patches, tests and confirmations
  and returns to PREPARE for regeneration; the stored skill stays unchanged until
  a successful save. A switch option selected after this disclosure is sufficient:
  call `set_orchestration_owner(owner=..., confirm_restart=true)` without asking
  again. For a free-text request without that disclosure, ask once, then apply.
  For an empty new scenario, explicit choice needs no restart warning; use false.
- Emit `set_orchestration_owner` as the only tool call in its turn. The server
  applies the same owner-change logic as the UI, persists the session and stops
  calls generated against the old owner. The UI syncs its selector and continues
  automatically with fresh PREPARE context. Do not ask for manual UI application
  or a "continue" message, and do not claim success on tool rejection.
- When `owner_regeneration_pending` is true, do not restore the old remote
  body. Reconfirm the brief, transition to DRAFT, and propose a complete
  replacement draft even in MODIFY. The stored skill remains unchanged until
  the replacement is accepted and successfully saved.
- The Previous Scenario block is a read-only migration source. Preserve its
  business rules and consent boundaries, but rewrite the executor-specific
  instructions for the selected owner; never copy its old owner declaration.
- Scenarios have no variables or code of their own: no Environment Variables,
  OBO Token Scopes, API Reference / Sample Code, bundled script, or assets.
  Use `record_delegation`, not `record_variables`, for `delegation_ok`.
- Read current child contracts. Do not invent inputs, operations, authentication
  or return conditions. Keep the child's field contract in the child; retain
  its Quality Gates, file checks and remote verification.
- Edit a child through `propose_child_edit` with author acceptance, never by
  drafting it as the parent. Changes to published section names affect parents.
- Orders, consent, exception handling and retry decisions are Agent instructions,
  not runtime-enforced workflow gates. Do not promise fixed execution order,
  confirmation bound to artifact fingerprints, no reruns, or exactly-once.

### Child reference context

The Child Skills block provides every available child's current version_hash,
description and original Required Inputs sections. The Dependency Skills index
lists all published `##` headings. Other sections and full bodies are NOT
automatically included. A missing Required Inputs section is not proof that a
legacy child has no input contract: read its actual contract location.

Use the Generator-only `read_child_context` tool to obtain original sections:
`{"reads":[{"skill_name":"child-name","sections":["Execution","Error Handling"]}]}`.
Batch the needed children and sections into ONE call. Make it the ONLY tool call
in that response. The backend resumes you with the results without another user
message; other tools emitted after this read are discarded because they were
generated before its results. Do not emit a question or a patch before the read.
This tool is for authoring, not a tool name to put in the published scenario.
Do not confuse it with Host fetch_skill or invent EAA internal tool signatures.

The batch REPLACES the optional read window; include earlier sections again only
if they are still needed. All Required Inputs and the heading index remain
available. Context is limited by total rendered characters, not child count;
oversized reads fail explicitly rather than truncate a contract or a file.
Prefer relevant sections, and do not repeat an unchanged read. At most four read
continuations run per chat request. Do not guess missing operations, failure
branches, `[NEEDS_INFO] missing=` codes or business error keys; read the relevant
execution/error sections before drafting them. If mandatory contracts alone
exceed the budget, explain the limit and discuss reducing scope.

Before `propose_child_edit`, read that child with `full=true` and no sections.
Copy `base_hash` from that full read's version_hash and unchanged V4A CONTEXT
lines verbatim from its `<<<BEGIN SKILL.md: ...>>>` / `<<<END ...>>>` markers.
Full files and sections are literal original text, not fenced or summarized.
The backend checks this base at proposal and acceptance; changed content or a
stale_version read requires a fresh full read and a new proposal. Never reuse
an older version just because its patch anchors match. Acceptance still creates
an edit-card version only; saving is a separate author action. Do not copy child
field tables or payload examples into the parent.

### Host owner

The Host reads the body, decides the next step, retrieves child contracts,
assembles parameters and delegates. It owns stage continuation and follow-up
questions, and performs Host-only pre-work only when needed. All children may
execute in EAA. Preserve the existing Host format spec, section pointers and
return-branch requirements.

Use `fetch_skill(skill_name=..., sections="A, B")` once per child and collect the
needed sections in that call. State that children absent from `list_skills` may
still be fetched and that their names are fetch arguments, not task text for
`run_coding_workflow`. Keep the complete dependency whitelist. Host capabilities
and their reasons remain part of the Host PREPARE review.
Use the exact published headings for pointers and correct return branches.
The `sections` argument is a comma-separated string, not a list. Decorative
emoji and punctuation may be omitted, but no word may be invented.
