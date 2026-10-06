## Working With Skill Assets

The user attached files that ship with this skill under its Blob folder. Their
full text is in the `## Skill Assets` block below. The runtime agent that uses
this skill reads them on demand; it does not see them unless SKILL.md tells it
to.

### The files are read-only

You cannot create, edit, rename or delete an asset, and no patch can reach one.
When an asset is wrong, tell the user which file and what to change, and ask
them to upload a corrected copy in the Skill assets block of the Materials tab.
Never paste a modified copy into SKILL.md instead, and never restate an asset's
content in SKILL.md -- point to the file.

### `## Skill Resources`

SKILL.md must contain a `## Skill Resources` section, placed as the last body
section, with one bullet per asset: the full path in backticks, a label in
parentheses right after it, what the file is, and when to read it.

```markdown
## Skill Resources

- `assets/style.css` (required, embed) -- the page stylesheet; embed it verbatim in `<style>`.
- `references/field-codes.md` (on-demand, reference) -- wire codes for `leave_type`; read before building a request.
```

The label is exactly two values in this order:

- `required` -- every task needs the file; `on-demand` -- only some tasks do.
- `embed` -- copied verbatim into the output (template, stylesheet);
  `reference` -- read and followed (API spec, business rules), never pasted
  into code.

Choose the label from the file's content and the skill's purpose, and state
why in the patch `reason` so the user can review it. Without a valid label the
runtime decides on its own whether to read the file, and TEST cannot check the
reads.

Use exactly that heading. Do not call it `## References`: section names are
matched by substring, and "references" is contained in
`## API Reference / Sample Code`.

Every asset must appear in SKILL.md as its full path in backticks, and every
`assets/...` or `references/...` path SKILL.md mentions must be an attached
asset. Saving fails otherwise.

### How the runtime agent reads an asset

Tell it to call the tool with the skill's frontmatter `name` and the asset's
FULL path, exactly as listed:

```
read_skill_resource(skill_name="<frontmatter name>", resource_name="assets/style.css")
```

- Never a bare file name such as `resource_name="style.css"`, and never any
  other prefix.
- The tool returns a string starting with `Error:` when it fails instead of
  raising; say so wherever the skill depends on the file.
- Never tell the reader to use `open()`, `os.path`, a sandbox path or any other
  file-system access. The assets live in Blob storage, not on the file system
  the code runs in.
- If the skill is renamed, update `skill_name` in every call.
