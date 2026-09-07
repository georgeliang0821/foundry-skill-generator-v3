# Stage: TEST -- Scenario Addendum

This session is authoring a SCENARIO (orchestration) skill. The TEST stage
prompt above still applies to the routing samples, with the redefinition below.

## What can and cannot be tested automatically

The host chooses a scenario skill by reading its `name` and `description`. The
test runner drives the BACKEND, not the host, so **the scenario skill's own
discoverability cannot be verified automatically.** Never report a passing test
run as evidence that the host will select this skill. Say so explicitly when you
summarize results, and treat description quality as a review item the user must
judge.

What the run does verify is three cheaper things, in order. A layer only runs
when the previous one passed.

## L1 -- static topology

The saved SKILL.md is checked against the topology rules (T1-T10): frontmatter
shape, `metadata.children` as a non-empty list, `metadata.skill_type`, child
existence and accessibility, one level of nesting, and the forbidden capability
sections. It is also checked against the pointer rules (P1-P6) and the section
rules (C1-C3): every `fetch_skill` target is declared, every requested section
name resolves to a real `##` heading of that child, each skill is fetched once,
the fetch-authorization paragraph is present, every real skill the body names is
declared, and no child field contract has been copied in. No request is sent. An
L1 failure is always a formatting problem in the skill itself -- fix it with
`propose_patch` before rerunning.

Note what L1 can and cannot catch. A wrong section name fails **loudly**: the
error lists the child's real section names. A skill MISSING from
`metadata.children` fails **silently** -- nothing in L1, L2 or L3 detects it,
because the host simply never gets that capability. P5 catches only the case
where the body also names the skill. The dependency walk in PREPARE is the real
gate; if a step's behaviour is inexplicably absent at runtime, suspect an
omitted entry first.

## L2 -- parent absence

One confirmed positive sample is sent to the runner and the runner's reported
`skills_referenced` is checked to confirm the scenario skill is **NOT** in it. A
scenario skill is supposed to be invisible to the backend's candidate set.

If the scenario skill DOES appear, do not guess a single cause. There are
exactly two, and you must state both:

1. **Shape** -- the frontmatter no longer satisfies T1-T4, so the exclusion
   mechanism does not recognise this file as a parent. Recheck the frontmatter
   even if L1 passed on the local draft: the saved copy is what was tested.
2. **Environment** -- the test endpoint is running with the static skill
   provider, which applies no exclusion at all. Nothing in the skill can fix
   this; it is an environment finding for the user.

Plan a patch only for cause 1, and tell the user plainly that cause 2 is
possible and how to tell them apart (if the shape is provably correct, it is the
environment).

## L3 -- child reachability

No request of its own. The `skills_referenced` from L2 is checked again, this
time to confirm it contains at least one of the declared children.

A failure means the sample never reached a child, and the parent's body cannot
fix that: either the child is not deployed to the test endpoint, or its own name
and description do not match the sample. Say which one you suspect and why, and
do not plan a patch to the parent for it.

One sample routes to one child, so a scenario with several children only ever
confirms the one this sample reaches. The others are neither confirmed nor
refuted -- do not report them as failures.

The payload contract is NOT verified at runtime. No layer executes the child,
because the child does real writes and a routing test must have no side effects.
L1's content lint is the only thing standing between a stale payload contract
and production; treat its findings accordingly.

## Reflection

`record_reflection` still applies. Attribute each finding to its layer (L1/L2/L3)
in `what_went_wrong`, and keep the scenario skill's discoverability out of the
confidence delta -- it was not measured.
