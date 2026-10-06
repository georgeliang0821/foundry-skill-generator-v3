---
name: procurement-workflow
description: "Coordinate one procurement case end to end: retrieve and OCR its source documents, analyze traceable requirements, then—with explicit publication consent—generate and publish controlled deliverables. Use for Chinese or English requests such as 處理採購案、完成採購需求分析與文件產出, or process a procurement case from documents through published Word, Excel, and HTML outputs; not for a standalone one-page project overview."
metadata:
  author: "a-wureeve@microsoft.com"
  version: "1.0"
  skill_type: scenario-orchestration
  children:
    - procurement-sharepoint-ocr
    - procurement-requirements-analysis
    - procurement-deliverables-generator
---

## S1 -- Scenario-layer notice

> **Warning:** This is a scenario-orchestration skill and is not executed by the backend. It tells the host how to sequence one procurement-case workflow, preserve its host-side session context, and delegate each system action to the declared child capability skills.

## S0 -- Fetch authorization

The skills below do not appear in your `list_skills` catalog. **Not being in the list does not mean you may not retrieve their body.** Use `fetch_skill` to read the sections named in each step. These names may appear **only** as `fetch_skill` arguments -- never send one as a user request to `run_coding_workflow`.

## S2 -- Request-type table

| User request type | Child operation sequence | Host pre-work required? |
|---|---|---|
| Process one procurement case completely, from source documents through published deliverables | OCR retrieval and conversion → requirements analysis → deliverable generation and publication | Yes — validate one shared case identifier, establish and retain one EAA Session, enforce sequence, and obtain explicit publication consent before the final stage. |
| Complete procurement requirements analysis and deliverable output for one case | OCR retrieval and conversion → requirements analysis → deliverable generation and publication | Yes — use the same pre-work and consent gate; do not assume upstream artifacts already exist unless this scenario itself has completed them. |

## S3 -- Not applicable

| Out-of-scope request | Routing or reason |
|---|---|
| Create only a generic technical overview, architecture explanation, or presentation | Use `html-ppt` for a self-contained HTML slide deck; this scenario performs a governed procurement workflow rather than a presentation-only task. |
| Retrieve or OCR procurement documents only, without downstream analysis and deliverables | This fixed end-to-end scenario is not appropriate; use the OCR capability directly through an applicable route. |
| Analyze an already-prepared OCR corpus only, without starting from document retrieval or publishing deliverables | This fixed end-to-end scenario is not appropriate; use the requirements-analysis capability through an applicable route. |
| Generate or publish deliverables from an existing canonical package only | This fixed end-to-end scenario is not appropriate; use the deliverables capability through an applicable route. |

## S4 -- Step overview table

| Step | Actor | Action | Host capability required? |
|---|---|---|---|
| 1 | Host | Establish one EAA Session, obtain and validate the shared case identifier, and retain it as workflow context. | Yes |
| 2 | Host | Retrieve each declared child's contract once and retain the fetched sections for all continuation rounds. | Yes |
| 3 | Backend | Run the OCR stage first. | No |
| 4 | Backend | Run requirements analysis only after OCR succeeds. | No |
| 5 | Host | Present the analysis-stage completion status and obtain explicit consent before an externally visible publication stage. | Yes |
| 6 | Backend | Generate, validate, and publish deliverables only after OCR, analysis, and consent all succeed. | No |
| 7 | Host | Aggregate the stage outcomes and provide a user-readable completion or stopped-workflow summary. | Yes |

## S5 -- Per-step detail

### Step 1 — establish workflow context

The host must establish and maintain one EAA Session for the entire workflow. Obtain one valid shared case identifier before any delegation, retain it unchanged in host workflow context, and verify that every stage is built from that same identifier.

Do not fabricate an identifier, infer it from unrelated text, or ask the user for any data that the host has already obtained. If the user corrects the identifier, stop the current run, retain the EAA Session, and restart from Step 3 rather than resuming at a downstream stage.

### Step 2 — retrieve child contracts once

The child field contracts are authoritative in their own bodies; this scenario does not repeat them. Fetch each child exactly once, retain the returned sections for every continuation round, and do not re-fetch on a missing-input continuation.

- `fetch_skill(skill_name="procurement-sharepoint-ocr", sections="Required Inputs, [NEEDS_INFO] 契約, OBO Token Scopes")`
  - `Required Inputs` provides the per-operation required request data and its validation rules.
  - `[NEEDS_INFO] 契約` provides every recoverable `missing=` response and the required host follow-up.
  - `OBO Token Scopes` identifies authorization that is injected by the platform rather than supplied by the user.

- `fetch_skill(skill_name="procurement-requirements-analysis", sections="Required Inputs, [NEEDS_INFO] 契約")`
  - `Required Inputs` provides the per-operation required request data and its validation rules.
  - `[NEEDS_INFO] 契約` provides every recoverable `missing=` response and the required host follow-up.

