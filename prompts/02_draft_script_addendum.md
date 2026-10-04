# Script Form Addendum (DRAFT and REFINE)

`## Skill Form` says `form: script`. This skill ships the user's code material,
unchanged, as its bundled script; SKILL.md tells the host how to run that script
and how to read what it returns. Where this addendum disagrees with the stage
prompt above or with the Skill Format Spec, this addendum wins.

## Sections

Write the body with these sections, in this order:

1. `## When NOT to Use This Skill` -- one line per neighbor, as for any skill.
2. `## How to Run` -- the skill is script-based: the host always executes its
   script with `run_skill_script` and never writes its own code for the task.
   Arguments are passed as a list of strings built from Required Inputs, with
   one example such as `["--building", "CLS", "--capacity", "8"]`.
3. `## Required Inputs` -- a table whose first column is every argument the
   script declares with `add_argument`, backticked as `` `--flag` ``: no more,
   no fewer, spelled exactly as in the script (S3). Then give the value, its
   meaning and its default. Guidance on building the arguments from the user's
   words (resolving relative dates, when to ask) goes below the table. Every
   backticked `--flag` anywhere in SKILL.md must be one the script declares.
4. `## Reading the Result` -- what stdout carries, then an exit-code table that
   lists exactly the exit codes the script can end with (0 = success, needs-info
   or business rejection; 1 = configuration; 3 = downstream failure) and what
   the host does for each. Needs-info gets a row of its own whose first cell
   is `` 0, `status` = `needs_info` ``; it says the first stdout line is
   `[NEEDS_INFO] missing=<field>`, followed by at most one JSON object, with
   exit 0. That `status` is the key in the script's own stdout JSON, never
   the tool's: `run_skill_script` reports this run as `success`, so never
   write that the tool returns `needs_info`. Then one output-field table per
   `status`, naming every key the script prints.
5. `## Composability` -- which skills depend on this one's result, and which
   are independent of it.
6. `## Prerequisites` -- one top-level bullet per environment variable the
   script reads: `` - `NAME` — what it is ``. Write "optional" on the bullet of
   a variable the script runs without; for a required one, say which exit code
   its absence produces. The sentence stating how the skill authenticates
   belongs here.

Leave out `## Overview`, `## When to Use This Skill` and the inline-form
sections: `` ## `[NEEDS_INFO]` 契約 ``, `## Environment Variables`,
`## OBO Token Scopes`, `## 部署設定使用規範` and `## API Reference / Sample Code`.
What they would say is covered by Reading the Result and Prerequisites.

## Writing rules

- **No Python code fence anywhere in SKILL.md** (S2). The script is shipped
  next to it; a copy in the body would drift from it.
- **Never mention where the script is stored**: no `scripts/` path and no script
  file name. The host finds the script by the skill name. Never mention
  `eaa_runs` either (S8).
- **Document the script as it is.** Every flag, exit code, output key and
  variable comes from the script under `## Bundled Script`. Never document
  behavior the script does not have.

## The script is read-only

- You never modify the script. `propose_patch` edits SKILL.md only. When a
  problem lies in the script itself (an operation it lacks, a flag the user
  wants), say so plainly instead of working around it in the prose.
- **Replacing the script:** ask the user to add the new version as a new code
  material and to remove the old code material. The bundled script is replaced
  once exactly one code material remains that differs from it, parses and
  passes the script checks.
- **After a system message says the bundled script was replaced:** confirm
  with the user in the chat that the new code still covers every operation this
  skill documents, then patch `## Required Inputs` so its first column matches
  the new script's arguments exactly (S3), and `## Reading the Result` /
  `## Prerequisites` wherever exit codes, output keys or variables changed.
- When `## Skill Form` lists why the code material did not replace the script,
  relay those reasons to the user. The previous script stays in effect until
  they are fixed.
