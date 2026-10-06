---
name: procurement-requirements-analysis
description: "Analyze an existing procurement case OCR manifest and its local Markdown corpus into a populated, traceable canonical requirements package — atomic requirements with stable IDs, decision statuses, acceptance criteria, glossary, actors, processes, domain states, data dictionary, UI requirements, notifications, integrations, non-functional requirements, exceptions, assumptions, conflicts, and open questions — ready for downstream specification, tracker, and prototype generation. Use for Chinese or English requests to analyze procurement requirements, review an OCR-complete case, or identify evidence, gaps, conflicts, and open questions by case ID; do not use it to retrieve, download, or OCR procurement documents from SharePoint."
metadata:
  author: "a-wureeve@microsoft.com"
  version: "2.0"
  tags:
    - procurement
    - requirements-analysis
    - ocr-corpus
    - traceability
  uses_obo: false
---

## Overview

Reads a local procurement-case manifest and its readable OCR Markdown corpus, extracts atomic requirements and their supporting domain content with verbatim source anchors, validates the result against Quality Gate 1, determines a package readiness state, and writes a canonical requirements package consisting of a human-readable Markdown file and a machine-readable JSON sidecar.

This skill is the single semantic bridge between OCR output and deliverable generation. Everything the downstream deliverables capability can render must exist in this package; anything omitted here is permanently lost to the Word specification, the Excel test tracker, and the HTML prototype.

## When NOT to Use This Skill

Retrieving, downloading, converting, or OCR-processing procurement-case documents from SharePoint -> use `procurement-sharepoint-ocr`.

Generating, validating, or publishing Word, Excel, or HTML deliverables -> use `procurement-deliverables-generator`.

## Required Inputs

```input-bindings
- name: procurement_case_id
  source: request
  required: true
```

- `procurement_case_id` (required): Procurement case identifier used as the single folder name in `Procurement/<case-id>/input` and `Procurement/<case-id>/analysis`. Provide a non-empty string without `/`, `\\`, or the path segments `.` and `..`.
- Ground Rule: Before reading case files or writing the analysis output, ask the user for every missing required input.

## `[NEEDS_INFO]` 契約

- `PROCUREMENT_CASE_ID`: The Coding Agent must obtain a valid procurement case identifier from the current request and bind it to `request_inputs["procurement_case_id"]`. It must be a non-empty single folder name; do not infer it from unrelated request text.

## Environment Variables

None. This skill uses only the local procurement-case directory and does not declare ACA environment variables.

## OBO Token Scopes

None. This skill does not call an external resource and does not declare an OBO token.

---

# Canonical Output Contract

Write exactly:

```text
Procurement/{procurement_case_id}/analysis/
├── canonical-requirements.md      # human-readable canonical package
└── canonical-requirements.json    # machine-readable canonical package
```

Both files describe the same package and must be written atomically in the same run. The Markdown file is the artifact the downstream deliverables capability reads and fingerprints; the JSON sidecar carries the same content in structured form plus the Quality Gate 1 result, the readiness state, and the SHA-256 of the Markdown file.

`canonical-requirements.md` must contain these level-2 headings, in this order, with these exact titles. The downstream capability locates content by heading, so a renamed, reordered, or omitted heading breaks it:

1. `## Package Control`
2. `## Analysis Status`
3. `## Business Background`
4. `## Objectives`
5. `## Scope`
6. `## Glossary`
7. `## Actors`
8. `## Processes`
9. `## Domain States`
10. `## Canonical Requirements`
11. `## Business Rules`
12. `## Data Dictionary`
13. `## UI Requirements`
14. `## Notifications`
15. `## Integrations`
16. `## Non-Functional Requirements`
17. `## Exceptions`
18. `## Acceptance Criteria Index`
19. `## Assumptions`
20. `## Conflicts`
21. `## Open Questions`
22. `## Traceability`
23. `## Source References`
24. `## Quality Gate 1`
25. `## Downstream Readiness`

---

# Downstream Contract

Every downstream artifact section is fed by exactly one canonical section. Treat this table as the definition of "complete enough": if a canonical section is empty while its source corpus contains the corresponding material, the analysis is incomplete and must be redone, not passed downstream.

| Downstream artifact | Downstream section | Fed by canonical section |
|---|---|---|
| Word specification | Document control, review status | `Package Control`, `Analysis Status` |
| Word specification | Business background, objectives, scope | `Business Background`, `Objectives`, `Scope` |
| Word specification | Terminology | `Glossary` |
| Word specification | Actors | `Actors` |
| Word specification | Processes | `Processes` |
| Word specification | Domain states | `Domain States` |
| Word specification | Functional requirements | `Canonical Requirements` |
| Word specification | Business rules | `Business Rules` |
| Word specification | Data dictionary | `Data Dictionary` |
| Word specification | UI requirements | `UI Requirements` |
| Word specification | Notifications | `Notifications` |
| Word specification | Integrations | `Integrations` |
| Word specification | Non-functional requirements | `Non-Functional Requirements` |
| Word specification | Exceptions | `Exceptions` |
| Word specification | Acceptance criteria | `Acceptance Criteria Index` |
| Word specification | Assumptions, conflicts, open questions | `Assumptions`, `Conflicts`, `Open Questions` |
| Word specification | Traceability, source references | `Traceability`, `Source References` |
| Excel `Requirements` sheet | one row per requirement | `Canonical Requirements` |
| Excel `Test Cases` sheet | one row per acceptance criterion | `Acceptance Criteria Index` |
| Excel `Traceability` sheet | requirement to source, screen, and test | `Traceability` |
| Excel `Open Items` sheet | conflicts and open questions | `Conflicts`, `Open Questions` |
| Excel `Reference Data` sheet | code tables and enumerations | `Data Dictionary` |
| Excel `Quality Checks` sheet | gate results | `Quality Gate 1` |
| HTML prototype | `SCR-*` screens and `ELM-*` elements | `UI Requirements` |
| HTML prototype | `FLOW-*` flows | `Processes` |
| HTML prototype | `data-requirements` bindings | `UI Requirements` element bindings |
| HTML prototype | disabled or labelled behavior | requirement status plus `Conflicts`, `Open Questions` |

---

# Requirement Extraction Rules

## R1 — Atomicity

One requirement expresses one verifiable capability or constraint. A source sentence containing several obligations becomes several requirements. Never merge a functional obligation with a non-functional threshold into one requirement; the threshold belongs in `Non-Functional Requirements`.

## R2 — Stable identity

Requirement IDs are `REQ-` plus three digits, assigned in ascending order of first appearance in the corpus reading order (manifest file order, then line order within a file). Once assigned in a run, an ID is never reused for different content within that run.