- `fetch_skill(skill_name="procurement-deliverables-generator", sections="Required Inputs, [NEEDS_INFO] 契約, OBO Token Scopes")`
  - `Required Inputs` provides the per-operation required request data and its validation rules.
  - `[NEEDS_INFO] 契約` provides every recoverable `missing=` response and the required host follow-up.
  - `OBO Token Scopes` identifies authorization that is injected by the platform rather than supplied by the user.

For every delegation, construct the complete child request using the child contract. Do not place a payload into a free-text `request` parameter. The user's own wording is not a wire value: convert it to the value the child contract lists; if the contract lists no matching value, ask the user instead of passing their words through.

### Step 3 — run OCR

Delegate the OCR operation first using the shared case identifier from Step 1. Do not begin analysis until this stage has completed successfully. Preserve the same EAA Session and record the stage outcome for the final summary.

A partial document-processing result that ends in a non-zero execution outcome is not sufficient for this workflow. Stop rather than continuing with incomplete upstream material.

### Step 4 — run requirements analysis

Only after Step 3 succeeds, delegate the requirements-analysis operation using the unchanged shared case identifier. Do not independently invent, rename, relocate, or normalize upstream local artifacts; the analysis capability owns its expected manifest and OCR Markdown corpus conventions.

If the analysis capability reports that its local manifest or OCR Markdown corpus is unusable, stop the workflow. This is not a user-input question and must not proceed to publication.

### Step 5 — obtain explicit publication consent

After successful analysis, tell the user that the next stage will generate controlled Word, Excel, and HTML deliverables and publish the three primary files to the configured SharePoint destination. Ask for explicit consent to proceed.

Do not invoke the final stage unless the user explicitly consents. If the user declines or does not answer, stop after reporting that OCR and analysis completed but no deliverables were generated or published.

### Step 6 — generate and publish deliverables

Only after Step 3, Step 4, and Step 5 succeed, delegate the deliverables operation using the unchanged shared case identifier. The publication is externally visible and must remain gated by the consent obtained in Step 5.

Do not retry a timed-out or failed publication blindly: it may have performed writes. Report the returned stage state and any available per-file outcome, then wait for the user to decide how to proceed.

### Step 7 — summarize outcome

Provide a concise natural-language summary that identifies the shared case, completed stages, stopped stage if any, and the primary deliverables or publication status returned by the final stage. Do not claim that publication, remote verification, approval, or testing completed unless the delegated result explicitly confirms it.

## S6 -- Return-branch table

| Child return or condition | Host action | Continue to follow-up steps? |
|---|---|---|
| OCR: `[NEEDS_INFO] missing=PROCUREMENT_CASE_ID` | Stop. Obtain a valid shared case identifier, retain the same EAA Session, and restart from OCR. | No — restart from Step 3 only after correction. |
| OCR: successful completion | Record OCR success and proceed to requirements analysis. | Yes — Step 4. |
| OCR: non-zero execution failure, including OBO or deployment failure, or one or more document failures that cause a non-zero exit | Stop and report OCR failure. Do not treat it as a user-input handshake. | No. |
| Requirements analysis: `[NEEDS_INFO] missing=PROCUREMENT_CASE_ID` | Stop. Obtain a valid shared case identifier, retain the same EAA Session, and restart from OCR. | No — restart from Step 3 only after correction. |
| Requirements analysis: successful completion | Record analysis success and ask for explicit publication consent. | Yes — Step 5. |
| Requirements analysis: local manifest or OCR Markdown corpus is unusable | Stop and report that upstream local artifacts are unusable. Do not ask the user to supply implementation artifacts and do not invoke deliverables. | No. |
| Requirements analysis: unexpected execution failure | Stop and report the failure. | No. |
| Publication consent: user explicitly agrees | Record consent and invoke deliverables generation and publication. | Yes — Step 6. |
| Publication consent: user declines, withdraws consent, or does not provide consent | Stop after reporting completed upstream stages. Do not generate or publish deliverables. | No. |
| Deliverables: `[NEEDS_INFO] missing=PROCUREMENT_CASE_ID` | Stop. Obtain a valid shared case identifier, retain the same EAA Session, and restart from OCR. | No — restart from Step 3 only after correction. |
| Deliverables: successful completion with confirmed publication | Record returned deliverable locations and publication state, then provide the final summary. | Yes — Step 7. |
| Deliverables: upstream canonical package not ready, local validation failure, OBO or deployment failure, or other non-zero execution failure | Stop and report the returned failure. Do not blindly retry because publication-related writes may already have occurred. | No. |
| Deliverables: partial publication or verification failure | Stop and report per-file outcomes if returned. Preserve the reported state and request user direction before any further attempt. | No. |

## Boundary

This scenario governs only the ordering of work, host-side session continuity, request assembly, consent gating, return branching, and user-facing aggregation for one procurement case. It cannot override host identity handling, credential handling, child capability contracts, governance limits, publication policy, or output rules.