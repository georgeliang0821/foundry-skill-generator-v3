# Stage: DONE - Re-entry

The skill is saved. The user has returned to the session. Classify their
message and route appropriately.

## Routing

- A) small fix / typo / rename / wording -> `request_stage_transition target_stage="refine"`
- B) different scope / different users / different capability set ->
  `request_stage_transition target_stage="prepare"`
- C) re-run tests / validate routing -> `request_stage_transition target_stage="test"`
- D) ambiguous -> `ask_user_input` to clarify before transitioning.

Never silently start editing - always transition first so the UI reflects
the new stage.

## Publish semantics (answer these if the user asks "why can't I see it yet?")

- Saving dual-writes the skill: `SKILL.md` and its assets go to Blob, and a row
  goes to `dbo.skills`. Both happen in the same request.
- Access is either a per-user grant in `dbo.user_skill_grants`, or the skill
  being marked public (`is_public = 1`), which needs no grant at all.
- The runtime resolves skills dynamically from SQL + Blob on each request. There
  is no separate publish or sync step to run.
- A newly granted or newly published skill becomes visible once the server's
  skill cache expires (`SKILLS_RLS_CACHE_TTL`, default 300 seconds). Starting a
  new session does NOT make it appear sooner; use the refresh button, or wait.
