## Adapting a code material's edges

- **Mechanical stdout fix first.** When `## Skill Form` says a mechanical
  stdout fix is available, propose it before any hand-written patch, with
  `propose_material_patch(stdout_fix=true)` and no `patch`: the backend
  computes it and adds `file=sys.stderr` to plain-text progress prints only.
  It is never refused for being partial and never counts as a refusal. Once
  it is applied, fix whatever `## Skill Form` still lists in one regular
  patch against the updated material, without asking the user again.
- **One patch fixes every finding.** A regular patch is refused unless the patched code
  clears every `script_lint` and `inputs` condition in `## Skill Form`; a
  partial fix changes nothing about the form. Find each finding by the source
  line it quotes, not by counting lines. Four rules the inline conventions do
  not prepare you for:
  - After the `[NEEDS_INFO] missing=...` line, a bundled script prints at most
    one `json.dumps` object; the human explanation goes into that object (for
    example `reason`), never onto its own stdout line.
  - A diagnostic that reports a failure (a delete that did not happen, a
    rejected request) must not just move to stderr: EAA does not read stderr on
    an exit-0 run, so the failure would look like success. Collect it into the
    result JSON and exit 3.
  - Values from the user's request arrive only as argparse `--flag` arguments;
    `globals()` never holds them and environment variables carry only
    deployment settings and credentials.
  - Adding argparse brings two rules of its own: create the parser with
    `ArgumentParser(add_help=False)`, and call `parse_args()` inside
    `try` / `except SystemExit` whose handler prints the `[NEEDS_INFO]` line
    (then at most one JSON object) and exits 0.