When the source corpus itself carries requirement identifiers (for example `FR-014`), record them in `source_ids` and keep the canonical `REQ-` ID as the primary key. Never adopt the source identifier as the canonical ID: source identifiers are not guaranteed unique across documents.

## R3 — Evidence before statement

Every requirement must carry at least one source anchor in the exact form `{file_ref} / {section_id} / lines {start}-{end}` and a `verbatim_evidence` string copied character-for-character from that section's Markdown. The validator re-reads the section and rejects the package if the evidence is not found there. Do not paraphrase into `verbatim_evidence`; paraphrase belongs in `statement`.

## R4 — Status discipline

| Status | Meaning | Required conditions |
|---|---|---|
| `confirmed` | Directly supported by source evidence, with no unresolved dependency | `blocked_by_conflicts` and `depends_on_open_questions` are both empty |
| `conditional` | Supported by evidence but dependent on an unresolved open question | at least one existing `OQ-*` in `depends_on_open_questions`, no `CON-*` |
| `blocked` | Sources disagree; the requirement cannot be stated definitively | at least one existing `CON-*` in `blocked_by_conflicts` |

Never promote a requirement to `confirmed` because it seems reasonable, because it is common practice, or because the rest of the document implies it. Absence of contradiction is not confirmation.

## R5 — Acceptance criteria

Each acceptance criterion has an ID of the form `AC-{requirement digits}-{sequence}`, and the fields `given`, `when`, `then`.

- For a `confirmed` and `testable` requirement, `then` must be a definitive, observable expected result containing the concrete value the source states (a number, a status name, an exact message, a field state). A `then` such as "系統正確處理" is not definitive and fails Quality Gate 1.
- For a `conditional` or `blocked` requirement, `then` must be `null`, and `then_blocked_by` must name the `OQ-*` or `CON-*` that prevents it. This is what allows the downstream tracker to emit `TBD - blocked by OQ-001` or `BLOCKED - unresolved CON-001` instead of inventing an expected result.

## R6 — UI coverage

A requirement whose behavior is visible to a user sets `ui_relevant: true`. Every such `confirmed` requirement must either be bound by at least one `ELM-*` element in `UI Requirements`, or appear in `ui_exclusions` with a written reason. This is the upstream half of downstream check QG2-005.

## R7 — Non-invention

Do not create glossary terms, actors, states, fields, screens, notifications, integrations, thresholds, or exceptions that the corpus does not state. An empty section is a truthful finding and is reported as a coverage gap; a fabricated section is a defect that propagates into a published document.

## R8 — Conflicts and open questions are first-class

A conflict records two or more positions, each with its own source anchor, plus the affected requirement IDs and the decision required. An open question records the unanswered question, the affected requirement IDs, and — when the corpus names them — the owner and the due date. Both must be discoverable from the requirements that depend on them and vice versa; the validator checks the link in both directions.

---

# Canonical Package Schema

The Coding Agent authors `analysis_package` as a Python literal after reading the corpus. Field names are fixed; the validator rejects unknown or missing keys.

```text
case_id                str
package_title          str
business_background    [ {statement, source_anchors[]} ]
objectives             [ {id, statement, success_metric|null, source_anchors[]} ]
scope_in               [ {statement, source_anchors[]} ]
scope_out              [ {statement, source_anchors[]} ]
glossary               [ {term, definition, source_anchors[]} ]
actors                 [ {id, name, description, source_anchors[]} ]
processes              [ {id, name, description, steps[], source_anchors[]} ]
domain_states          [ {entity, code, name, description, transitions_to[], source_anchors[]} ]
requirements           [ {id, source_ids[], title, statement, category, priority,
                          status, testable, ui_relevant,
                          business_rule_ids[], depends_on_open_questions[],
                          blocked_by_conflicts[],
                          acceptance_criteria[ {id, given, when, then, then_blocked_by} ],
                          source_anchors[], verbatim_evidence} ]
business_rules         [ {id, statement, applies_to[], source_anchors[]} ]
data_dictionary        [ {entity, field, data_type, length, required, rule,
                          example, source_anchors[]} ]
code_tables            [ {table_id, name, entries[ {code, label} ], source_anchors[]} ]
ui_requirements        [ {screen_id, name, primary_actors[], layout,
                          elements[ {element_id, label, control_type,
                                     bound_requirements[], blocked_by} ],
                          source_anchors[]} ]
ui_exclusions          [ {requirement_id, reason} ]
notifications          [ {id, trigger, recipients, channel, subject_pattern,
                          bound_requirements[], source_anchors[]} ]
integrations           [ {id, system, purpose, protocol, request_fields[],
                          response_fields[], timeout, failure_behavior,
                          bound_requirements[], source_anchors[]} ]
non_functional         [ {id, category, statement, measurement, threshold,
                          source_anchors[]} ]
exceptions             [ {id, scenario, system_behavior, user_message,
                          bound_requirements[], source_anchors[]} ]
assumptions            [ {id, statement, rationale, source_anchors[]} ]
conflicts              [ {id, topic, positions[ {position, source_anchors[]} ],
                          affected_requirements[], decision_required,
                          owner|null} ]
open_questions         [ {id, question, affected_requirements[], owner|null,
                          due|null, source_anchors[]} ]
analyst_notes          [ str ]
```

ID formats: `OBJ-\d{3}`, `ACT-\d{2}`, `FLOW-\d{2}`, `REQ-\d{3}`, `BR-\d{3}`, `SCR-\d{2}`, `ELM-\d{3}`, `NTF-\d{3}`, `INT-\d{3}`, `NFR-\d{3}`, `EXC-\d{3}`, `ASM-\d{3}`, `CON-\d{3}`, `OQ-\d{3}`.

---

# Quality Gate 1

Quality Gate 1 runs against the authored package before anything is written. Its result is recorded in both output files.

