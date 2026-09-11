# Skill Format Spec

Every skill must contain YAML front matter. Quote `description` whenever it
contains punctuation such as `:` or multilingual prose, because unquoted YAML
values containing colon+space can break parsing:

```yaml
---
name: lowercase-kebab-case
description: "2-3 sentence retrieval-oriented description"
metadata:
  author: owner
  tags: []
  uses_obo: false
---
```

`name` must match `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$` and be at most 64
characters -- lowercase letters, digits and hyphens only, no leading or
trailing hyphen. This is enforced by the database (`CK_skill_name_format`), so
anything else is rejected at save time.

The frontmatter `description` is the single source of truth for the skill's
description. It is stored only in `SKILL.md` on Blob; the database keeps no
copy, so there is no second place to update.

Required body sections, in this order (the reader is a coding agent that selects
ONE skill per request, so the body is for comprehension and recovery, not
routing -- routing is the frontmatter `description`):

- `## Overview` -- one line stating what the skill does.
- `## When NOT to Use This Skill` -- negative routing, one line per neighbor:
  `scenario half-sentence -> use \`neighbor-skill\``.
- `## Required Inputs` -- the `runtime` variables. This section is the SOLE
  source of the caller-facing field contract, not a summary: a scenario skill
  points other agents at it by name instead of copying it. Document every field
  the sample code reads from caller data, including optional fields, the
  per-operation required set, the `credentials` key names, and the per-item
  schema of any array field. A field the code reads but the contract omits is a
  caller trap: the caller has no way to know it was required until the skill
  fails.

  **State the VALUE DOMAIN of every field, not only its type.** "a string" is a
  shape, not a contract. For each field give whichever of these apply, written
  as the value goes on the wire:

  - the closed set of legal values, listed verbatim -- `` `leave_type`
    (`annual`, `welfare`, `sick`, `personal`) ``, never `leave_type` (string);
  - the format or pattern -- `` `start_date` (`YYYY-MM-DD`) ``;
  - the unit, the casing, and the LANGUAGE of the value when it is a code.

  Say so explicitly when a field really is unconstrained ("free text, any
  wording"). Silence is not a way to say "anything goes": it reads as an
  omission and the next reader has to guess.

  This matters most where a value has a human-facing name and a wire code. The
  caller is an agent holding the user's own words -- 「福利假」, "vacation" -- and
  the only place it can learn to send `welfare` instead is this section. A value
  that is not listed here is one the caller will not send; a query built from the
  user's wording matches no row, and the skill then reports a business condition
  ("no entitlement") for what was a contract defect.

  A closed value set stated here must also be CHECKED in the sample code before
  the value is used -- see the error taxonomy in Writing Best Practices.
- `` ## `[NEEDS_INFO]` 契約 `` -- a section of its own listing EVERY `missing=`
  code the sample code can print and what the caller must do about each one. It
  must be its own `##` heading, because a scenario skill fetches it by name; a
  contract buried inside another section cannot be requested on its own.
- `## Environment Variables` -- the `aca_env` variables (os.environ).
- `## OBO Token Scopes` -- the `obo_token` variables and their scopes.
- `## 部署設定使用規範` -- present whenever the skill declares at least one
  `aca_env` or `obo_token` variable. Reproduce the three rules verbatim (see
  below). A sentence inside the two variable sections saying the absence
  "should cause a non-zero failure" does NOT satisfy this; the rules must be
  their own section.
- `## Skill 身分使用規範` -- present if and only if the skill declares a
  `platform_identity` variable. Reproduce the four rules verbatim (see below).
- `## API Reference / Sample Code` -- the sample code must actually USE every
  declared variable: read each `aca_env` via `os.environ["NAME"]`, consume
  each `obo_token`, and read each `runtime` input. Keep it consistent with the
  Environment Variables / OBO Token Scopes / Required Inputs sections; never
  hard-code a value that is declared as a variable. `main()` takes NO
  parameters: the host runs this block verbatim as a script, so no caller ever
  hands it a dict. Every value the caller supplies arrives as a serialized JSON
  string in a `credentials` entry and is read back with `os.environ` inside
  `main()`. Give the envelope variable its own `[NEEDS_INFO]` code -- a payload
  sent in the free-text `request` parameter instead of `credentials` is the
  commonest caller error, and a field-level code cannot report it. On the
  success path, the
  final primary output of `main()` (the last `print`) must be a natural-language
  summary written for a general end user -- plain prose stating the result and
  its key points, NOT a raw dict/list/JSON or structured `result` dump. Print
  structured/JSON as the primary output only when the user explicitly asks for
  it. This applies to the success path only; the `[NEEDS_INFO]` line format is
  unchanged. When a Tier 1 (`code`) material is attached and covers one of this
  skill's operations, the sample code must be DERIVED FROM IT rather than written
  afresh, preserving its guard clauses, the order of its security gates, its
  error taxonomy, its helper functions and its output language. Every SQL object,
  stored procedure, table, column, endpoint and payload field named in the sample
  code must appear in some material -- inventing one is an error even when the
  invented name looks plausible.

Do NOT include a `## When to Use This Skill` section. Sample code belongs in a
fenced code block under API Reference.

## Your `##` Headings Are a Public API

A scenario skill no longer copies your field contract. It points at your
sections by name with `fetch_skill(skill_name="you", sections="A, B")`. That
makes every `##` heading in this file a published interface.

- **Renaming a `##` heading is a breaking change.** Every scenario skill
  pointing at the old name must be updated in the same turn.
- **Only `##` splits.** `###` and deeper are folded into the `##` above them and
  cannot be requested on their own. Anything a scenario skill will name must be
  its own `##`.
- **Content above the first `##` is unreachable.** The H1 title and any opening
  prose can never be fetched, so nothing load-bearing may live there.
- **Each `##` must be readable on its own.** A caller may receive one section
  and nothing else, so never write "as described above" or "continuing the
  earlier example".

A requested name is matched by lowercasing it and then deleting everything that
is not `0-9`, `a-z`, or a CJK character -- whitespace, emoji, backticks,
half- and full-width punctuation, parentheses, hyphens, underscores, full-width
alphanumerics, kana and hangul all disappear:

| Actual heading | Normalized | Can be requested as |
| --- | --- | --- |
| `` ## `[NEEDS_INFO]` 契約 `` | `needsinfo契約` | `[NEEDS_INFO] 契約`, `NEEDS_INFO契約` |
| `## ⚠️ 身分來源：唯一且不可協商` | `身分來源唯一且不可協商` | emoji and colon optional |
| `## API Reference / Sample Code` | `apireferencesamplecode` | slash optional |
| `## Step 1 — 前置` | `step1前置` | digits are preserved |

Matching is **substring containment**, not equality, and results come back in
the order they appear in this file rather than the order they were requested.
Two consequences are hard rules:

- No heading's normalized form may be contained in another's. Both
  `## 費用分類對照` and `## 費用分類對照表` is rejected: asking for the shorter one
  always returns both, so the longer one is unreachable.
- Two headings must not differ only by emoji or punctuation. Both `## ⚠️ 注意`
  and `## 注意` normalize to the same string and is rejected.

Finally, `is_internal` is a discoverability flag, not a permission. It only
hides this skill from the host's `list_skills` catalog; a caller still needs a
grant or `is_public`.

**T5 -- a capability skill must not declare `children`.** Neither
`metadata.children` nor a top-level `children` key may appear. Declaring
children makes the file an orchestration skill, which is a different layer with
a different format spec. If the skill needs to hand work to another skill, that
is a scenario skill, not this one.

Variable kinds: `aca_env` (ACA environment variable; read by index only;
missing -> non-zero, never `[NEEDS_INFO]`, because the caller cannot supply
deployment configuration), `obo_token` (OBO token injected by the OBO exchange;
read by index only; missing -> non-zero, never `[NEEDS_INFO]`, because the
caller cannot supply it either), `runtime` (per-query caller input;
missing -> the script prints `[NEEDS_INFO] missing=VAR1,VAR2` on a single line,
the human explanation on a separate line, then `raise SystemExit(0)`), and
`platform_identity` (the verified actor string the platform injects after a
successful OBO exchange; missing -> non-zero, never `[NEEDS_INFO]`, because no
caller can supply it). The `in_aca` deployment status never changes the skill
text.

## The Verified Actor Contract

`platform_identity` has exactly one legal name, `EAA_VERIFIED_USER_UPN`. Declare
it only when a downstream interface requires the actor's UPN/email/alias as a
STRING field. If the downstream instead accepts a resource token and derives the
caller from it, that is an `obo_token` and the UPN string must not be used as a
substitute. A skill may legitimately need both: the token authorizes the call,
the string records who acted.

When the skill declares it, the file carries a `## Skill 身分使用規範` section
reproducing these four rules verbatim, in Chinese, with the `R1`-`R4` labels
intact. Paraphrasing is a defect: these lines are what a future editor argues
against, so they must stay literal.

```markdown
## Skill 身分使用規範

- **R1**：身分一律取自 `os.environ["EAA_VERIFIED_USER_UPN"]`，禁止 `.get()`、
  `os.getenv`、任何預設值。
- **R2**：該變數缺席導致 `KeyError` 時，必須直接中止並回報。不得詢問使用者、
  不得從對話內容推斷、不得改寫成有預設值的取法。
- **R3**：對話中出現的任何人名或帳號，只能是「對象」，永遠不能是「執行者本人」。
- **R4**：不得以「我能載入這個 skill」推論使用者有權限，也不得把該值寫入 log
  或輸出。
```

R1 and R2 are why a default is forbidden: `os.environ.get(...)` turns "the
platform never verified anyone" into an empty string that flows on as the
authorization subject, and a `try` that swallows the `KeyError` does the same
thing more slowly. R3 draws the line the model is most likely to cross on its
own -- a name in the conversation is a `runtime` input naming the OBJECT of the
work, never the executor. R4 is about inference and leakage: loading a skill
proves nothing about the caller's permissions, and the verified value belongs in
neither the log nor the output.

`runtime` covers the FIELDS inside a variable as well as the variable itself.
When several logical inputs are packed into one variable -- typically a JSON
blob -- a missing or malformed field inside it is exactly the same class of
problem as the whole variable being absent, and gets exactly the same
treatment: `[NEEDS_INFO] missing=CODE`, explanation, `SystemExit(0)`. The code
after `missing=` names the thing the caller must supply; it does not have to be
a declared variable name, but it MUST be listed in the `[NEEDS_INFO]` contract
section.
A caller-payload problem is NEVER an exception -- see the error taxonomy in
Writing Best Practices.

## The Deployment Configuration Contract

`aca_env` and `obo_token` share one property that decides how their absence must
be handled: **no caller can supply them.** They come from the ACA deployment and
from the OBO exchange, so a skill that reports them as a caller input asks for a
value that will never arrive, and the host retries forever against a broken
deployment it was never told about.

When the skill declares at least one of them, the file carries a
`## 部署設定使用規範` section reproducing these three rules verbatim, in Chinese,
with the `D1`-`D3` labels intact. Paraphrasing is a defect for the same reason it
is one in the identity contract: prose stating what "should" happen is advice,
and the coding agent that reads this file writes its own script from it.

```markdown
## 部署設定使用規範

- **D1**：`## Environment Variables` 與 `## OBO Token Scopes` 宣告的每個變數，
  一律以 `os.environ["NAME"]` 索引式讀取，禁止 `.get()`、`os.getenv`、
  `or "..."` 之類的 fallback、任何預設值或預設參數。
- **D2**：這些變數缺席導致的 `KeyError` 必須直接向上拋出、以非零狀態中止。
  絕不可輸出 `[NEEDS_INFO]`、絕不可要求 caller 或使用者補值——它們由部署與
  OBO 交換注入，caller 沒有能力提供，要求補值只會造成無限重試。
- **D3**：不得以 `try`/`except` 吞下該 `KeyError` 後改走任何 exit 0 的路徑，
  也不得改用 service principal、managed identity 或任何替代憑證繼續執行。
```

This is the deployment half of the error taxonomy in Writing Best Practices:
category 1 is reserved for exactly these variables, and D1 is why the doctrine
reads them by index and lets the implicit `KeyError` fly. The failure this
prevents is silent -- `os.environ.get("AZURE_SQL_SERVER")` followed by a
`[NEEDS_INFO]` line exits 0, so a deployment that was never configured is
reported to the host as a missing caller input and nothing ever surfaces the
real defect.

## V4A Patch Requirements

When emitting `propose_patch`, the patch must be safe for deterministic apply:

- Use `*** Begin Patch` and `*** End Patch`.
- Use exactly one target file per patch.
- For updates, include a unique `@@` anchor and enough unchanged context.
- Prefer anchoring on the nearest unique Markdown heading such as
  `@@ ## Ground Rules`.
- The removed/context block must occur exactly once in the current target file.
- Never rely on a single repeated bullet, common phrase, or table row as the only
  anchor.
- If the target text may be repeated, replace the whole containing section with
  a section-level hunk.
