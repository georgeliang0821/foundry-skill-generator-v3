# Writing Best Practices

- Write descriptions for retrieval: intent verbs first, implementation details later.
- Include both what the skill does and when the agent should select it.
- Include Chinese and English trigger phrases when the expected users are bilingual.
- Make When NOT to Use entries discriminative and point to adjacent skills when known.
- Keep environment variable instructions explicit and actionable.
- Preserve user-provided constraints and avoid inventing unsupported capabilities.
- On the success path, make the sample code's final primary output a natural-language summary a general end user can read, not a raw dict/list/JSON dump; use structured/JSON output only when the user explicitly asks for it.
- **The backend sees only `name` and `description` when it picks a skill.** The
  body plays no part in selection -- it is read after the skill is already
  chosen, for comprehension and recovery. Putting routing signal in the body
  does nothing; it belongs in the `description`.

## Error Taxonomy (capability skills)

A capability skill has exactly THREE ways to end. Every failure the code can
reach must be deliberately placed in one of them, because the host reads the
exit code and the output to decide what to do next.

1. **Deployment / configuration is broken** -> let it raise, exit non-zero.
   This is reserved for `aca_env`, `obo_token` and `platform_identity` inputs
   being absent, which is
   why the doctrine reads them as `os.environ["NAME"]` and lets the implicit
   `KeyError` fly. Nothing else belongs here. An explicit `raise` in the sample
   code is almost always a misfiled error from one of the other two categories.
   `EAA_VERIFIED_USER_UPN` is the sharpest case: it looks like something a
   caller could provide, but nothing outside the platform may set it, so its
   absence can never become a `[NEEDS_INFO]` line or a question to the user.

   **This category has to be stated as a literal rule inside the skill file,
   not merely respected by the sample code.** The runtime reads the SKILL.md and
   writes its own script from it, so a sentence like "missing environment
   variables are deployment errors and should cause a non-zero failure" is read
   as advice and routinely downgraded into `os.environ.get(...)` plus a
   `[NEEDS_INFO]` line -- which exits 0 and reports a broken deployment as a
   missing caller input the host will retry forever. That is why `aca_env` and
   `obo_token` get their own `## 部署設定使用規範` section carrying the verbatim
   `D1`-`D3` rules, exactly as `platform_identity` gets `R1`-`R4`.
2. **The caller did not supply what the skill needs** -> print
   `[NEEDS_INFO] missing=CODE` on one line, the human explanation on the next,
   then `raise SystemExit(0)`. This covers a missing `runtime` variable AND a
   missing or malformed field inside one. The caller can fix these and retry,
   so they must not look like a crash.
3. **A business rule rejected the request** -> return a named `error` key to
   `main()`, render it as end-user prose, exit 0. Not found, no entitlement,
   insufficient balance, overlapping record, past date, conflict: the system
   worked correctly and the answer is no.

Two rules that follow from this:

- **Validate every field you read from caller data before you read it.** A
  `KeyError` or `TypeError` on caller-supplied data is a bug, not an error
  path -- it exits non-zero and the host reports a deployment failure for what
  was really a missing field. If an array of objects is supplied, check the
  fields on each item, not just that the array exists.
- **Catch business conditions raised by external systems.** A SQL `THROW` /
  `RAISERROR`, an HTTP 4xx, or a driver exception that represents a business
  condition must be caught and mapped into category 3. Letting it escape turns
  "this employee has no record" into "the skill is broken".

Validation covers the VALUE, not only the presence, of a field:

- **A field with a closed set of legal values must be checked against that set
  before it is used, and an illegal value is category 2**, not category 3. Print
  `[NEEDS_INFO] missing=THAT_FIELD`, name the legal values in the explanation,
  and exit 0 so the caller can convert and retry.
- **An empty result set is only a business condition once the inputs are known
  to be legal.** A query built from an unvalidated value returns no row for two
  very different reasons -- the user genuinely has nothing, or the caller sent
  `福利假` where the column stores `welfare` -- and reporting both as "no
  entitlement" sends whoever debugs it after the data instead of the contract.
  Every closed value set must be listed in `## Required Inputs` as well: the
  check in the code protects the run, the list in the contract is the only way
  the caller learns what to send.

The error taxonomy must be exhaustive in both directions: every `error` key the
code can return needs a human-readable rendering, and every rendering needs a
reachable code path.

## Scenario (Orchestration) Skills

- A scenario skill describes what the HOST agent does. It is never executed by
  the backend, so it has no variables, no OBO scopes and no sample code.
- Keep the two layers apart: anything that reads or writes a system belongs in
  the child capability skill; anything that sequences, branches, or uses a host
  capability belongs in the scenario skill.
- Keep `metadata` small. Every top-level frontmatter key is republished verbatim
  into the host's skill catalog on every listing, so a long contract in
  `metadata` is a cost paid on every request. Prose belongs in the body.
- Write branch tables, not prose, for anything the child can return. The host
  needs to look up one row, not parse a paragraph.
- Gate every irreversible, externally visible action (declining a meeting,
  sending a message, cancelling something) behind an explicit success branch and
  explicit user consent.
- Warn about tools that look interchangeable but are not, and name the
  observable symptom rather than a theory about the cause.
- `metadata.children` is a whitelist of every skill the flow may use, not just
  the ones handed a payload. Omitting one costs the host that capability with no
  error message, so list a skill whenever you are unsure.
- Point at a child's sections; never copy its field table. The copy is the one
  that drifts, and it drifts in only one direction.
- **Pointing at a contract is not the same as having one.** When you replace an
  inlined payload example with a pointer, every constraint the example carried
  implicitly -- the legal values, the format, the unit, the casing, the language
  of a code -- must be present in the child's `## Required Inputs` before the
  example goes away. Nothing at runtime reports a constraint that was dropped in
  the move: the code is still correct, it just receives a value nobody told the
  caller was wrong.
- The host holds the user's own words. Say plainly that they must be converted
  to the child's codes, and that the child's section is where those codes live.
- `is_internal` is a discoverability flag, not a permission. It only hides a
  skill from the host's `list_skills` catalog; the caller still needs a grant or
  `is_public`. Marking a shared skill internal takes it out of everyone's
  catalog, so it belongs only on a capability skill that exists to serve one
  parent.

