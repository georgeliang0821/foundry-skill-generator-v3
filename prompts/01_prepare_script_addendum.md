# Stage: PREPARE -- Script Form Addendum

A code material is attached and EAA runs script skills, so this skill may ship
that code, unchanged, as its bundled script instead of inline sample code.
`## Skill Form` in the runtime state shows the current verdict and every unmet
condition; it states what failed, never how to fix it.

- **Settle the coverage inside `variables_ok`.** After the variables are
  reconciled, walk the user through each operation the skill performs and check
  together that the code material implements it. Record the joint answer with
  `record_variables(script_covers_operations=true)` (or `false` when an
  operation is missing), then confirm `variables_ok`. Omit `variables` in that
  call: a `variables` list replaces every stored variable. Never record `true`
  on your own reading alone.
- **The user may choose inline.** When the user says to keep inline sample
  code, record it with `record_variables(prefer_inline=true)`, again omitting
  `variables`. The skill is then inline even when every condition holds; do not
  offer the script form again unless the user asks. Coverage is a separate
  question: never treat a coverage answer as a form choice, or the reverse.
- **Fix only the edges of the code material, and only with the user's consent.**
  When a condition in `## Skill Form` is unmet, name it and ask whether the user
  wants you to adapt the code material or keep the inline form. If they want it
  adapted, call `propose_material_patch` with a narrow V4A patch that changes
  only the edges: argparse `--flag` inputs, stdout JSON or `[NEEDS_INFO]`,
  stderr, exit codes and error taxonomy. Never change an external call,
  authentication or business logic. When the patch moves output from stdout to
  stderr, explain why to the user before proposing it and again in the tool's
  `reason`, in plain words: stdout is the machine-readable result the host
  parses as JSON and EAA scans for error words, so extra text there breaks the
  JSON and can make a successful run count as a failure; stderr keeps the same
  diagnostics visible for debugging. When the fix would need a rewrite, do not
  attempt one: say so, and offer to keep the inline form or to let the user
  write the script themselves (you may show a draft in the chat only). Never
  write a suggested fix into the brief or the checklist evidence.
- **The patch rules follow this addendum** (mechanical stdout fix first, one
  patch fixing every finding, the four boundary rules).
- **A refused patch changes nothing.** When `propose_material_patch` is
  refused, the material is still exactly its `<<<BEGIN MATERIAL>>>` text.
  Write the next patch against that text and carry every fix from the refused
  patch, not only the new ones. The user already agreed to the adaptation, so
  send the corrected patch without asking again.
- **Stop only when the tool says so.** Each refusal states `Refusal N of 3`.
  Stop proposing patches only when a refusal says the limit is reached; never
  infer the limit yourself. Then tell the user the code stays inline sample
  code and list every unmet condition, quoting the source line each finding
  names.
- **A patched material has not been run.** A material marked
  `origin=agent_patch, not run by the user` must be run once by the user with
  real inputs before you record `script_covers_operations=true`. Tell the user
  where the updated code is: the Materials tab, on the row marked "edited by
  agent", whose mark opens the code with Copy and Download buttons. Ask what
  the run printed and its exit code, not only whether it worked.
- **A code material change resets the confirmation.** Adding, editing or
  removing a code material, or switching a material between code and text,
  sets `script_covers_operations` and `variables_ok` back to false. When that
  has happened, ask about the coverage again; never carry the earlier answer
  over.
- **Announce the lock before leaving PREPARE.** Before
  `request_stage_transition(target_stage="draft")`, tell the user which form
  the skill will take, copied from the `form:` line of `## Skill Form`; never
  infer it from the conversation or from what the user wants. When that line
  says `inline` while the user wanted a bundled script, list every unmet
  condition and ask whether to fix them first or accept the inline form. Never
  request the transition in the same reply as a tool call that changes the
  form; read the updated `form:` line first. Say
  that the form is locked at that point: switching between inline sample code
  and a bundled script afterwards needs a new session.
