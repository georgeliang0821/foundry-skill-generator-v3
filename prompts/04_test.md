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
  script is there **in full** -- the name comparison below depends on that.
  Only samples that routed to this skill appear; a sample that routed elsewhere
  has no code shown because that code implements another skill. If a script was
  omitted for context budget the section says so by name; do not review that one
  from memory.

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
  - **Usage:** review each script under `### Prepared code` against the checks
    below.
  - `record_reflection` fields: `what_went_wrong`, `what_to_change`,
    `confidence_delta` (optional float -1..1), `raw`.
- `record_reflection` automatically transitions you back to REFINE. In the next
  turn, propose the narrow patch (metadata for discoverability, content for
  usage) or wait for the user's review decision when human judgment is required.
- For test-run failures unrelated to the skill content (env, transport, or a
  batch aborted because the runtime did not echo the requested `mode`), surface
  the error and ask the user how to proceed. Those are deployment findings and
  no patch can fix them.

## Usage axis: what to check in the code

### 1. Declared variable names vs the names the code reads (primary check)

Collect two sets and compare them.

- **Declared** -- every variable name in `## Required Inputs`, and the same for
  `## Environment Variables` and `## OBO Token Scopes`. `EAA_VERIFIED_USER_UPN`
  counts as declared whenever `## Skill 身分使用規範` is present; it is governed
  by check 4 below, not by this diff.
- **Read** -- every name passed to `os.environ[...]`, `os.environ.get(...)` or
  `os.getenv(...)` anywhere in the code, helpers included.

Compare **case-insensitively**, and treat a chain of alternatives as a single
read: `os.environ.get("hr_leave_json") or os.environ.get("HR_LEAVE_JSON")`
satisfies the one declared name `hr_leave_json`. That is correct code -- neither
a duplicate nor a mismatch -- so report nothing for that shape. A declared name
is satisfied when **at least one** read matches it case-insensitively. Report
only:

- a declared name with **no** case-insensitive match among the reads -- the
  caller will set a variable that nothing ever looks at; or
- a read name that matches **no** declared name -- the code depends on something
  the contract never asked the caller to supply.

This is the one check that is strictly better than what an executing test could
do. A `runtime` input declared under one name and read under another prints
`[NEEDS_INFO]` when executed, which is byte-for-byte what a correct skill prints
when the caller simply omits that input; the two cases were indistinguishable.
Here it is a plain text diff.

`skills/hr-leave-system/SKILL.md` is the reference for the correct shape.

Run the same comparison on the sample code inside the SKILL.md body. That needs
no test run at all, and when both disagree with the declarations, the body is
the root cause and the place to patch.

### 2. Names that must trace back to the body

Every endpoint, SQL object (table, view, procedure, column) and payload field in
the code must also appear somewhere in the SKILL.md body. A name that exists
only in the response was either invented or the body is out of date -- say which.

### 3. Security shapes the body already forbids

- An ownership filter stacked on top of server-side row security (for example a
  `WHERE upn = ?` in a skill whose body says RLS is active). The redundant
  filter hides an RLS failure instead of surfacing it.
- A fallback to a managed identity or service principal in a skill whose body
  says the connection identity must be the end user.
- A guard the body declares -- order of security gates, error taxonomy,
  validation of caller-supplied data -- that the code drops.

### 4. The verified actor contract (when the body has `## Skill 身分使用規範`)

Check the prepared code against the four rules. This is a static read; nothing
executed, so the only evidence is the code shape.

- **R1** -- the actor comes from `os.environ["EAA_VERIFIED_USER_UPN"]` by index.
  Any `.get()`, `os.getenv`, `or "..."` fallback, or a default parameter carrying
  the identity is a violation: it converts "never verified" into a value that is
  then used as the authorization subject.
- **R2** -- the read is not inside a `try` whose handler recovers, and the
  absence path is a non-zero exit, never a `[NEEDS_INFO]` line and never a
  question to the user. The caller cannot supply this variable, so asking for it
  is asking to be lied to.
- **R3** -- this is the one a routing test is most likely to expose. Read the
  sample query, then read the code: if the query names a person and that name
  (or anything derived from it) ends up in the field that identifies the
  EXECUTOR, the skill has let conversation content become identity. The named
  person may only appear as the OBJECT of the operation, carried by a `runtime`
  input.
- **R4** -- the verified value is not passed to `print`, a logger, or the
  success-path summary, and the code nowhere reasons that the caller is
  authorized merely because the skill loaded.

R3 and the reasoning half of R4 cannot be caught by the automatic lint -- they
depend on what the code means, not on its shape -- so they are yours to check
here. Report a violation as a USAGE (content) finding and fix it in REFINE.

### 5. Deployment configuration misfiled as a caller input

Every name declared in `## Environment Variables` or `## OBO Token Scopes` must
appear in the prepared code as `os.environ["NAME"]` and nowhere else. Report a
violation when such a name is:

- read with `os.environ.get(...)`, `os.getenv(...)`, an `or "..."` fallback, or
  a default parameter; or
- listed in a `[NEEDS_INFO] missing=` line, or otherwise turned into a question
  to the caller or the user; or
- read inside a `try` whose handler recovers, or replaced by a service
  principal / managed identity / any substitute credential.

All three make a broken deployment or a broken OBO chain exit 0 and look like a
missing caller input, which the host will retry forever against a deployment
nobody was told is misconfigured. `[NEEDS_INFO]` is only for values a caller can
actually supply.

This is a USAGE (content) finding, and the fix is in the BODY, not the sample
code: check that `## 部署設定使用規範` exists and carries the verbatim `D1`-`D3`
rules. The sample code being correct is not enough -- the runtime writes its own
script from the prose, so a missing or paraphrased rule block is the root cause.

### Classifying what you find

- **Objective error** you can fix by editing SKILL.md content (wrong endpoint,
  wrong parameter, missing required step, a mismatch from check 1) -> say so and
  plan a content patch.
- **Subjective / needs domain judgment** you cannot verify from the materials ->
  do NOT silently "fix" it. Tell the user plainly that this case needs **human
  review**, state exactly what to check, and ask them to confirm the expected
  behavior.
- **Merely different** -- the model may legitimately write code that is not a
  copy of the body's sample. Only report a difference when you can name the rule
  it breaks. Restyling, renamed locals and a different helper split are not
  findings.