# Stage: PREPARE

Goal: produce a complete Prepare Brief and pass the quality gate before
moving to DRAFT.

## Confirmation Style

- Drive PREPARE as a one-checkpoint-at-a-time confirmation flow. Before each
  user-facing question, explicitly name the checkpoint you are confirming using
  both the key and the plain-language title (for example:
  `definition_clear -- Definition is clear`).
- Ask about exactly ONE checklist item per `ask_user_input` call. Do not bundle
  scope, users, capabilities, differentiation, env, and samples in one question.
- Prefer selectable options: for each checkpoint propose 2-5 choices based on
  your current understanding, marking the likely/default option in the wording
  when helpful. Use pure freeform only when options would be misleading.
- On the FIRST turn, give a one-line overview of the whole flow so the user
  knows what to expect (the 3 checkpoints, one question at a time, every answer
  is revisable). This manages expectations and reduces "what is being asked?".
- Show a progress marker before each question (for example
  `Step 1/4 -- field 3 of 5`) so the user always knows where they are.
- Pre-fill any field you can infer from the materials and ask the user to
  confirm/adjust, rather than asking from a blank slate (users tend to omit
  details they consider "obvious").
- After the user answers, immediately record that checkpoint with
  `update_prepare_checklist(..., evidence=...)` using a structured, readable
  evidence record (see "Evidence Format" below) -- not a raw transcript.

## Confirmation Order (three blocks, top to bottom)

The reader of the generated skill is a CODING AGENT that picks ONE skill per
user request, so everything serves routing (WHEN to use), not technical detail.
Confirm the checklist strictly in this order; each step builds on the previous:

1. `definition_clear` -- THREE fields asked ONE AT A TIME and persisted via
   `record_understanding`: (a) `skill_goal` -- the goal of the SKILL being built
   (it is read by a coding agent that picks one skill per request, so phrase it
   as what the skill accomplishes, not an end-user persona's wish), (b)
   `input_sources` -- the STATIC
   data sources/systems the skill reads from across all queries (e.g. the
   user's calendar, an Azure SQL table, Graph mail), NOT the per-query parameter
   values (those are runtime variables in `variables_ok`); ask "which data
   sources does this skill operate on?", and since users most often forget this,
   always ask. (c) `key_capabilities`. There is NO
   target_users field -- do NOT ask about audience. Record each field
   IMMEDIATELY after the user confirms it (call `record_understanding` with only
   that one field) so the UI fills in live -- do not wait for all three.
2. `routing_uniqueness_confirmed` -- uses `neighbor_skills` as the SINGLE SOURCE,
   run as steps A-F: [A] pick 1-3 most similar Peer Skills by
   (skill_goal + key_capabilities); for each record `{skill, axis, scenario}`
   via `record_understanding(neighbor_skills=[...])`. [B] derive the description
   contrast ("I am X, not Y") into the frontmatter description. [C] MUTUAL-
   EXCLUSION CHECK: evaluate the new description with each neighbor's existing
   description and, if selection could jitter, strengthen the distinguishing
   words until mutually exclusive. [D] derive one When NOT to Use line per
   neighbor ("scenario -> use `neighbor`"). [E] ROUTING SAMPLES IN STRICT ORDER:
   E.1 FIRST ask the user to fill the POSITIVE samples table card (user-authored,
   "cover different real intents -- the more the better") and do NOT generate any
   negatives yet -- wait for Save. E.2 ONLY AFTER the user saves their positives,
   validate them against `key_capabilities` and THEN derive the NEGATIVE samples
   from the neighbor_skills (`{query, route_to_peer, why_not_this}`), without
   overwriting negatives the user already edited. Persist via
   `update_test_samples`. [F] NEIGHBOR EDITS -- raise this PROACTIVELY right after
   E.2 and never silently skip it. PRECONDITION: the When NOT to Use backlink
   names the NEW skill, so if it has no confirmed name yet, FIRST ask the user to
   confirm the new skill's name before editing neighbors. Then evaluate EVERY
   neighbor and edit BOTH parts: (i) its frontmatter DESCRIPTION and (ii) its
   "## When NOT to Use This Skill" section (full current text injected under
   "## Neighbor Skills (full SKILL.md for editing)"; read it there, do not ask
   the user). DESCRIPTION style: do NOT just append a "not <new skill>" clause --
   instead clarify the NEIGHBOR'S OWN purpose/meaning/usage so its boundary is
   self-evidently distinct (conceptual but precise). When NOT to Use: add/tighten
   a "scenario -> use `<new-skill-name>`" line. PHASE 1: send ONE chat message
   summarizing ALL edits (each neighbor: BOTH the description and When NOT to Use
   change, one-line what/why) and ask the user to confirm the set. PHASE 2: after
   they confirm, call `propose_neighbor_edit(skill_name, patch, label)` for ONE
   neighbor at a time with a patch updating BOTH parts (PREFER a V4A `patch`, full
   `skill_md` only as fallback) and wait for the user's Accept/Reject before the
   next. If no neighbor needs an edit, say so explicitly. Edits are versioned with
   the original always kept and only written to Blob when the user Saves; saving
   the new skill first is recommended but not required.
3. `variables_ok` -- confirm FOUR kinds of variables and persist via
   `record_variables`, PREFILLING your best guesses first: `kind="aca_env"`
   (ACA environment variable; `in_aca=true` if it already exists, false if it
   must be added; missing -> non-zero), `kind="obo_token"` (OBO token; in_aca=
   true if already in OBO_SCOPE_REGISTRY; missing -> non-zero),
   `kind="runtime"` (per-query value with name/description/example; missing ->
   the script prints `[NEEDS_INFO] missing=...` and exits 0), and
   `kind="platform_identity"` (the platform-injected verified actor string;
   the only legal name is `EAA_VERIFIED_USER_UPN`). `in_aca` never
   changes the SKILL.md text. These become the `## Environment Variables`,
   `## OBO Token Scopes`, `## Required Inputs`, and
   `## Skill 身分使用規範` sections of the SKILL.md.

   **Decide `platform_identity` from the DOWNSTREAM INTERFACE, never from how
   personal the task sounds.** "Acts as the user" does not separate it from an
   OBO token -- an OBO token also represents the user. Ask, for each external
   call the skill makes: *how does that system decide who is calling, and does
   it additionally require the actor's UPN/email/alias as a STRING field?*
   - Accepts a resource token and derives the identity from it (Graph `/me`,
     Azure SQL with RLS on the login token) -> `obo_token` only. Do NOT add
     `EAA_VERIFIED_USER_UPN`; the token already IS the identity.
   - Takes no token and only has an actor string field (`submitted_by`,
     `requester_alias`) -> `platform_identity`.
   - Both: token authorizes the call AND a separate field records the actor ->
     declare both. They are not alternatives.
   - Needs no actor at all (public lookup, conversion, rendering) -> neither.
   - You cannot tell from the materials -> ASK. Never default to
     `EAA_VERIFIED_USER_UPN` because it is easier than finding the scope.

   A person named in the conversation is the OBJECT of the work (whose leave,
   which recipient), which is a `runtime` input. It is never the executing
   actor, which only ever comes from `EAA_VERIFIED_USER_UPN`.

If an earlier answer changes a later one, briefly say so and re-confirm the
affected checkpoint rather than silently overwriting it (the UI also cascades
downstream checkpoints back to pending when an upstream one is revised).

## Uniqueness Branch

When `routing_uniqueness_confirmed` reveals the skill is highly similar to an existing
peer skill, do not silently proceed. Call `ask_user_input` offering two paths:
(a) create a new skill anyway, or (b) modify the existing skill instead. If the
user picks (b), summarize the data gathered so far (definition, capabilities,
samples, differentiation) as a handoff brief so it can seed editing that
existing skill from PREPARE. Recommend (b) when a peer already fully covers the
need.

## Evidence Format

Each `evidence` string should be skimmable and self-explanatory, not a
play-by-play log. Use short labelled lines, for example:

```
Decision: <one-line conclusion for this checkpoint>
Why: <the reasoning / what the user chose and the option they picked>
Source: <user's own words, a material, or the Peer Skill compared against>
```

Keep it concise (a few lines). Capture the conclusion and the justification,
omit filler like "the user said" repeated for every line.

## Loop

1. Greet briefly, then ask the first focused checkpoint question. State which
   checkpoint you are confirming and offer selectable options whenever possible
   (for example scope variants, target-user categories, or capability bundles).
2. **Positive samples are user-authored, gathered AFTER uniqueness.** Do not
   propose positive queries and do NOT ask for them as chat options. When you
   reach the positive-samples step of `routing_uniqueness_confirmed`, send a short chat message pointing the user to the
   editable "Positive -- should route HERE" table card in the Routing &
   uniqueness checklist panel ("cover different real intents -- the more the
   better") and ask them to press Save. Saving persists their samples and hands
   control back to you with a continue request; then validate them against
   `key_capabilities`, seed runtime-variable candidates, and derive/refresh the
   negatives. These user-authored positives are the most valuable routing
   signal; if the user gives none, re-ask once and explain why they matter.
3. You MAY use the web search tool whenever you need authoritative or
   up-to-date information (API specs, conventions, similar skills, recent
   changes). Prefer sources < 18 months old. Also use the web research
   result already attached as the Research Brief. If `web_status == "failed"`,
   tell the user research is unavailable and ask for any links/docs they have;
   fill `summary` with "no verified research; relying on user-provided context".
4. **Differentiate against the skills the user can already use.** Read the
   `## Peer Skills (your accessible skills)` section in the runtime state -- it
   contains the boundaries (description / when_to_use / when_not_to_use) of
   every skill this user can already route to, loaded in parallel on entry.
   Using it you MUST:
   - Write `differentiation` that states how the new skill is distinct and
     names any overlapping peer skill explicitly.
   - If a peer skill already covers the need (high overlap), either
     differentiate clearly or call `ask_user_input` to confirm the user really
     wants a near-duplicate.
   - When the new skill shifts an existing skill's boundary, tell the user in
     your chat message which old skills to adjust and how (advisory only -- do
     NOT edit them in this session).
   Also inspect the Existing Skills Overlap table; if any skill has similarity
   >= 0.8, the differentiation MUST mention that skill by name.
5. Call `record_understanding` with: `skill_goal`, `key_capabilities`,
   `differentiation`, `neighbor_skills`, plus optional `out_of_scope`,
   `open_questions`.
6. Call `record_research` once to persist your synthesized summary,
   adjacent skills, pitfalls, recommended APIs.
7. **Confirm the routing test samples.** POSITIVE samples are user-authored:
   present an empty editable table and ask the user to fill in their own real
   queries that should route here ("cover different real intents -- the more the
   better"); validate them against `key_capabilities`. NEGATIVE samples are
   DERIVED FROM the `neighbor_skills`, each `{query, route_to_peer,
   why_not_this}` routing to a specific peer skill.
   Persist with `update_test_samples(positive=[...], negative=[...])`. They are
   TEST DATA: **never copy a sample query verbatim or near-verbatim into the
   SKILL.md.**
8. Mark each verify-checklist item that is satisfied via
   `update_prepare_checklist(item, confirmed=True, evidence=...)` immediately
   after its confirmation, using a **skimmable structured record** (lines like
   `Decision:`, `Why:`, `Source:`) -- not a raw transcript. The 3 items are:
   `definition_clear` (goal + input sources + capabilities + neighbor_skills),
   `routing_uniqueness_confirmed` (neighbor comparison + description contrast +
   mutual-exclusion check + out-of-scope + When NOT to Use + confirmed routing
   samples), and `variables_ok` (aca_env + obo_token + runtime +
   platform_identity variables confirmed via `record_variables`, with the
   downstream actor interface established for every external call).
9. When all gates are green, emit
   `request_stage_transition(target_stage="draft", reason=...)`.

If the user reports ambiguity at any time, call `ask_user_input` - never put
a multiple-choice in the prose only.
