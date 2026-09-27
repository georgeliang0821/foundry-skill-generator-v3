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

A skill that uses the platform Managed Identity also declares
`metadata.mi_scopes` (see The Managed Identity Contract); every other skill
omits that key.

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
- `## OBO Token Scopes` -- the `obo_token` variables and their scopes. This is
  also where the skill states how it authenticates when it uses no OBO token:
  a Managed Identity skill writes the authentication sentence here (see The
  Managed Identity Contract below).
- `## 部署設定使用規範` -- present whenever the skill declares at least one
  `aca_env` or `obo_token` variable. Reproduce the three rules verbatim (see
  below). A sentence inside the two variable sections saying the absence
  "should cause a non-zero failure" does NOT satisfy this; the rules must be
  their own section.
- `## Skill 身分使用規範` -- present if and only if the skill declares a
  `platform_identity` variable. Reproduce the four rules verbatim (see below).
- `## API Reference / Sample Code` -- the sample code must actually USE every
  declared input according to the Business Input Sources contract. Explicit
  `input-bindings` may use request, credentials, or mixed business sources; the
  following environment-only recipe applies to LEGACY inputs without bindings.
  For those legacy inputs, read every
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

D3 forbids switching to another identity AFTER a declared variable turned out
to be missing. It does not forbid a skill whose declared way of authenticating
IS the platform Managed Identity; that skill follows the contract below.

## The Managed Identity Contract

Some skills call an Azure resource (Azure Storage, Table Storage, Foundry, ...)
with the platform's Managed Identity because no OBO token is available for it.
The runtime no longer assumes a Managed Identity on its own: a skill that does
not name its credential explicitly fails, or comes back as `[NEEDS_INFO]`. The
runtime's MI gate also hands a token to a script only when its skill was loaded
in the same turn AND declares the resource in `metadata.mi_scopes`; an
undeclared resource fails with an error containing `EAA MI proxy`.

### Choose the authentication, in this order

