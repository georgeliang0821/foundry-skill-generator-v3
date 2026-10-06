---
name: procurement-sharepoint-ocr
description: "Retrieve and OCR-process documents for a procurement case from the fixed SharePoint Procurement library, producing local Markdown files and a manifest summary. Use for Chinese or English requests to load, extract, or convert procurement-case documents by case ID; use `html-ppt` instead when the request is to create a technical overview presentation."
metadata:
  author: "a-wureeve@microsoft.com"
  mi_scopes:
    - https://cognitiveservices.azure.com
  tags:
    - procurement
    - sharepoint
    - ocr
    - content-understanding
  uses_obo: true
---

## Overview
Recursively retrieves supported documents for one procurement case, converts them to PDF when necessary, extracts Markdown with Content Understanding, and writes a manifest with per-file results.

## When NOT to Use This Skill
Creating a technical project overview, architecture explanation, or presentation unrelated to a procurement case -> not this skill; for a self-contained HTML slide deck use `html-ppt`.

## Required Inputs
```input-bindings
- name: procurement_case_id
  source: request
  required: true
```

- `procurement_case_id` (required): Procurement case identifier and the folder name under the fixed `Procurement` SharePoint document library. Provide a non-empty single folder name; it must not contain `/`, `\\`, `.` or `..` path segments.
- Ground Rule: Before calling SharePoint, Microsoft Graph, or Content Understanding, ask the user for every missing required input.

## `[NEEDS_INFO]` 契約
- `PROCUREMENT_CASE_ID`: The Coding Agent must obtain a non-empty procurement case identifier from the current request and bind it to `request_inputs["procurement_case_id"]`. The identifier selects the folder below the fixed `Procurement` document library.

## Environment Variables
- `CONTENTUNDERSTANDING_ENDPOINT` (required): Azure AI Content Understanding service endpoint used for the `prebuilt-layout` analyzer. Read it only as `os.environ["CONTENTUNDERSTANDING_ENDPOINT"]`; if absent, deployment configuration is broken and execution must end non-zero.

## OBO Token Scopes
- `GRAPH_ACCESS_TOKEN` (required): OBO token for Microsoft Graph with scope `https://graph.microsoft.com/.default`, used to resolve the SharePoint site and library, enumerate folder contents, and download or convert documents. It is injected by the OBO exchange; if absent, the OBO chain is broken and execution must end non-zero.

## 部署設定使用規範

- **D1**：`## Environment Variables` 與 `## OBO Token Scopes` 宣告的每個變數，
  一律以 `os.environ["NAME"]` 索引式讀取，禁止 `.get()`、`os.getenv`、
  `or "..."` 之類的 fallback、任何預設值或預設參數。
- **D2**：這些變數缺席導致的 `KeyError` 必須直接向上拋出、以非零狀態中止。
  絕不可輸出 `[NEEDS_INFO]`、絕不可要求 caller 或使用者補值——它們由部署與
  OBO 交換注入，caller 沒有能力提供，要求補值只會造成無限重試。

