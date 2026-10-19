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
  the mismatch and ask the author to change the scope or select Host using the UI.
  Do not invent Host tasks just to satisfy a checkpoint, and do not change owner.
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

Describe the actual business behavior in prose, for example:

1. Ask for a missing case identifier; never guess.
2. Read the selected child contracts, perform OCR, and analyze only after success.
3. When analysis is ready, return `needs_input` asking whether to generate and
   publish the deliverables.
4. Continue only on explicit approval. Stop on refusal; clarify ambiguous answers.
5. If publication has an unknown outcome, verify remotely before considering a retry.
6. Summarize only evidenced results and actual artifact links.

These numbered sentences are instructions, not a workflow DSL or programmatic
guarantee. Preserve the child's own Quality Gates and output validation.
Continuation uses the same `session_id`, `scenario`, and `needs_input`; no new
continue tool, operation ID, or confirmation token is introduced.

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