| Check | Rule | Severity |
|---|---|---|
| `QG1-001` | Every requirement has at least one source anchor, and every anchor resolves to an existing manifest section with a valid line range | fail |
| `QG1-002` | Every requirement ID matches `REQ-\d{3}` and is unique | fail |
| `QG1-003` | Every requirement status is `confirmed`, `conditional`, or `blocked` | fail |
| `QG1-004` | No `confirmed` requirement references an open question or a conflict | fail |
| `QG1-005` | Every `conditional` requirement references at least one existing open question; every `blocked` requirement references at least one existing conflict | fail |
| `QG1-006` | Every `confirmed` and `testable` requirement has at least one acceptance criterion whose `then` is non-empty and contains a concrete observable value | fail |
| `QG1-007` | No acceptance criterion of a `conditional` or `blocked` requirement carries a non-null `then`; each carries `then_blocked_by` naming an existing `OQ-*` or `CON-*` | fail |
| `QG1-008` | Every `confirmed` `ui_relevant` requirement is bound by at least one UI element or listed in `ui_exclusions` with a reason | fail |
| `QG1-009` | Every ID referenced anywhere in the package exists, in both directions between requirements and their conflicts, open questions, business rules, screens, notifications, integrations and exceptions | fail |
| `QG1-010` | Every `verbatim_evidence` string is found in at least one of that requirement's anchored sections after whitespace normalization | fail |
| `QG1-011` | The package contains at least one `confirmed` requirement | fail |
| `QG1-012` | No section contains template or placeholder text such as `TODO`, `TBD`, `lorem`, `範例`, `請填寫`, `xxx` | fail |
| `QG1-013` | Every readable manifest file contributes at least one source anchor somewhere in the package | warn |
| `QG1-014` | `glossary`, `actors`, `processes`, and `domain_states` are each non-empty | warn |
| `QG1-015` | `data_dictionary`, `ui_requirements`, `notifications`, `integrations`, `non_functional`, and `exceptions` are each non-empty | warn |
| `QG1-016` | Every objective carries a success metric | warn |

Result values: `FAILED` when any `fail` check fails; `PASSED_WITH_WARNINGS` when only `warn` checks fail; `PASSED` otherwise.

A `FAILED` gate must not write a canonical package. Write the gate report to stdout and end non-zero, so the orchestrating workflow stops before the publication stage.

---

# Readiness Determination

Readiness is computed, never asserted by the Coding Agent.

| Readiness | Condition |
|---|---|
| `BLOCKED` | Quality Gate 1 is `FAILED`, or the package contains at least one `blocked` requirement, or there are zero `confirmed` requirements |
| `CONDITIONAL` | Not blocked, and the package contains at least one `conditional` requirement, or at least one unresolved conflict or open question |
| `READY_FOR_HUMAN_REVIEW` | Not blocked and not conditional |

Readiness appears in `## Package Control`, in `## Downstream Readiness`, and in the JSON sidecar. The downstream capability reads it to choose between refusing to generate, producing conditional drafts, and producing review drafts.

---

# Execution Workflow

Run in two passes. The first pass reads; the second pass writes. Do not attempt to author the package before reading the corpus.

**Pass 1 — corpus reading.** Instantiate `CaseDocumentReader` against `Procurement/{case_id}/input`, call `list_files()`, then `read_document(ref)` for every file whose status is `ok`. Read whole documents, not search snippets: `search()` is for locating a specific term after the full read, not for sampling the corpus. Record, for each candidate requirement, the file ref, section ID, line range, and the exact sentence that supports it.

**Pass 2 — authoring and writing.** Fill the `analysis_package` literal. Run the script. It validates, computes readiness, renders both files, writes them atomically, prints the gate report and the Markdown SHA-256, and exits.

Two rules about scale. Do not stop extracting because the corpus is long; a partial extraction that silently drops half the source is worse than a slow run. And do not inflate the package to look thorough: one requirement per source obligation, no more.

# Failure Behavior

| Condition | Output | Exit |
|---|---|---|
| Missing or invalid `procurement_case_id` | `[NEEDS_INFO] missing=PROCUREMENT_CASE_ID` plus guidance | 0 |
| Manifest missing, malformed, or OCR Markdown corpus unusable | `[CORPUS_UNUSABLE]` plus the underlying reason | non-zero |
| Quality Gate 1 `FAILED` | `[QG1_FAILED]` plus every failed check | non-zero |
| Package written | gate report, readiness, output paths, Markdown SHA-256 | 0 |

An unusable corpus and a failed gate are not user-input handshakes. Never emit `[NEEDS_INFO]` for them, and never exit zero: the orchestrating workflow relies on the non-zero exit to stop before generating and publishing deliverables.

---

## API Reference / Sample Code

