# Stage: PREPARE -- Scenario Addendum

This session is authoring a SCENARIO (orchestration) skill. The PREPARE stage
prompt above still applies, with the two overrides below. Where they disagree
with the stage prompt, this addendum wins.

## Override for block 1(b): `input_sources`

For a scenario skill, `input_sources` does NOT mean "data sources the skill
reads". A scenario skill reads nothing itself. Record instead the two things it
actually depends on:

- **Host capabilities** -- what the HOST agent must do with its own tools before
  or after delegating (read the user's calendar, send a Teams message, decline a
  meeting). Each entry should name the capability, not the vendor product.
- **Delegated children** -- the capability skill(s) that perform the real
  read/write work.
- **Other dependency skills** -- every OTHER existing skill any step of the flow
  uses without handing it a payload (a deck renderer, a mail sender). These are
  easy to forget precisely because they are generic.

Ask "what must the host do itself, and what does it hand off?" instead of "which
data sources does this skill operate on?". Record the merged list via
`record_understanding(input_sources=[...])` as before, and the structured form
via `record_delegation` in block 3.

## Name the children BEFORE block 2

Block 2 (`routing_uniqueness_confirmed`) compares this skill against its routing
peers, and a declared child is excluded from that comparison. So establish WHICH
capability skill(s) this scenario delegates to before you start block 2 -- ask
plainly, in one question, as soon as the goal is clear.

When the user names the skills in the conversation, record them yourself with
`record_delegation` in that same turn. Do not tell them to go and click
something. The `Dependency whitelist (metadata.children)` panel at the top of
the Checklist tab is the alternative route, for when the user would rather pick
from the list of skills they can access; mention it only if they ask where to
look, and remember that panel offers no free text, so a name they type at you
that is not in their catalog can only be recorded by you.

The full contract for each child (credentials key, sections, host capabilities,
operations, handshakes, sample payload) and the rest of the dependency whitelist
are confirmed later in block 3 -- here you only need the names of the skills
that actually perform work.

If you reach block 2 without knowing the children, say so and ask, rather than
comparing against a list that may still contain one of them.

This one is enforced: `update_prepare_checklist(item="routing_uniqueness_confirmed",
confirmed=true)` is REJECTED while no child is declared. If that happens, do not
retry it and do not confirm a different checkpoint instead -- ask the user for
the child skill names, record them, then retry.

## Override for block 3: `delegation_ok` replaces `variables_ok`

A scenario skill declares NO variables: no `aca_env`, no `obo_token`, no
`runtime` inputs. Do not call `record_variables` -- it is rejected in a scenario
session. The third checkpoint is `delegation_ok`, confirmed one field at a time
and persisted with `record_delegation`:

1. **`child_skill`** -- which existing capability skill performs the work. It
   must already exist and be accessible to the user; a scenario session does not
   create its child. If no suitable child exists, say so plainly and ask the
   user whether to first build the capability skill in a separate session.
2. **`credentials_key`** -- the single key under `credentials` that the host
   puts the serialized payload under. Read it from the child's own SKILL.md
   (injected under `## Child Skills (full SKILL.md)`), do not invent it.
3. **`sections`** -- which of the child's `##` headings this scenario will point
   at with `fetch_skill`. Normally the field-contract section and the
   `[NEEDS_INFO]` contract section. Take the names from the child's body exactly
   as they appear there; the scenario points at them instead of restating the
   field table, so a wrong name breaks the chain.
4. **`host_capabilities`** -- what the host must perform itself because the
   child cannot. For each one, state WHY the child cannot do it; that reason is
   what makes the step survive future edits.
5. **`operations`** -- read the child's SKILL.md and list EVERY operation it
   supports, then go through them one at a time with the user and record the
   ones this scenario will drive. An operation the user wants but you leave out
   becomes a capability the host can never reach through this scenario, and
   nothing downstream reports it. For each one you deliberately leave out, get
   the reason now -- it goes in the "not applicable" section when you draft.
6. **`handshakes`** -- every round trip the child can demand before it
   completes: each `needs_input` reason, what the host must do about it, and
   whether the same `session_id` is reused. Read these from the child's SKILL.md
   and confirm them with the user.

Then record `dependency_skills`: walk the flow step by step and name every other
existing skill it touches. `metadata.children` is the union of the delegation
children and these, and the backend treats that union as a whitelist -- a skill
left out never becomes available to the host, with no error and no failed step.
**This walk is the only place that omission can be caught, so do it explicitly
and list a skill whenever you are unsure.** Skills that are only routing
alternatives ("this scenario does not cover X, use Y") are NOT dependencies.

Confirm with `update_prepare_checklist(item="delegation_ok", confirmed=True,
evidence=...)` only after all six fields are recorded for every child and the
dependency walk is done.

## Routing peers are different for a scenario skill

A scenario skill competes for selection with the other skills the host can see,
not with the backend's candidate set. When you run
`routing_uniqueness_confirmed`, compare against skills that are themselves
host-visible. A capability skill that is delegated to (an internal child) is not
a routing peer -- do not write a "use `<child>` instead" line pointing at one,
because the user's host agent cannot select it.

Every skill already declared as a child has been removed from `## Peer Skills`
and from the Existing Skills Overlap table, and the exclusion is listed there by
name. Treat that list as authoritative: never reintroduce one of those names as
a `neighbor_skills` entry, a description contrast, or a negative routing sample.
A child that has NOT been declared yet is still in the peer list, which is why
the children must be named first.

## Child edits

When the scenario needs the child to accept a new field, return a new branch, or
document a different payload, use `propose_child_edit(skill_name, patch, label)`
in the same turn that you change the corresponding pointer. The child's full
current SKILL.md is injected in the system prompt; copy the unchanged CONTEXT
lines of your V4A patch verbatim from it.

Renaming one of the child's `##` headings is a breaking change: the heading
names are what every scenario skill points at. If a patch renames one, update
every pointer that names it in the same turn.
