# Stage: TEST

Goal: run the selection tests and capture a reflection across two axes.

## What a test run actually is

Selection tests are sent with `mode="route_only"`. The runtime routes the query
and prepares the code it would have run, then stops. **Nothing is executed and
nothing is written.** That changes what the results can tell you:

- There is no execution, so there is no observed behavior to judge. The usage
  axis is a **static review of code**, not a report on what happened. Never
  write that the skill "worked", "returned", or "handled" anything.
- The audit instruction is not appended in this mode, so **no response contains
  a `Skill used:` or a `Final answer:` line**. Do not look for them. Prose is
  not routing evidence either -- a response that merely mentions a skill name
  has not routed to it.
- `[NEEDS_INFO]` cannot appear as a run result. Its guard sits on the first line
  of `main()`, which never runs here, so its absence proves nothing about the
  payload contract.
- The code sits under `### Prepared code` inside `## Latest Test Run`, and each
  script is there **in full**, followed by the static findings already computed
  against it. Only samples that routed to this skill appear; a sample that
  routed elsewhere has no code shown because that code implements another skill.
  If a script was omitted for context budget the section says so by name; do not
  review that one from memory.

## The two axes

1. **Discoverability** -- did the router select the correct skill? The router
   reads the frontmatter `description` ONLY, so a miss here is a **description**
   problem.
2. **Usage correctness** -- is the code the runtime prepared actually right? A
   failure here is a **content** problem in the body sections.

## Rules

- The positive/negative samples were already confirmed during PREPARE and live
  on the session (`test_samples`). Routing samples can change in three ways:
  - **User edits them and stays on the current skill** -- a silent user-driven
    UI action with no message to you. Just use the new values on the next
    `request_test_run`; do NOT touch the requirements (Prepare Brief) or the
    verify checklist.
  - **User edits them and returns to PREPARE** -- you WILL get a message saying
    so. Rethink how the new samples affect the Prepare Brief (scope,
    capabilities, differentiation) and the verify checklist, and update them
    with the user.
  - **You want to change the samples** -- first `ask_user_input` whether to
    simply update the samples or return to PREPARE to rethink requirements. If
    the change conflicts with the established brief and needs re-confirmation,
    recommend returning to PREPARE. Then persist via
    `update_test_samples(positive=[...], negative=[...])` before running.
- Call `request_test_run` with those confirmed positive/negative samples. Never
  silently invent fresh samples at test time.
- After a test run completes, **do NOT emit `propose_patch` in the same turn**.
  First analyze BOTH axes, then emit `record_reflection`:
  - **Routing:** judge each sample by the runtime's `skills_referenced` list and
    by nothing else. The router reads the frontmatter `description` ONLY, so
    plan a `description` fix (do not change tags for routing). If positive
    samples miss, broaden or clarify the description with the missing intent
    families, synonyms, product/data-source names, object types, and
    Chinese/English trigger phrases. If negative samples incorrectly select this
    skill, narrow the description and state explicit exclusions/boundaries
    inside it so adjacent skills keep their territory. If both happen, separate
    recall fixes (positive misses) from precision fixes (negative false
    positives) and keep a precise 2-3 sentence description rather than keyword
    stuffing. Never copy failed sample queries verbatim into the skill text.
  - **Usage:** for each script under `### Prepared code`, carry over every
    static finding printed with it, then apply the checks below.
  - `record_reflection` fields: `what_went_wrong`, `what_to_change`,
    `confidence_delta` (optional float -1..1), `raw`.
  - **`what_to_change` is the fix list the whole REFINE round runs on**, so make
    every entry ONE atomic fix that a single `propose_patch` can close. Never
    bundle two fixes into one string and never leave a finding out of the array
    because it "belongs to the same area".
- When you ask the user to accept the correction direction, the `ask_user_input`
  options MUST let them choose the batch in one click -- the user cannot be
  expected to know they may ask for several patches in a row. With N > 1 fix
  items, offer options that spell out the count, e.g. 「一次修完全部 N 項（建議）」
  (recommended default), 「只修第 1 項，其餘先擱置」, 「先不修，維持現狀」. State in
  the surrounding text that fixing all N takes N patch cards to Accept and no
  test run in between.
- `record_reflection` automatically transitions you back to REFINE, where your
  `what_to_change` array appears as the `## Open Fix List`. Work it top-down:
  one narrow patch per item (metadata for discoverability, content for usage)
  with `addresses` naming the item it closes, proposing the next one as soon as
  the previous is accepted, and NO test run until the list is empty. Wait for
  the user's review decision instead when human judgment is required.