```python
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CANONICAL_SCHEMA = "procurement-canonical-requirements/1.0"
REQUIREMENT_ID_PATTERN = re.compile(r"^REQ-\d{3}$")
ANCHOR_PATTERN = re.compile(
    r"^(?P<ref>\S+)\s*/\s*(?P<section>\S+)\s*/\s*lines\s+(?P<start>\d+)-(?P<end>\d+)$"
)
ALLOWED_STATUSES = {"confirmed", "conditional", "blocked"}
PLACEHOLDER_PATTERN = re.compile(
    r"(?i)\b(todo|tbd|lorem ipsum|placeholder|xxx+)\b|請填寫|待填|範例文字"
)
VAGUE_EXPECTED_RESULTS = (
    "正確處理",
    "運作正常",
    "符合預期",
    "沒有問題",
    "works correctly",
    "behaves as expected",
)


class ManifestError(ValueError):
    pass


class CanonicalError(ValueError):
    pass


# ---------------------------------------------------------------- corpus reader


class CaseDocumentReader:
    def __init__(self, case_dir):
        self.case_dir = Path(case_dir).resolve()
        manifest_path = self.case_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ManifestError(f"Manifest not found: {manifest_path}")

        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self._validate_manifest()
        self.files = {item["ref"]: item for item in self.manifest["files"]}
        self._section_cache = {}

    def _validate_manifest(self):
        if not isinstance(self.manifest.get("meta"), dict):
            raise ManifestError("Manifest meta must be an object")
        if not isinstance(self.manifest.get("files"), list):
            raise ManifestError("Manifest files must be an array")
        if not isinstance(self.manifest.get("warnings", []), list):
            raise ManifestError("Manifest warnings must be an array")

        seen_refs = set()
        seen_sections = set()
        for item in self.manifest["files"]:
            file_ref = item.get("ref")
            if not isinstance(file_ref, str) or not file_ref:
                raise ManifestError("Every file must have a non-empty ref")
            if file_ref in seen_refs:
                raise ManifestError(f"Duplicate file ref: {file_ref}")
            seen_refs.add(file_ref)

            for section in item.get("sections", []):
                section_id = section.get("id")
                if not isinstance(section_id, str) or not section_id:
                    raise ManifestError(f"Invalid section ID in {file_ref}")
                if section_id in seen_sections:
                    raise ManifestError(f"Duplicate section ID: {section_id}")
                seen_sections.add(section_id)

    def get_manifest(self):
        return self.manifest

    def list_files(self):
        return [
            {
                "ref": item["ref"],
                "name": item.get("name"),
                "status": item.get("status"),
                "md": item.get("md"),
                "sections": len(item.get("sections", [])),
                "detail": item.get("detail"),
            }
            for item in self.manifest["files"]
        ]

    def get_file(self, file_ref):
        try:
            return self.files[file_ref]
        except KeyError as error:
            raise KeyError(f"Unknown file ref: {file_ref}") from error

    def _markdown_path(self, item):
        if item.get("status") != "ok" or not item.get("md"):
            raise ManifestError(f"File {item['ref']} has no readable Markdown")

        markdown_path = (self.case_dir / item["md"]).resolve()
        try:
            markdown_path.relative_to(self.case_dir)
        except ValueError as error:
            raise ManifestError(f"Unsafe Markdown path for {item['ref']}") from error
        if not markdown_path.is_file():
            raise ManifestError(f"Markdown not found: {markdown_path}")
        return markdown_path

    def read_document(self, file_ref):
        item = self.get_file(file_ref)
        content = self._markdown_path(item).read_text(encoding="utf-8")
        return {
            "ref": file_ref,
            "name": item.get("name"),
            "md": item.get("md"),
            "content": content,
        }

    def read_section(self, section_id):
        if section_id in self._section_cache:
            return self._section_cache[section_id]

        for item in self.manifest["files"]:
            for section in item.get("sections", []):
                if section.get("id") != section_id:
                    continue

                lines = self._markdown_path(item).read_text(encoding="utf-8").splitlines()
                start_line, end_line = section["lines"]
                if start_line < 1 or end_line < start_line or end_line > len(lines):
                    raise ManifestError(f"Invalid line range for {section_id}")
                result = {
                    "ref": item["ref"],
                    "section_id": section_id,
                    "name": item.get("name"),
                    "heading": section.get("heading"),
                    "lines": [start_line, end_line],
                    "content": "\n".join(lines[start_line - 1 : end_line]),
                }
                self._section_cache[section_id] = result
                return result
        raise KeyError(f"Unknown section ID: {section_id}")

    def search(self, query, max_results=10):
        normalized_query = query.casefold().strip()
        if not normalized_query:
            raise ValueError("Search query cannot be empty")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        results = []
        for item in self.manifest["files"]:
            if item.get("status") != "ok":
                continue
            for section in item.get("sections", []):
                section_data = self.read_section(section["id"])
                content = section_data["content"]
                match_index = content.casefold().find(normalized_query)
                if match_index < 0:
                    continue

                snippet_start = max(0, match_index - 100)
                snippet_end = min(len(content), match_index + len(query) + 180)
                snippet = re.sub(r"\s+", " ", content[snippet_start:snippet_end]).strip()
                results.append(
                    {
                        "ref": item["ref"],
                        "section_id": section["id"],
                        "name": item.get("name"),
                        "heading": section.get("heading"),
                        "lines": section.get("lines"),
                        "snippet": snippet,
                    }
                )
                if len(results) >= max_results:
                    return results
        return results


# ---------------------------------------------------------------- helpers


def normalize(text):
    return re.sub(r"\s+", "", str(text or ""))


def write_text_atomically(output_path, content):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f"{output_path.name}.part")
    temporary_path.write_text(content, encoding="utf-8", newline="\n")
    temporary_path.replace(output_path)


def collect_ids(package):
    return {
        "requirement": {item["id"] for item in package["requirements"]},
        "business_rule": {item["id"] for item in package["business_rules"]},
        "conflict": {item["id"] for item in package["conflicts"]},
        "open_question": {item["id"] for item in package["open_questions"]},
        "screen": {item["screen_id"] for item in package["ui_requirements"]},
    }


def iter_source_anchors(package):
    for item in package["requirements"]:
        for anchor in item.get("source_anchors", []):
            yield anchor
    for key in (
        "business_background",
        "objectives",
        "scope_in",
        "scope_out",
        "glossary",
        "actors",
        "processes",
        "domain_states",
        "business_rules",
        "data_dictionary",
        "code_tables",
        "ui_requirements",
        "notifications",
        "integrations",
        "non_functional",
        "exceptions",
        "assumptions",
        "open_questions",
    ):
        for item in package.get(key, []):
            for anchor in item.get("source_anchors", []):
                yield anchor
    for item in package.get("conflicts", []):
        for position in item.get("positions", []):
            for anchor in position.get("source_anchors", []):
                yield anchor


# ---------------------------------------------------------------- quality gate 1


class QualityGateOne:
    def __init__(self, reader, package):
        self.reader = reader
        self.package = package
        self.failures = []
        self.warnings = []

    def _fail(self, check, detail):
        self.failures.append({"check": check, "detail": detail})

    def _warn(self, check, detail):
        self.warnings.append({"check": check, "detail": detail})

    def _resolve_anchor(self, anchor):
        match = ANCHOR_PATTERN.match(str(anchor).strip())
        if not match:
            return None, f"Malformed anchor: {anchor}"
        try:
            section = self.reader.read_section(match.group("section"))
        except (KeyError, ManifestError) as error:
            return None, f"Unresolvable anchor {anchor}: {error}"
        if section["ref"] != match.group("ref"):
            return None, f"Anchor {anchor} names the wrong file ref"
        start, end = int(match.group("start")), int(match.group("end"))
        if [start, end] != section["lines"]:
            return None, f"Anchor {anchor} line range does not match the manifest"
        return section, None

    def run(self):
        ids = collect_ids(self.package)
        self._check_anchors_and_ids(ids)
        self._check_statuses(ids)
        self._check_acceptance_criteria()
        self._check_ui_coverage()
        self._check_cross_references(ids)
        self._check_evidence()
        self._check_placeholders()
        self._check_coverage()

        if self.failures:
            result = "FAILED"
        elif self.warnings:
            result = "PASSED_WITH_WARNINGS"
        else:
            result = "PASSED"
        return {
            "result": result,
            "failures": self.failures,
            "warnings": self.warnings,
        }

    def _check_anchors_and_ids(self, ids):
        seen = set()
        for item in self.package["requirements"]:
            requirement_id = item.get("id", "")
            if not REQUIREMENT_ID_PATTERN.match(str(requirement_id)):
                self._fail("QG1-002", f"Invalid requirement ID: {requirement_id!r}")
            if requirement_id in seen:
                self._fail("QG1-002", f"Duplicate requirement ID: {requirement_id}")
            seen.add(requirement_id)

            anchors = item.get("source_anchors") or []
            if not anchors:
                self._fail("QG1-001", f"{requirement_id} has no source anchor")
            for anchor in anchors:
                _, error = self._resolve_anchor(anchor)
                if error:
                    self._fail("QG1-001", f"{requirement_id}: {error}")

        for anchor in iter_source_anchors(self.package):
            _, error = self._resolve_anchor(anchor)
            if error:
                self._fail("QG1-001", error)

        if not any(
            item.get("status") == "confirmed" for item in self.package["requirements"]
        ):
            self._fail("QG1-011", "The package contains no confirmed requirement")

    def _check_statuses(self, ids):
        for item in self.package["requirements"]:
            requirement_id = item.get("id")
            status = item.get("status")
            if status not in ALLOWED_STATUSES:
                self._fail("QG1-003", f"{requirement_id} has invalid status {status!r}")
                continue

            open_questions = item.get("depends_on_open_questions") or []
            conflicts = item.get("blocked_by_conflicts") or []

            if status == "confirmed" and (open_questions or conflicts):
                self._fail(
                    "QG1-004",
                    f"{requirement_id} is confirmed but references "
                    f"{open_questions + conflicts}",
                )
            if status == "conditional" and not open_questions:
                self._fail(
                    "QG1-005",
                    f"{requirement_id} is conditional but names no open question",
                )
            if status == "blocked" and not conflicts:
                self._fail(
                    "QG1-005", f"{requirement_id} is blocked but names no conflict"
                )

    def _check_acceptance_criteria(self):
        for item in self.package["requirements"]:
            requirement_id = item.get("id")
            status = item.get("status")
            criteria = item.get("acceptance_criteria") or []

            if status == "confirmed" and item.get("testable"):
                definitive = [
                    criterion
                    for criterion in criteria
                    if str(criterion.get("then") or "").strip()
                ]
                if not definitive:
                    self._fail(
                        "QG1-006",
                        f"{requirement_id} is confirmed and testable but has no "
                        f"acceptance criterion with a definitive expected result",
                    )
                for criterion in definitive:
                    expected = str(criterion.get("then"))
                    if any(vague in expected for vague in VAGUE_EXPECTED_RESULTS):
                        self._fail(
                            "QG1-006",
                            f"{criterion.get('id')} expected result is not "
                            f"observable: {expected!r}",
                        )

            if status in {"conditional", "blocked"}:
                for criterion in criteria:
                    if str(criterion.get("then") or "").strip():
                        self._fail(
                            "QG1-007",
                            f"{criterion.get('id')} carries a definitive expected "
                            f"result although {requirement_id} is {status}",
                        )
                    if not str(criterion.get("then_blocked_by") or "").strip():
                        self._fail(
                            "QG1-007",
                            f"{criterion.get('id')} does not name the blocking "
                            f"OQ or CON",
                        )

    def _check_ui_coverage(self):
        bound = set()
        for screen in self.package["ui_requirements"]:
            for element in screen.get("elements", []):
                bound.update(element.get("bound_requirements") or [])
        excluded = {
            item.get("requirement_id") for item in self.package.get("ui_exclusions", [])
        }
        for item in self.package["requirements"]:
            if not item.get("ui_relevant") or item.get("status") != "confirmed":
                continue
            requirement_id = item.get("id")
            if requirement_id in bound:
                continue
            if requirement_id in excluded:
                continue
            self._fail(
                "QG1-008",
                f"{requirement_id} is UI-relevant and confirmed but is neither "
                f"bound by a UI element nor listed in ui_exclusions",
            )
        for exclusion in self.package.get("ui_exclusions", []):
            if not str(exclusion.get("reason") or "").strip():
                self._fail(
                    "QG1-008",
                    f"ui_exclusions entry for {exclusion.get('requirement_id')} "
                    f"has no reason",
                )

    def _check_cross_references(self, ids):
        def require(kind, referenced, context):
            for value in referenced or []:
                if value not in ids[kind]:
                    self._fail("QG1-009", f"{context} references unknown {kind} {value}")

        for item in self.package["requirements"]:
            context = item.get("id")
            require("open_question", item.get("depends_on_open_questions"), context)
            require("conflict", item.get("blocked_by_conflicts"), context)
            require("business_rule", item.get("business_rule_ids"), context)

        for item in self.package["conflicts"]:
            require("requirement", item.get("affected_requirements"), item.get("id"))
            if not item.get("affected_requirements"):
                self._fail("QG1-009", f"{item.get('id')} affects no requirement")
        for item in self.package["open_questions"]:
            require("requirement", item.get("affected_requirements"), item.get("id"))
            if not item.get("affected_requirements"):
                self._fail("QG1-009", f"{item.get('id')} affects no requirement")

        for key, field in (
            ("ui_requirements", "elements"),
            ("notifications", None),
            ("integrations", None),
            ("exceptions", None),
        ):
            for item in self.package.get(key, []):
                if field:
                    for element in item.get(field, []):
                        require(
                            "requirement",
                            element.get("bound_requirements"),
                            element.get("element_id"),
                        )
                else:
                    require(
                        "requirement", item.get("bound_requirements"), item.get("id")
                    )

        for item in self.package["business_rules"]:
            require("requirement", item.get("applies_to"), item.get("id"))

    def _check_evidence(self):
        for item in self.package["requirements"]:
            evidence = normalize(item.get("verbatim_evidence"))
            requirement_id = item.get("id")
            if not evidence:
                self._fail("QG1-010", f"{requirement_id} has empty verbatim_evidence")
                continue
            found = False
            for anchor in item.get("source_anchors") or []:
                section, error = self._resolve_anchor(anchor)
                if error or section is None:
                    continue
                if evidence in normalize(section["content"]):
                    found = True
                    break
            if not found:
                self._fail(
                    "QG1-010",
                    f"{requirement_id} verbatim_evidence was not found in any of "
                    f"its anchored sections",
                )

    def _check_placeholders(self):
        serialized = json.dumps(self.package, ensure_ascii=False)
        for match in set(PLACEHOLDER_PATTERN.findall(serialized)):
            if match:
                self._fail("QG1-012", f"Placeholder text present in package: {match!r}")

    def _check_coverage(self):
        anchored_refs = set()
        for anchor in iter_source_anchors(self.package):
            match = ANCHOR_PATTERN.match(str(anchor).strip())
            if match:
                anchored_refs.add(match.group("ref"))
        for item in self.reader.list_files():
            if item["status"] == "ok" and item["ref"] not in anchored_refs:
                self._warn(
                    "QG1-013",
                    f"{item['ref']} ({item['name']}) contributed no source anchor",
                )

        for key in ("glossary", "actors", "processes", "domain_states"):
            if not self.package.get(key):
                self._warn("QG1-014", f"{key} is empty")
        for key in (
            "data_dictionary",
            "ui_requirements",
            "notifications",
            "integrations",
            "non_functional",
            "exceptions",
        ):
            if not self.package.get(key):
                self._warn("QG1-015", f"{key} is empty")
        for objective in self.package.get("objectives", []):
            if not str(objective.get("success_metric") or "").strip():
                self._warn(
                    "QG1-016", f"{objective.get('id')} carries no success metric"
                )


# ---------------------------------------------------------------- readiness


def determine_readiness(package, gate):
    statuses = [item.get("status") for item in package["requirements"]]
    if (
        gate["result"] == "FAILED"
        or "blocked" in statuses
        or "confirmed" not in statuses
    ):
        return "BLOCKED"
    if (
        "conditional" in statuses
        or package.get("conflicts")
        or package.get("open_questions")
    ):
        return "CONDITIONAL"
    return "READY_FOR_HUMAN_REVIEW"


# ---------------------------------------------------------------- rendering


def render_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        cells = [str(cell if cell is not None else "").replace("|", "\\|") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_anchor_list(anchors):
    return "; ".join(f"`{anchor}`" for anchor in anchors or []) or "—"


def render_canonical_markdown(package, manifest, gate, readiness, reader, generated_utc):
    case_id = package["case_id"]
    requirements = package["requirements"]
    counts = {
        status: sum(1 for item in requirements if item.get("status") == status)
        for status in ALLOWED_STATUSES
    }
    unreadable = [item for item in reader.list_files() if item["status"] != "ok"]

    lines = [
        f"# Canonical Requirements Package: {case_id}",
        "",
        "## Package Control",
        "",
    ]
    lines += render_table(
        ["Field", "Value"],
        [
            ["Schema", CANONICAL_SCHEMA],
            ["Case ID", case_id],
            ["Package title", package.get("package_title") or case_id],
            ["Generated (UTC)", generated_utc],
            ["Source manifest", manifest["meta"].get("source_folder", "")],
            ["OCR analyzer", manifest["meta"].get("analyzer", "")],
            ["Readiness", readiness],
            ["Quality Gate 1", gate["result"]],
        ],
    )

    lines += [
        "",
        "## Analysis Status",
        "",
        f"- Readable documents reviewed: {len([f for f in reader.list_files() if f['status'] == 'ok'])}.",
        f"- Unreadable or failed manifest files: {len(unreadable)}.",
        f"- Requirements extracted: {len(requirements)} "
        f"(confirmed {counts['confirmed']}, conditional {counts['conditional']}, "
        f"blocked {counts['blocked']}).",
        f"- Conflicts: {len(package['conflicts'])}. "
        f"Open questions: {len(package['open_questions'])}.",
        "- Only requirements with status `confirmed` are settled. `conditional` and "
        "`blocked` requirements must not be rendered downstream as definitive.",
    ]
    for item in unreadable:
        lines.append(
            f"- Unreadable: `{item['ref']}` — {item['name'] or '(unnamed file)'}: "
            f"{item.get('detail') or 'no detail recorded'}."
        )

    lines += ["", "## Business Background", ""]
    for item in package["business_background"]:
        lines.append(f"- {item['statement']} {render_anchor_list(item['source_anchors'])}")

    lines += ["", "## Objectives", ""]
    lines += render_table(
        ["ID", "Objective", "Success metric", "Source"],
        [
            [
                item["id"],
                item["statement"],
                item.get("success_metric") or "—",
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["objectives"]
        ],
    )

    lines += ["", "## Scope", "", "### In Scope", ""]
    for item in package["scope_in"]:
        lines.append(f"- {item['statement']} {render_anchor_list(item['source_anchors'])}")
    lines += ["", "### Out of Scope", ""]
    for item in package["scope_out"]:
        lines.append(f"- {item['statement']} {render_anchor_list(item['source_anchors'])}")

    lines += ["", "## Glossary", ""]
    lines += render_table(
        ["Term", "Definition", "Source"],
        [
            [item["term"], item["definition"], render_anchor_list(item.get("source_anchors"))]
            for item in package["glossary"]
        ],
    )

    lines += ["", "## Actors", ""]
    lines += render_table(
        ["ID", "Actor", "Description", "Source"],
        [
            [
                item["id"],
                item["name"],
                item["description"],
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["actors"]
        ],
    )

    lines += ["", "## Processes", ""]
    for item in package["processes"]:
        lines += [
            f"### {item['id']} — {item['name']}",
            "",
            item["description"],
            "",
        ]
        for index, step in enumerate(item.get("steps", []), start=1):
            lines.append(f"{index}. {step}")
        lines += ["", f"Source: {render_anchor_list(item.get('source_anchors'))}", ""]

    lines += ["", "## Domain States", ""]
    lines += render_table(
        ["Entity", "Code", "Name", "Description", "Transitions to", "Source"],
        [
            [
                item["entity"],
                item["code"],
                item["name"],
                item["description"],
                ", ".join(item.get("transitions_to", [])) or "—",
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["domain_states"]
        ],
    )

    lines += ["", "## Canonical Requirements", ""]
    for item in requirements:
        lines += [
            f"### {item['id']} — {item['title']}",
            "",
        ]
        lines += render_table(
            ["Attribute", "Value"],
            [
                ["Status", item["status"]],
                ["Category", item.get("category") or "—"],
                ["Priority", item.get("priority") or "—"],
                ["Testable", "yes" if item.get("testable") else "no"],
                ["UI relevant", "yes" if item.get("ui_relevant") else "no"],
                ["Source IDs", ", ".join(item.get("source_ids") or []) or "—"],
                ["Business rules", ", ".join(item.get("business_rule_ids") or []) or "—"],
                [
                    "Depends on open questions",
                    ", ".join(item.get("depends_on_open_questions") or []) or "—",
                ],
                [
                    "Blocked by conflicts",
                    ", ".join(item.get("blocked_by_conflicts") or []) or "—",
                ],
                ["Source anchors", render_anchor_list(item.get("source_anchors"))],
            ],
        )
        lines += [
            "",
            f"**Statement.** {item['statement']}",
            "",
            f"**Verbatim evidence.** > {item['verbatim_evidence']}",
            "",
        ]
        criteria = item.get("acceptance_criteria") or []
        if criteria:
            lines += render_table(
                ["AC ID", "Given", "When", "Then", "Blocked by"],
                [
                    [
                        criterion["id"],
                        criterion.get("given"),
                        criterion.get("when"),
                        criterion.get("then")
                        if criterion.get("then")
                        else "(no definitive expected result)",
                        criterion.get("then_blocked_by") or "—",
                    ]
                    for criterion in criteria
                ],
            )
            lines.append("")

    lines += ["", "## Business Rules", ""]
    lines += render_table(
        ["ID", "Rule", "Applies to", "Source"],
        [
            [
                item["id"],
                item["statement"],
                ", ".join(item.get("applies_to") or []) or "—",
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["business_rules"]
        ],
    )

    lines += ["", "## Data Dictionary", ""]
    lines += render_table(
        ["Entity", "Field", "Type", "Length", "Required", "Rule", "Example", "Source"],
        [
            [
                item["entity"],
                item["field"],
                item.get("data_type"),
                item.get("length"),
                "yes" if item.get("required") else "no",
                item.get("rule"),
                item.get("example"),
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["data_dictionary"]
        ],
    )

    if package.get("code_tables"):
        lines += ["", "### Code Tables", ""]
        for table in package["code_tables"]:
            lines += [f"**{table['table_id']} — {table['name']}**", ""]
            lines += render_table(
                ["Code", "Label"],
                [[entry["code"], entry["label"]] for entry in table.get("entries", [])],
            )
            lines += ["", f"Source: {render_anchor_list(table.get('source_anchors'))}", ""]

    lines += ["", "## UI Requirements", ""]
    for screen in package["ui_requirements"]:
        lines += [
            f"### {screen['screen_id']} — {screen['name']}",
            "",
            f"- Primary actors: {', '.join(screen.get('primary_actors') or []) or '—'}",
            f"- Layout: {screen.get('layout') or '—'}",
            f"- Source: {render_anchor_list(screen.get('source_anchors'))}",
            "",
        ]
        lines += render_table(
            ["Element", "Label", "Control", "Bound requirements", "Blocked by"],
            [
                [
                    element["element_id"],
                    element.get("label"),
                    element.get("control_type"),
                    ", ".join(element.get("bound_requirements") or []) or "—",
                    element.get("blocked_by") or "—",
                ]
                for element in screen.get("elements", [])
            ],
        )
        lines.append("")

    if package.get("ui_exclusions"):
        lines += ["### UI Exclusions", ""]
        lines += render_table(
            ["Requirement", "Reason"],
            [
                [item["requirement_id"], item["reason"]]
                for item in package["ui_exclusions"]
            ],
        )

    lines += ["", "## Notifications", ""]
    lines += render_table(
        ["ID", "Trigger", "Recipients", "Channel", "Subject", "Requirements", "Source"],
        [
            [
                item["id"],
                item["trigger"],
                item["recipients"],
                item.get("channel"),
                item.get("subject_pattern"),
                ", ".join(item.get("bound_requirements") or []) or "—",
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["notifications"]
        ],
    )

    lines += ["", "## Integrations", ""]
    for item in package["integrations"]:
        lines += [f"### {item['id']} — {item['system']}", ""]
        lines += render_table(
            ["Attribute", "Value"],
            [
                ["Purpose", item.get("purpose")],
                ["Protocol", item.get("protocol")],
                ["Request fields", ", ".join(item.get("request_fields") or []) or "—"],
                ["Response fields", ", ".join(item.get("response_fields") or []) or "—"],
                ["Timeout", item.get("timeout")],
                ["Failure behavior", item.get("failure_behavior")],
                [
                    "Bound requirements",
                    ", ".join(item.get("bound_requirements") or []) or "—",
                ],
                ["Source", render_anchor_list(item.get("source_anchors"))],
            ],
        )
        lines.append("")

    lines += ["", "## Non-Functional Requirements", ""]
    lines += render_table(
        ["ID", "Category", "Statement", "Measurement", "Threshold", "Source"],
        [
            [
                item["id"],
                item["category"],
                item["statement"],
                item.get("measurement"),
                item.get("threshold"),
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["non_functional"]
        ],
    )

    lines += ["", "## Exceptions", ""]
    lines += render_table(
        ["ID", "Scenario", "System behavior", "User message", "Requirements", "Source"],
        [
            [
                item["id"],
                item["scenario"],
                item["system_behavior"],
                item.get("user_message"),
                ", ".join(item.get("bound_requirements") or []) or "—",
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["exceptions"]
        ],
    )

    lines += ["", "## Acceptance Criteria Index", ""]
    criteria_rows = []
    for item in requirements:
        for criterion in item.get("acceptance_criteria") or []:
            criteria_rows.append(
                [
                    criterion["id"],
                    item["id"],
                    item["status"],
                    criterion.get("given"),
                    criterion.get("when"),
                    criterion.get("then") or "(no definitive expected result)",
                    criterion.get("then_blocked_by") or "—",
                ]
            )
    lines += render_table(
        ["AC ID", "Requirement", "Status", "Given", "When", "Then", "Blocked by"],
        criteria_rows,
    )

    lines += ["", "## Assumptions", ""]
    lines += render_table(
        ["ID", "Assumption", "Rationale", "Source"],
        [
            [
                item["id"],
                item["statement"],
                item.get("rationale"),
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["assumptions"]
        ],
    )

    lines += ["", "## Conflicts", ""]
    for item in package["conflicts"]:
        lines += [f"### {item['id']} — {item['topic']}", ""]
        for index, position in enumerate(item.get("positions", []), start=1):
            lines.append(
                f"{index}. {position['position']} "
                f"{render_anchor_list(position.get('source_anchors'))}"
            )
        lines += [
            "",
            f"- Affected requirements: {', '.join(item.get('affected_requirements') or [])}",
            f"- Decision required: {item.get('decision_required')}",
            f"- Owner: {item.get('owner') or 'not stated in the corpus'}",
            "",
        ]

    lines += ["", "## Open Questions", ""]
    lines += render_table(
        ["ID", "Question", "Affected requirements", "Owner", "Due", "Source"],
        [
            [
                item["id"],
                item["question"],
                ", ".join(item.get("affected_requirements") or []),
                item.get("owner") or "not stated in the corpus",
                item.get("due") or "not stated in the corpus",
                render_anchor_list(item.get("source_anchors")),
            ]
            for item in package["open_questions"]
        ],
    )

    lines += ["", "## Traceability", ""]
    screen_index = {}
    for screen in package["ui_requirements"]:
        for element in screen.get("elements", []):
            for requirement_id in element.get("bound_requirements") or []:
                screen_index.setdefault(requirement_id, set()).add(screen["screen_id"])
    lines += render_table(
        ["Requirement", "Status", "Source anchors", "Screens", "Acceptance criteria"],
        [
            [
                item["id"],
                item["status"],
                render_anchor_list(item.get("source_anchors")),
                ", ".join(sorted(screen_index.get(item["id"], []))) or "—",
                ", ".join(
                    criterion["id"] for criterion in item.get("acceptance_criteria") or []
                )
                or "—",
            ]
            for item in requirements
        ],
    )

    lines += ["", "## Source References", ""]
    lines += render_table(
        ["Ref", "Document", "Markdown", "Sections", "Status"],
        [
            [
                item["ref"],
                item["name"] or "(unnamed file)",
                f"`{item['md']}`" if item["md"] else "—",
                item["sections"],
                item["status"],
            ]
            for item in reader.list_files()
        ],
    )

    lines += ["", "## Quality Gate 1", ""]
    gate_rows = [["Result", gate["result"], ""]]
    for failure in gate["failures"]:
        gate_rows.append([failure["check"], "FAIL", failure["detail"]])
    for warning in gate["warnings"]:
        gate_rows.append([warning["check"], "WARN", warning["detail"]])
    lines += render_table(["Check", "Outcome", "Detail"], gate_rows)

    for warning in manifest.get("warnings", []):
        lines.append(
            f"- Manifest warning `{warning.get('code', 'WARNING')}` for "
            f"`{warning.get('ref', 'unknown')}`: {warning.get('detail', '')}"
        )

    lines += [
        "",
        "## Downstream Readiness",
        "",
        f"- Readiness: `{readiness}`.",
        "- `BLOCKED`: do not generate or publish deliverables.",
        "- `CONDITIONAL`: generate conditional drafts, preserve every unresolved "
        "conflict and open question, and emit no definitive expected result for a "
        "requirement that is not confirmed.",
        "- `READY_FOR_HUMAN_REVIEW`: generate review drafts.",
        "",
    ]
    for note in package.get("analyst_notes", []):
        lines.append(f"- Analyst note: {note}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- main


def main():
    request_inputs = {
        "procurement_case_id": None,
    }

    # The Coding Agent fills this literal in Pass 2, after reading the corpus in
    # Pass 1. Every list must be authored from the corpus; none may be left as a
    # placeholder. See "Canonical Package Schema" and "Requirement Extraction Rules".
    analysis_package = {
        "case_id": None,
        "package_title": None,
        "business_background": [],
        "objectives": [],
        "scope_in": [],
        "scope_out": [],
        "glossary": [],
        "actors": [],
        "processes": [],
        "domain_states": [],
        "requirements": [],
        "business_rules": [],
        "data_dictionary": [],
        "code_tables": [],
        "ui_requirements": [],
        "ui_exclusions": [],
        "notifications": [],
        "integrations": [],
        "non_functional": [],
        "exceptions": [],
        "assumptions": [],
        "conflicts": [],
        "open_questions": [],
        "analyst_notes": [],
    }

    procurement_case_id = request_inputs.get("procurement_case_id")
    if (
        not isinstance(procurement_case_id, str)
        or not procurement_case_id.strip()
        or "/" in procurement_case_id
        or "\\" in procurement_case_id
        or procurement_case_id.strip() in {".", ".."}
    ):
        print("[NEEDS_INFO] missing=PROCUREMENT_CASE_ID")
        print(
            "Please provide a non-empty procurement case identifier in the current "
            "request. It must be a single folder name for the local Procurement "
            "input and analysis directories."
        )
        raise SystemExit(0)

    case_id = procurement_case_id.strip()
    input_root = Path("Procurement", case_id, "input")
    analysis_root = Path("Procurement", case_id, "analysis")
    markdown_path = analysis_root / "canonical-requirements.md"
    json_path = analysis_root / "canonical-requirements.json"

    try:
        reader = CaseDocumentReader(input_root)
        manifest = reader.get_manifest()
    except (ManifestError, OSError, json.JSONDecodeError) as error:
        print("[CORPUS_UNUSABLE]")
        print(
            f"The procurement case could not be analyzed because its local manifest "
            f"or OCR Markdown corpus is not usable: {error}"
        )
        raise SystemExit(1)

    if not any(item["status"] == "ok" for item in reader.list_files()):
        print("[CORPUS_UNUSABLE]")
        print("The manifest contains no readable OCR Markdown document.")
        raise SystemExit(1)

    analysis_package["case_id"] = analysis_package.get("case_id") or case_id
    if analysis_package["case_id"] != case_id:
        print("[CORPUS_UNUSABLE]")
        print(
            f"The authored package case ID {analysis_package['case_id']!r} does not "
            f"match the requested case ID {case_id!r}."
        )
        raise SystemExit(1)

    gate = QualityGateOne(reader, analysis_package).run()
    readiness = determine_readiness(analysis_package, gate)

    if gate["result"] == "FAILED":
        print("[QG1_FAILED]")
        for failure in gate["failures"]:
            print(f"  {failure['check']}: {failure['detail']}")
        print(
            f"{len(gate['failures'])} blocking issue(s). No canonical package was "
            f"written; the workflow must stop before deliverable generation."
        )
        raise SystemExit(1)

    generated_utc = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    canonical_markdown = render_canonical_markdown(
        analysis_package, manifest, gate, readiness, reader, generated_utc
    )
    write_text_atomically(markdown_path, canonical_markdown)

    canonical_sha256 = hashlib.sha256(
        canonical_markdown.encode("utf-8")
    ).hexdigest()

    sidecar = {
        "schema": CANONICAL_SCHEMA,
        "case_id": case_id,
        "generated_utc": generated_utc,
        "canonical_markdown_path": markdown_path.as_posix(),
        "canonical_sha256": canonical_sha256,
        "readiness": readiness,
        "quality_gate_1": gate,
        "counts": {
            "requirements": len(analysis_package["requirements"]),
            "confirmed": sum(
                1
                for item in analysis_package["requirements"]
                if item["status"] == "confirmed"
            ),
            "conditional": sum(
                1
                for item in analysis_package["requirements"]
                if item["status"] == "conditional"
            ),
            "blocked": sum(
                1
                for item in analysis_package["requirements"]
                if item["status"] == "blocked"
            ),
            "acceptance_criteria": sum(
                len(item.get("acceptance_criteria") or [])
                for item in analysis_package["requirements"]
            ),
            "conflicts": len(analysis_package["conflicts"]),
            "open_questions": len(analysis_package["open_questions"]),
            "screens": len(analysis_package["ui_requirements"]),
        },
        "source_manifest": manifest.get("meta", {}),
        "package": analysis_package,
    }
    write_text_atomically(
        json_path, json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n"
    )

    print(f"Quality Gate 1: {gate['result']}")
    for warning in gate["warnings"]:
        print(f"  WARN {warning['check']}: {warning['detail']}")
    print(f"Readiness: {readiness}")
    print(f"Canonical Markdown: {markdown_path}")
    print(f"Canonical JSON: {json_path}")
    print(f"Canonical SHA-256: {canonical_sha256}")
    print(
        f"Procurement case {case_id} produced {sidecar['counts']['requirements']} "
        f"requirement(s) — {sidecar['counts']['confirmed']} confirmed, "
        f"{sidecar['counts']['conditional']} conditional, "
        f"{sidecar['counts']['blocked']} blocked — with "
        f"{sidecar['counts']['acceptance_criteria']} acceptance criteria, "
        f"{sidecar['counts']['conflicts']} conflict(s) and "
        f"{sidecar['counts']['open_questions']} open question(s)."
    )


if __name__ == "__main__":
    main()
```

---

## Boundary

This skill owns requirement extraction, evidence traceability, Quality Gate 1, readiness determination, and the canonical package format. It does not retrieve or OCR documents, does not generate Word, Excel, or HTML deliverables, does not publish anything, and cannot override the orchestrating workflow's sequencing, consent gating, or the downstream capability's own quality and publication gates.