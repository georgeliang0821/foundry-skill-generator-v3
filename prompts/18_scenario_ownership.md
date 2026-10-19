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

The Child Skills block contains complete current capability SKILL.md files;
the Dependency Skills index lists their published `##` headings. Read the real
input contracts, `[NEEDS_INFO] missing=` codes and business error keys. Do not
copy child field tables or payload examples into the parent.

For `propose_child_edit`, copy unchanged V4A CONTEXT lines verbatim from between
the `<<<BEGIN ...>>>` / `<<<END ...>>>` markers. The text is literal file content,
not fenced, because the files contain their own code blocks.

### Host owner

The Host reads the body and performs Host-only pre-work, retrieves child
contracts, assembles parameters and delegates. Preserve the existing Host format
spec, section pointers and return-branch requirements.

Use `fetch_skill(skill_name=..., sections="A, B")` once per child and collect the
needed sections in that call. State that children absent from `list_skills` may
still be fetched and that their names are fetch arguments, not task text for
`run_coding_workflow`. Keep the complete dependency whitelist. Host capabilities
and their reasons remain part of the Host PREPARE review.
Use the exact published headings for pointers and correct return branches.
The `sections` argument is a comma-separated string, not a list. Decorative
emoji and punctuation may be omitted, but no word may be invented.