- For test-run failures unrelated to the skill content (env, transport, or a
  batch aborted because the runtime did not echo the requested `mode`), surface
  the error and ask the user how to proceed. Those are deployment findings and
  no patch can fix them.

## Usage axis: what to check in the code

Every script under `### Prepared code` is followed by its own **Static findings**
list. The same content lint that runs over the SKILL.md body now also runs over
the script the runtime wrote, and it already decided everything that can be
decided from code SHAPE: declared vs. read variable names (A2), `[NEEDS_INFO]`
codes and their documentation (A4, A11), the entry point and the caller's
channel (A9, A10), deployment configuration misfiled as a caller input (D1-D3),
the identity read shape and its recovery path (I1-I3), leaking the verified
actor (I4), and an external call whose result is never inspected (A12).

**Do not re-derive those.** Re-reading the two variable lists and announcing a
diff the tool already printed costs a turn and produces a second opinion that can
only disagree with the first. Copy each printed finding into `what_to_change` as
its own atomic entry, then spend your reading on the four checks below -- the
ones that depend on what the code MEANS and that no static rule can settle.

When the same rule fires on both the body lint and the prepared-code findings,
the body is the root cause and the place to patch. When it fires only on the
prepared code, the body's prose is what misled the runtime -- the sample code
being correct is not a defense, because the runtime writes its own script from
the prose.

### 1. Every external identifier must trace back to the body

Take each endpoint, SQL object (table, view, procedure, column), payload field,
CLI subcommand and CLI flag the code uses, and find it in the SKILL.md body. A
name that appears only in the prepared code means the body does not pin it down,
so the next run is free to pick a different one. State which of the two you are
looking at: the body is out of date, or the body never specified it at all.

Do not claim the model "invented" a name -- you cannot tell that from here, and
the fix is the same either way: write the name into the body.

### 2. Security shapes the body already forbids

Skip this check entirely unless the body mentions RLS, row-level security, OBO,
or a required end-user connection identity. With none of those present there is
no declared rule to break, and looking for one produces speculation.

- An ownership filter stacked on top of server-side row security (for example a
  `WHERE upn = ?` in a skill whose body says RLS is active). The redundant
  filter hides an RLS failure instead of surfacing it.
- A fallback to a managed identity or service principal in a skill whose body
  says the connection identity must be the end user.
- A guard the body declares -- order of security gates, error taxonomy,
  validation of caller-supplied data -- that the code drops.

### 3. Identity as meaning: R3 and the reasoning half of R4

The lint covers the shape of the identity read (R1, R2, R4's leak). These two
depend on what the code means, so they are yours -- and they are the highest
severity finding available here.

- **R3** -- read the sample query, then read the code: if the query names a
  person and that name (or anything derived from it) ends up in the field that
  identifies the EXECUTOR, the skill has let conversation content become
  identity. The named person may only appear as the OBJECT of the operation,
  carried by a `runtime` input.
- **R4 (reasoning)** -- the code nowhere concludes that the caller is authorized
  merely because the skill loaded, or because a value was present.

Report either as a USAGE (content) finding and fix it in REFINE.

### 4. What the code claims happened

A12 catches the mechanical half: a call whose result is never looked at. The
other half is yours -- read every string the code prints on its way out and ask
whether the code KNOWS it is true. A line like "已成功送出" printed from a branch
that only knows the call returned is a claim about a system nobody queried. The
host shows a completed response to the user verbatim, so an unverified claim
reaches them as fact, and a create/submit the user believes failed gets sent
twice.

The fix is almost never a bigger success message. It is to print what the tool
actually returned, or to check the outcome before saying anything about it.

### Classifying what you find

- **Objective error** you can fix by editing SKILL.md content (wrong endpoint,
  wrong parameter, missing required step, any printed static finding) -> say so
  and plan a content patch.
- **Subjective / needs domain judgment** you cannot verify from the materials ->
  do NOT silently "fix" it. Tell the user plainly that this case needs **human
  review**, state exactly what to check, and ask them to confirm the expected
  behavior.
- **Merely different** -- the model may legitimately write code that is not a
  copy of the body's sample. Only report a difference when you can name the rule
  it breaks. Restyling, renamed locals and a different helper split are not
  findings.