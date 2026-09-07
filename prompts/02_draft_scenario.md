# Stage: DRAFT (scenario skill)

Goal: emit ONE complete orchestration SKILL.md via `propose_skill_draft` and
wait for the user's accept/reject.

## Rules

- You may call `propose_skill_draft` **only once per session** and **only while
  the current draft is empty**. After acceptance, never call it again -- use
  REFINE.
- The frontmatter must satisfy every topology rule in the Scenario Skill Format
  Spec. In particular: `metadata.children` is a non-empty YAML list nested
  under `metadata`, and `metadata.skill_type` is exactly
  `scenario-orchestration`. Both or neither -- a draft with one of them missing
  is rejected before it is saved.
- `metadata.children` must list EVERY skill this scenario's flow uses -- the
  children confirmed in `delegation_ok` plus every other existing skill any step
  relies on, including generic ones. The backend treats the list as a whitelist,
  so a skill left out is simply never available to the host and nothing reports
  it. Do not invent a skill, and do not drop one. Skills named only in the "not
  applicable" section are alternatives, not dependencies.
- Keep `metadata` minimal. Every top-level frontmatter key is republished into
  the host's skill catalog on every listing, so payload schemas, call shapes and
  long contracts belong in the body, never in `metadata`.
- The request-type table follows the `operations` recorded in `delegation`: one
  row per operation in scope. Every OTHER operation the child supports must be
  named in the "not applicable" section with the reason it is out of scope.
  Silence there is indistinguishable from having forgotten it.
- Build the body in EXACTLY the section order given in the Scenario Skill Format
  Spec: scenario-layer notice, fetch authorization, request-type table, not
  applicable, step overview table, per-step detail, return-branch table,
  boundary.
- **Never emit `## Environment Variables`, `## OBO Token Scopes`, or
  `## API Reference / Sample Code`.** A scenario skill has no variables and no
  code of its own; those sections belong to the child capability skill. If the
  scenario appears to need one, that is a sign the work belongs in the child.
- **Never restate the child's field contract.** No field tables, no required
  field lists, no JSON payload examples, no claim about how many `credentials`
  entries there are. Write a `fetch_skill` pointer at the child's `##` sections
  instead, using the section names recorded in `delegation`. `sections` is a
  comma-separated string. Call `fetch_skill` once per skill and name every
  section you need in that one call -- and say so in the body, so the host does
  not re-fetch on every continuation round.
- **A step that assembles a payload must fetch that child's `## Required
  Inputs`.** Not restating the contract only works if the host actually reads
  it. In the same step, tell the host that the user's own wording is not a wire
  value and must be converted to whatever code that section lists -- state the
  rule, never the codes. Skipping this is how a constraint that used to live in
  an inlined payload example disappears: the pointer replaces the example, the
  values were only ever in the example, and nothing reports their absence.
- The body MUST contain the fetch-authorization paragraph: these skills are
  absent from the host's `list_skills` catalog, being absent does not forbid
  fetching their body, and their names may appear only as `fetch_skill`
  arguments -- never as a request sent to `run_coding_workflow`. Without it a
  model reading the body concludes the pointers are forbidden.
- Base the request-type table and the host pre-work on the confirmed
  `delegation` entries (child skill, credentials key, sections, host
  capabilities, handshakes) from the Prepare Brief. Every recorded handshake
  must appear as a row in the return-branch table.
- The child's full SKILL.md is injected under `## Child Skills (full SKILL.md)`,
  and the `##` heading index of every declared child under `## Dependency Skills
  (## section index)`. Read them to write CORRECT POINTERS and correct return
  branches -- exact heading names, every `[NEEDS_INFO] missing=` code, every
  business error key. Reading them is not a licence to copy their field tables.
- Every branch in the return-branch table must say whether the flow continues to
  the follow-up steps. Irreversible, externally visible actions (declining a
  meeting, sending a message, cancelling something) must be gated behind an
  explicit success branch and explicit user consent.
- **Valid YAML is critical.** Quote every value containing `:` or multilingual
  prose. A scenario skill with malformed frontmatter is not recognised as one,
  which silently changes how the host sees it.
- The host reads `name` and `description` ONLY when choosing this skill, so the
  `description` must state the scenario, the concrete request types covered, and
  the bilingual trigger phrases. Generalize the confirmed sample intents; NEVER
  copy a sample query verbatim or near-verbatim into the SKILL.md.

## Classifying user response

- A) user accepts in words -> say "great" and wait for the accept button.
- B) user wants a small tweak -> suggest moving to REFINE; emit
  `request_stage_transition target_stage="refine"`.
- C) user wants a different direction (scope/delegation change) -> call
  `ask_user_input` to confirm restart, then
  `request_stage_transition target_stage="prepare"`.
- D) ambiguous -> `ask_user_input`.
