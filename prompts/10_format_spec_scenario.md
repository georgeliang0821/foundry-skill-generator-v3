# Scenario Skill Format Spec

A scenario skill is an ORCHESTRATION skill. It is not executed by the backend.
It tells the HOST agent what to do: which of its own capabilities to use first,
how to assemble the payload, which child capability skill to delegate to, and
how to react to each returned branch.

## Topology Rules (hard requirements)

These are formal requirements on the file. Satisfy every one of them; a
violation is rejected at save time.

- **T1** -- The file must begin with a `---` fence on its very first line, with
  no byte order mark and no leading blank line, and close the block with
  another `---` on its own line.
- **T2** -- The frontmatter block must not contain `---` anywhere inside it,
  including inside a quoted value.
- **T3** -- The frontmatter must be a YAML mapping that parses cleanly. Quote
  any value containing `: `.
- **T4** -- `children` must be declared as `metadata.children` (nested under
  `metadata`, never at the top level) and must be a non-empty YAML list of
  non-empty strings. A bare string is not accepted by every parser.

  `metadata.children` is the **complete dependency list of this scenario**, not
  just the capability skills you hand a payload to. The host passes
  `scenario=<this skill's name>` and the backend treats this list as a
  **whitelist**: a skill that is not listed never materializes for the host, no
  matter how generic it is (`html-ppt`, `ms-graph-send-mail`, and so on). The
  consequence of leaving one out is **silent** -- there is no error message and
  no failed step, the capability is simply absent. **When in doubt, list it.**

  Skills named in S3 as routing alternatives are NOT part of this flow and must
  not be listed.
- **T5** -- A capability skill must not declare `children`. Only a scenario
  skill does.
- **T6** -- The frontmatter `name` must be identical to the name the skill is
  stored under.
- **T7** -- Every entry in `metadata.children` must name a skill that exists and
  that the user can access, and no child may declare `children` of its own.
  Depth is pinned at one level; a child that declares its own `children` has
  them silently filtered.
- **T8** -- Keep `metadata` small. Every top-level frontmatter key is
  republished verbatim in the host's skill catalog, so long prose in `metadata`
  is a recurring context cost. Put prose in the body.
- **T9** -- A scenario skill must not contain `## Environment Variables`,
  `## OBO Token Scopes`, or `## API Reference / Sample Code`. Those belong to
  the child capability skill.
- **T10** -- `metadata.skill_type: scenario-orchestration` and a non-empty
  `metadata.children` list imply each other. Declare both or neither.

## Pointer Rules (hard requirements)

A scenario skill does not restate a child's field contract. It names the
child's sections and lets the host retrieve them. These rules are checked at
save time.

- **P1** -- Every `fetch_skill(skill_name=X)` in the body must name a skill that
  is listed in `metadata.children`.
- **P2** -- Every name inside `sections` must resolve to a `##` heading of that
  child. A name that does not resolve is rejected, and the error lists the
  child's real section names -- pointer mistakes fail loudly, so do not guess
  defensively.
- **P3** -- Call `fetch_skill` **once per skill**, listing every section you
  need in that single call. Not one section at Step 1 and another at Step 3, and
  never again on a `needs_input` continuation round. Write this rule into the
  scenario body itself, not only here.
- **P4** -- When the body contains at least one pointer, it must also contain
  the S0 authorization block below.
- **P5** -- Every real skill the body names must appear in `metadata.children`.
  The S3 routing-alternatives section is exempt.
- **P6** -- The body must not restate field names declared in a child's
  `## Required Inputs`.

`sections` is a **comma-separated string**, not a YAML or JSON list -- the tool
parameters can only carry primitives:

```
fetch_skill(skill_name="hr-leave-system", sections="Required Inputs, [NEEDS_INFO] 契約")
```

Omitting `sections` (or passing an empty string) returns the child's full body,
which is always safe but costs context.

## What Stays Here and What Moves to the Child

| Stays in the scenario body | Moves to the child's body |
| --- | --- |
| Image recognition and what to do when the image is unreadable | Field names and types |
| How to design a question round, and what to ask | Which fields are required per operation |
| Judging the output format the user wants | `credentials` key names |
| Branching on the child's result | Code / category lookup tables |
| Continuing a multi-turn exchange | Payload structure and serialization |
| The instruction to convert the user's wording into the child's codes | The legal values, formats and units themselves |

Behavioural rules that are not field contracts stay here too: converting ROC
years to Gregorian, refusing to invent a value that cannot be read clearly,
rules about what to do when the user contradicts themselves.

## Frontmatter

```yaml
---
name: lowercase-kebab-case
description: "2-3 sentence retrieval-oriented description of the SCENARIO"
metadata:
  version: "1.0"
  skill_type: scenario-orchestration
  children:
    - child-capability-skill
---
```

`name` must match `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$` and be at most 64
characters. `metadata.skill_type` is required and must be exactly
`scenario-orchestration`. Keep the rest of `metadata` minimal -- `version` and
optionally `author` / `created_at`. Do not add contracts, payload schemas, or
call shapes to `metadata`; they go in the body.