## API Reference / Sample Code
```python
import json
import mimetypes
import os
import re
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TIMEOUT_SECONDS = 60
OCR_ANALYZER_ID = "prebuilt-layout"
PDF_CONVERTIBLE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".dot",
    ".dotm",
    ".dotx",
    ".dsn",
    ".dwg",
    ".eml",
    ".epub",
    ".fluidframework",
    ".form",
    ".htm",
    ".html",
    ".loop",
    ".loot",
    ".markdown",
    ".md",
    ".msg",
    ".note",
    ".odp",
    ".ods",
    ".odt",
    ".page",
    ".pps",
    ".ppsx",
    ".ppt",
    ".pptx",
    ".pulse",
    ".rtf",
    ".task",
    ".tif",
    ".tiff",
    ".wbtx",
    ".whiteboard",
    ".xls",
    ".xlsm",
    ".xlsx",
}


class OcrError(RuntimeError):
    pass


def can_process_as_pdf(file_name):
    extension = PurePosixPath(file_name).suffix.casefold()
    return extension == ".pdf" or extension in PDF_CONVERTIBLE_EXTENSIONS


def graph_get(url, token):
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as error:
        request_id = error.headers.get("request-id")
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Graph request failed: {error.code}\n"
            f"URL: {url}\n"
            f"request-id: {request_id}\n"
            f"BODY: {body[:3000]}"
        ) from error


def resolve_site_id(site_url, token):
    parsed = urlparse(site_url.rstrip("/"))
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("site_url must be a full HTTPS URL")
    site_path = quote(parsed.path or "/", safe="/")
    return graph_get(f"{GRAPH_ROOT}/sites/{parsed.netloc}:{site_path}", token)["id"]


def resolve_drive_id(site_id, library_name, token):
    data = graph_get(f"{GRAPH_ROOT}/sites/{site_id}/drives", token)
    for drive in data.get("value", []):
        if drive["name"].casefold() == library_name.casefold():
            return drive["id"], drive["name"]
    available = ", ".join(drive["name"] for drive in data.get("value", []))
    raise ValueError(
        f"Document library not found: {library_name}. Available: {available or '(none)'}"
    )


def list_children(drive_id, folder_path, token):
    normalized_path = folder_path.strip("/")
    if normalized_path:
        encoded_path = quote(normalized_path, safe="/")
        url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{encoded_path}:/children"
    else:
        url = f"{GRAPH_ROOT}/drives/{drive_id}/root/children"

    while url:
        data = graph_get(url, token)
        yield from data.get("value", [])
        url = data.get("@odata.nextLink")


def list_files(drive_id, folder_path, token):
    pending_folders = [folder_path.strip("/")]
    while pending_folders:
        current_folder = pending_folders.pop()
        for item in list_children(drive_id, current_folder, token):
            item_path = "/".join(part for part in (current_folder, item["name"]) if part)
            if "folder" in item:
                pending_folders.append(item_path)
            elif can_process_as_pdf(item["name"]):
                yield {"id": item["id"], "name": item["name"], "path": item_path}


def read_sharepoint_file_as_pdf(drive_id, file, token):
    source_extension = PurePosixPath(file["name"]).suffix.casefold()
    url = f"{GRAPH_ROOT}/drives/{drive_id}/items/{file['id']}/content"
    if source_extension in PDF_CONVERTIBLE_EXTENSIONS:
        url += "?format=pdf"
    elif source_extension != ".pdf":
        raise ValueError(
            f"Microsoft Graph cannot convert {source_extension or '(no extension)'} "
            f"to PDF: {file['path']}"
        )

    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as error:
        request_id = error.headers.get("request-id")
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Download failed: {error.code}\n"
            f"PATH: {file['path']}\n"
            f"request-id: {request_id}\n"
            f"BODY: {body[:3000]}"
        ) from error


def analyze_document(client, file_bytes, file_name):
    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    try:
        poller = client.begin_analyze(
            analyzer_id=OCR_ANALYZER_ID,
            inputs=[
                AnalysisInput(
                    data=file_bytes,
                    name=PurePosixPath(file_name).name,
                    mime_type=content_type,
                )
            ],
        )
        result = poller.result()
    except HttpResponseError as error:
        raise OcrError(
            f"Content Understanding request failed for {file_name}: {error.message}"
        ) from error
    if not result.contents:
        raise OcrError(f"Analyzer returned no content for {file_name}")
    return result


def render_markdown(result):
    markdown_parts = [content.markdown for content in result.contents if content.markdown]
    if not markdown_parts:
        raise OcrError("Content Understanding returned no Markdown content")
    return "\n\n".join(markdown_parts).rstrip() + "\n"


def markdown_output_path(output_root, sharepoint_path):
    source_path = PurePosixPath(sharepoint_path)
    if any(part in {"", ".", ".."} for part in source_path.parts):
        raise ValueError(f"Unsafe SharePoint path: {sharepoint_path}")
    output_name = f"{source_path.stem}.ocr.md"
    return output_root.joinpath(*source_path.parent.parts, output_name)


def path_relative_to_folder(sharepoint_path, folder_path):
    source_path = PurePosixPath(sharepoint_path)
    folder = PurePosixPath(folder_path.strip("/"))
    try:
        return source_path.relative_to(folder)
    except ValueError as error:
        raise ValueError(
            f"File path is outside the selected folder: {sharepoint_path}"
        ) from error


def save_markdown(markdown, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f"{output_path.name}.part")
    temporary_path.write_text(markdown, encoding="utf-8", newline="\n")
    temporary_path.replace(output_path)


def markdown_sections(markdown, file_ref):
    lines = markdown.splitlines()
    heading_lines = [
        (index, match.group(1).strip())
        for index, line in enumerate(lines, start=1)
        if (match := re.match(r"^#{1,6}\s+(.+?)\s*$", line))
    ]
    if heading_lines and heading_lines[0][0] == 1:
        boundaries = heading_lines
    else:
        boundaries = [(1, "(preamble)"), *heading_lines]

    sections = []
    for index, (start_line, heading) in enumerate(boundaries):
        end_line = (
            boundaries[index + 1][0] - 1
            if index + 1 < len(boundaries)
            else max(len(lines), 1)
        )
        section_text = "\n".join(lines[start_line - 1 : end_line])
        sections.append(
            {
                "id": f"{file_ref}-S{index:02d}",
                "heading": heading,
                "lines": [start_line, end_line],
                "chars": len(section_text),
            }
        )
    return sections


def successful_manifest_entry(file_ref, file, output_path, output_root, markdown, conversion):
    return {
        "ref": file_ref,
        "name": file["name"],
        "md": output_path.relative_to(output_root).as_posix(),
        "item_id": file["id"],
        "status": "ok",
        "sent_as": "pdf",
        "conversion": conversion,
        "total_chars": len(markdown),
        "sections": markdown_sections(markdown, file_ref),
    }


def save_manifest(manifest, output_root):
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "manifest.json"
    temporary_path = output_root / "manifest.json.part"
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary_path.replace(output_path)


def main():
    request_inputs = {
        "procurement_case_id": None,
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
            "Please provide a non-empty procurement case identifier in the current request. "
            "It must be a single folder name under the Procurement document library."
        )
        raise SystemExit(0)

    graph_token = os.environ["GRAPH_ACCESS_TOKEN"]
    content_understanding_endpoint = os.environ["CONTENTUNDERSTANDING_ENDPOINT"]

    site_url = "https://mngenvmcap352952.sharepoint.com"
    library_name = "Procurement"
    folder_path = procurement_case_id.strip()
    output_root = Path(library_name, folder_path, "input").resolve()

    site_id = resolve_site_id(site_url, graph_token)
    drive_id, resolved_library_name = resolve_drive_id(
        site_id, library_name, graph_token
    )
    files = sorted(
        list(list_files(drive_id, folder_path, graph_token)),
        key=lambda file: file["path"].casefold(),
    )

    credential = DefaultAzureCredential()

    client = ContentUnderstandingClient(
        endpoint=content_understanding_endpoint,
    	credential=credential
    )

    print(f"Document library: {resolved_library_name}")
    print(f"Markdown directory: {output_root}")
    print(f"Files found: {len(files)}")

    manifest = {
        "meta": {
            "case_id": PurePosixPath(folder_path).name,
            "source_folder": f"/{library_name}/{folder_path.strip('/')}",
            "generated_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "analyzer": OCR_ANALYZER_ID,
            "sdk_version": (
                f"azure-ai-contentunderstanding=="
                f"{version('azure-ai-contentunderstanding')}"
            ),
        },
        "files": [],
        "warnings": [],
    }
    completed = 0
    failed = 0
    for index, file in enumerate(files, start=1):
        file_ref = f"F{index:02d}"
        relative_path = path_relative_to_folder(file["path"], folder_path)
        output_path = markdown_output_path(output_root, relative_path.as_posix())
        print(f"[{index}/{len(files)}] ANALYZE {file['path']}")
        source_extension = PurePosixPath(file["name"]).suffix.casefold()
        conversion = "not_required" if source_extension == ".pdf" else "pending"
        try:
            file_bytes = read_sharepoint_file_as_pdf(drive_id, file, graph_token)
            if conversion == "pending":
                conversion = "ok"
        except (RuntimeError, ValueError) as error:
            detail = str(error)
            manifest["files"].append(
                {
                    "ref": file_ref,
                    "name": file["name"],
                    "md": None,
                    "item_id": file["id"],
                    "status": "failed",
                    "sent_as": "pdf",
                    "conversion": "failed",
                    "detail": detail,
                }
            )
            manifest["warnings"].append(
                {
                    "code": "GRAPH_CONVERSION_FAILED",
                    "ref": file_ref,
                    "name": file["name"],
                    "detail": detail,
                }
            )
            failed += 1
            print(f"[{index}/{len(files)}] FAILED {file['path']}: {error}")
            continue

        try:
            pdf_name = f"{PurePosixPath(file['name']).stem}.pdf"
            result = analyze_document(client, file_bytes, pdf_name)
            markdown = render_markdown(result)
            save_markdown(markdown, output_path)
            manifest["files"].append(
                successful_manifest_entry(
                    file_ref,
                    file,
                    output_path,
                    output_root,
                    markdown,
                    conversion,
                )
            )
            completed += 1
            print(f"[{index}/{len(files)}] SAVED {output_path}")
        except OcrError as error:
            detail = str(error)
            manifest["files"].append(
                {
                    "ref": file_ref,
                    "name": file["name"],
                    "md": None,
                    "item_id": file["id"],
                    "status": "failed",
                    "sent_as": "pdf",
                    "conversion": conversion,
                    "detail": detail,
                }
            )
            manifest["warnings"].append(
                {
                    "code": "CU_FAILED",
                    "ref": file_ref,
                    "name": file["name"],
                    "detail": detail,
                }
            )
            failed += 1
            print(f"[{index}/{len(files)}] FAILED {file['path']}: {error}")

    save_manifest(manifest, output_root)
    print(f"Manifest: {output_root / 'manifest.json'}")
    print(
        f"Procurement case {folder_path} processing is complete: "
        f"{completed} document(s) were converted to Markdown and {failed} document(s) failed."
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
```