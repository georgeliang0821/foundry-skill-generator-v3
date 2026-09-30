# Stage: TEST -- Script Form Addendum

This skill is script-based. Besides the prepared code, each sample that the
runtime answered carries `requested_scripts` in `## Latest Test Run`: the
scripts the host asked `run_skill_script` to run, and with which arguments.
Nothing ran; read them as the host's plan.

- `valid` means the arguments are a list of strings and every `--flag` passed
  to THIS skill's script is one the script declares. It says nothing about the
  values: check those against `## Required Inputs` and the sample query yourself.
- `INVALID` lists its `problems`:
  - `args is not a list of strings` -- the host did not follow `## How to Run`.
    Fix the wording there.
  - `` `--x` is not declared by the script `` -- the host invented or misspelled
    a flag. Find the SKILL.md text that led it there (`## Required Inputs` must
    name exactly the script's flags) and fix that text.
- A sample that routed here with `requested_scripts: (none)` means the host did
  not plan to run the script at all -- a usage finding against `## How to Run`.
- An entry for another skill is that skill's business; its flags are not
  checked against this script.

Record each problem as its own `what_to_change` item. The fix is always a
SKILL.md patch; the script is never edited here. If a sample needs something the
script cannot do, say so and ask the user whether to supply a new code material.