1. The call acts AS THE USER (the user's own data, auditing down to a person)
   -> an OBO token such as `GRAPH_ACCESS_TOKEN` or `AZURE_SQL_ACCESS_TOKEN`
   (`obo_token`). Never the Managed Identity.
2. The call acts as the PLATFORM against an Azure service that supports Entra
   -> `DefaultAzureCredential()` plus `metadata.mi_scopes`. Never switch to an
   API key.
3. An on-premises or third-party API fronted by APIM or Entra App Proxy that
   accepts Entra tokens -> rule 1 or 2, with a token.
4. A service that only accepts an API key -> read the value an ACA secret
   injects with `os.environ["<ENV_NAME>"]` and declare it as an `aca_env`
   variable. Never write the key into the skill: the file enters the model
   context, the conversation history and every `fetch_skill` result.

Only use the Managed Identity when the materials or the user say so. A skill
whose downstream decides the caller from a user token (Graph `/me`, Azure SQL
with RLS) is an `obo_token` skill and must not use a Managed Identity.

### Declare `metadata.mi_scopes`

The field is REQUIRED whenever the sample code calls
`DefaultAzureCredential(` or `ManagedIdentityCredential(`, including when the
credential is handed to an Azure SDK client that fetches the token internally
(`TableServiceClient(..., credential=DefaultAzureCredential())`). A skill that
does not use the Managed Identity must NOT carry the field.

```yaml
---
name: <skill-name>
description: "..."
metadata:
  mi_scopes:
    - https://storage.azure.com
---
```

It is a YAML list. Each entry is an Entra resource identifier, `https://<host>`
with no path; a trailing `/.default` is optional. List only the resources the
code actually reaches (least privilege):

| Service the code reaches | `mi_scopes` entry |
| --- | --- |
| Blob / Table / Queue / Data Lake (`azure-storage-*`, `azure-data-tables`) | `https://storage.azure.com` |
| Microsoft Foundry / Agent Service / Routines | `https://ai.azure.com` |
| Azure AI Search (`azure-search-documents`) | `https://search.azure.com` |
| Azure OpenAI / Azure AI Services (cognitiveservices) | `https://cognitiveservices.azure.com` |
| Service Bus | `https://servicebus.azure.net` |
| Event Hubs | `https://eventhubs.azure.net` |
| Azure SQL as the platform | `https://database.windows.net` |
| ARM / management plane as the platform | `https://management.azure.com` |
| Key Vault | FORBIDDEN -- the runtime always refuses it; the lint rejects it |

A scope string written in the code (for example
`get_token("https://ai.azure.com/.default")`) must match a declared entry.

### Declared is not the same as allowed

The platform keeps a global `MI_SCOPE_ALLOWLIST`, by default only
`https://storage.azure.com,https://ai.azure.com`. When the skill needs any
other resource, tell the user in your `text` field (never in the skill file):
「部署前需把 `<資源>` 加入 ACA 環境變數 `MI_SCOPE_ALLOWLIST`，並替平台 MI 指派
`<最小 RBAC 角色>`」. Allowlisting `https://database.windows.net` or
`https://management.azure.com` gives every skill that declares it the platform
MI's full rights on that resource, so prefer an OBO token for those unless the
resource is genuinely not authorized per user.

### Write the code this way

- Import the credential explicitly --
  `from azure.identity import DefaultAzureCredential` -- and pass
  `credential=DefaultAzureCredential()` to the client. Use
  `DefaultAzureCredential`, not `ManagedIdentityCredential`.
- Get the token only through azure-identity. Never read or hard-code
  `IDENTITY_ENDPOINT`, `IDENTITY_HEADER`, `MSI_ENDPOINT` or `MSI_SECRET`, and
  never call `169.254.169.254` or `localhost:<port>/msi/token` yourself.
- Never pass `managed_identity_client_id=`, `client_id=`, `object_id=` or
  `mi_res_id=`. The platform has only a system-assigned identity and rejects
  any selector (403).
- When the token is refused and the error contains `EAA MI proxy`, print a
  clear error and exit non-zero. Never retry with another credential
  (`ChainedTokenCredential`, `ClientSecretCredential`, `AzureKeyCredential`,
  ...), never ask the user for a key, never switch to another identity.
- Never leave the credential for someone else to supply: no `credential=...`,
  no `credential=None`, no `<your-credential>`, and no prose such as "fill in
  the credential for your deployment" or 依部署環境自行填入.
- The resource endpoint is still deployment configuration: declare it as an
  `aca_env` variable, never hard-code it.
- The Execution Environment Contract still applies: no run-time `pip install`,
  no `pwd.getpwuid()` / `os.getlogin()`, no background process, write only in
  the working directory.

### State it in `## OBO Token Scopes`

One sentence naming the credential and the declared resources, for example:
「本 skill 不需要 OBO token 變數。以 `DefaultAzureCredential()`（平台 Managed Identity）
驗證，已宣告 `metadata.mi_scopes: [https://storage.azure.com]`。」 When the skill also
uses OBO tokens, list them and add the sentence for the resources that use the
Managed Identity.

The runtime executes a sample only when the skill is loaded in that same turn,
so an MI token is never available to code run outside it. Route-only tests do
not execute code and are unaffected.

## Platform-Reserved Names

The runtime strips these platform secrets from every skill's environment. A
skill must never read them and must never list them in `## Environment
Variables`, `## OBO Token Scopes`, `## Required Inputs` or any env.yaml as
required -- not even in a prose example, because the runtime model follows the
prose and the runtime lint scans the full text:
`OBO_CLIENT_SECRET`, `TEAMS_NOTIFY_WEBHOOK_URL`, `LOGIC_APP_SKILL_REVIEW_URL`.

A value the caller sends in `credentials` must not use a name the runtime
discards: `PATH`, anything starting with `PYTHON`, `LD_` or `EAA_VERIFIED_`,
or any `OBO_SCOPE_REGISTRY` key (for example `AZURE_SQL_ACCESS_TOKEN`,
`GRAPH_ACCESS_TOKEN`). Such a key never reaches the script. OBO tokens are
unchanged: keep reading them with `os.environ["<OBO_SCOPE_REGISTRY key>"]` and
declare them under `## OBO Token Scopes`, never as caller inputs.

## The Execution Environment Contract

The runtime executes the script as a subprocess that may run under a fresh,
never-reused, non-root uid. Its working directory is the session's private
`work_dir`, and `HOME`, `TMPDIR`, `MPLCONFIGDIR` and `XDG_CACHE_HOME` all point
there. That uid has no `/etc/passwd` entry. When the run ends, every process
the uid started is killed, and symlinks, special files and multiply-linked
files in `work_dir` are deleted. Output files are collected only from the top
level of `work_dir`, only when they are new regular files with an allowed
extension. `DefaultAzureCredential()` keeps working for a skill that declares
`metadata.mi_scopes` (see The Managed Identity Contract).

The sample code must follow these five rules. They hold whether or not the
sandbox is on, so apply them to every capability skill. This contract adds no
section to the skill file.

- **E1** -- Read and write files only in the current working directory. Never
  write to an absolute path such as `/app`, `/tmp`, `/home` or `/root`. Use a
  relative path, or `tempfile` (which lands in `TMPDIR`) for scratch files.
- **E2** -- Never install packages at run time: no `pip install`, no
  `subprocess` call to pip, no `os.system("pip ...")`. Use only packages
  already in the container. When one is missing, print a `[NEEDS_INFO]` line
  explaining it instead of trying to install it.
- **E3** -- Never depend on user-account information: no `pwd.getpwuid()`, no
  `os.getlogin()`. When a home directory is needed, use `os.environ["HOME"]`
  or `Path.home()`.
- **E4** -- Leave no background process behind and never expect a process to
  survive into the next turn: no `nohup`, no trailing `&`, no `os.fork()`
  without waiting, no `setsid`, no daemon thread used as a service. All work
  finishes before the script exits.
- **E5** -- Every output file is a regular file at the top level of the working
  directory. Never produce a symlink or hard link as output, and never expect a
  file inside a subdirectory to be uploaded.

The content lint warns on E1-E4 when a code block contains an absolute path
under those prefixes, `pip install` / `-m pip`, `getpwuid` / `getlogin`, or
`nohup` / `setsid` / `os.fork` / `start_new_session=True` / `daemon=True`.

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