The `description` is what the host reads when choosing this skill. It must
describe the SCENARIO the user is in, list the concrete request types the skill
covers, and carry both Chinese and English trigger phrases when the users are
bilingual.

## Body Sections (in this order)

Each section is a numbered rule so it can be cited directly in review.

1. **S1 -- Scenario-layer notice** -- a short block stating that this skill is
   not executed by the backend, that it describes what the host must do, and
   that the actual work is delegated to the named child skill. Open it with a
   warning line to that effect.
2. **S0 -- Fetch authorization** -- immediately after S1. The child skills do
   NOT appear in the host's `list_skills` catalog, so a model reading this body
   will otherwise infer "not in the list, therefore forbidden" and never follow
   the pointers. State plainly:

   > The skills below do not appear in your `list_skills` catalog. **Not being
   > in the list does not mean you may not retrieve their body.** Use
   > `fetch_skill` to read the sections named in each step. These names may
   > appear **only** as `fetch_skill` arguments -- never send one as a user
   > request to `run_coding_workflow`.

3. **S2 -- Request-type table** -- one row per request the scenario covers: what
   the user wants, the child `operation` (or equivalent selector), and whether
   host pre-work is required. Readers must be able to tell in one glance which
   requests are single-turn. Every operation the child supports must be
   accounted for: either it has a row here, or S3 says why this scenario does
   not do it. An operation named in neither is a child capability the host can
   never reach through this scenario, and nothing at runtime reports it.
4. **S3 -- Not applicable** -- what this scenario does NOT cover and where to go
   instead. This is the negative routing section; keep every line
   discriminative. Skills named here are alternatives, not dependencies: they
   must NOT be added to `metadata.children`. Name here, with its reason, every
   child operation you deliberately left out of S2.
5. **S4 -- Step overview table** -- the whole flow as a numbered list of steps,
   each labelled with its actor (host or backend) and marked when it needs a
   host capability. This column must agree with the rest of the file: if a
   step's detail section names a host tool or MCP capability, that step's row
   MUST be marked as requiring a host capability, and the S2 pre-work column
   for the requests that reach that step must agree. A step marked "No" that
   later tells the host to call one of its own tools is a contradiction the
   host cannot plan around.
6. **S5 -- Per-step detail** -- one section per step that needs it. Cover:
   - **Host pre-work**: exactly which data the host must fetch itself, and every
     field that must be carried on each item.
   - **Tool-choice warnings**: when two host tools look interchangeable but only
     one produces usable identifiers, say which one and why the other fails.
     Name the observable symptom, not a theory.
   - **Section pointer**: instead of a field table, write all three parts --
     omitting any one of them invites the next maintainer to inline the table
     again:
     1. A sentence stating that the field contract is whatever `<child>`'s body
        says, and that this section does not repeat it.
     2. The `fetch_skill` call itself, with every section name this step needs.
     3. One line per section name saying what that section provides ("the
        per-operation required fields and the `credentials` key names", "every
        `missing=` code and what each one means").

     State explicitly that the payload must not be written into the free-text
     `request` parameter. Do NOT enumerate the child's fields here, do NOT paste
     a JSON payload example, and do NOT claim how many `credentials` entries
     there are -- a hand-copied contract drifts in one direction only.
   - **Value conversion**: any step that assembles a payload must name the
     child's `## Required Inputs` among the sections it fetches, and must state
     that the user's own wording is not a wire value. The user says 「福利假」 or
     "vacation"; the field takes a code, and the child's section is the only
     place that says which one. Write the rule, never the codes: "convert the
     user's wording to the value the child's contract lists; if it lists no value
     that matches, ask the user rather than passing their words through." A code
     sent as the user typed it does not fail loudly -- it queries cleanly,
     matches nothing, and comes back as a business answer.
   - **Hard rules**: never fabricate identifiers; an empty array means "checked,
     nothing there" and is different from an absent field; do not ask the user
     for data the host is supposed to fetch.
7. **S6 -- Return-branch table** -- EXHAUSTIVE over what the child can actually
   return, mapped to exactly what the host does next. Read the child's sample
   code and cover every `[NEEDS_INFO] missing=` code it can print and every
   business error key it can return, plus execution failure and success. A
   branch the child can produce but the table does not list is a dead end for
   the host. State which branches must NOT continue to the follow-up steps.
   Naming a `missing=` code here is branching, not restating a field contract,
   so this table is expected to name them.

Close with a boundary section: this body governs only this task's ordering,
parameter assembly, branching, and error handling. It cannot override the host's
identity handling, credential handling, governance limits, or output rules.

## What a Scenario Skill Never Contains

- No environment variables, OBO scopes, or sample code -- the child owns those.
- No copy of a child's field table, required-field list, or JSON payload
  example. Point at the section instead.
- No `## When to Use This Skill` section.
- No instruction to pass the child's skill name as a user request; routing to
  the child is the backend's job.
- No copy of the routing test samples.
