import os
import re
from datetime import datetime, timezone, timedelta
from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential


def _to_list(value):
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value or [])


def _runtime(name):
    # Runtime-injected variable first, then environment variable
    return globals().get(name) or os.environ.get(name)


AZURE_STORAGE_ACCOUNT_NAME = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
TARGET_TABLES = _to_list(_runtime("TARGET_TABLES"))
TARGET_CONTAINERS = _to_list(_runtime("TARGET_CONTAINERS"))   # 新增：要清的 blob container
BLOB_PREFIX = _runtime("BLOB_PREFIX") or None                 # 選填：只清某個「資料夾」前綴
RETENTION_RULE = _runtime("RETENTION_RULE")
DRY_RUN = str(_runtime("DRY_RUN") or "false").lower() in ("1", "true", "yes")

missing = []
if not (TARGET_TABLES or TARGET_CONTAINERS):
    missing.append("TARGET_TABLES_or_TARGET_CONTAINERS")
if not RETENTION_RULE:
    missing.append("RETENTION_RULE")
if missing:
    print(f"[NEEDS_INFO] missing={','.join(missing)}")
    print("I need the target table/container name(s) and retention rule before I can delete Azure Storage data.")
    raise SystemExit(0)


def parse_retention_days(rule) -> int:
    """Accepts '14', '14d', '14 days', '14天'. Replace with the skill's real parser if needed."""
    m = re.search(r"(\d+)", str(rule))
    if not m:
        raise ValueError(f"Cannot parse RETENTION_RULE: {rule!r}")
    return int(m.group(1))


cutoff = datetime.now(timezone.utc) - timedelta(days=parse_retention_days(RETENTION_RULE))
credential = DefaultAzureCredential()
print(f"cutoff={cutoff.isoformat()} dry_run={DRY_RUN}")

# ---------------------------------------------------------------- Table Storage
table_deleted = 0
if TARGET_TABLES:
    table_service = TableServiceClient(
        endpoint=f"https://{AZURE_STORAGE_ACCOUNT_NAME}.table.core.windows.net",
        credential=credential,
    )
    for table_name in TARGET_TABLES:
        table = table_service.get_table_client(table_name)
        to_delete = []

        # Do NOT use list_entities(filter=...) — filter in code instead.
        for entity in table.list_entities():
            raw = entity.get("updated_at") or entity.get("UpdatedAt")
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                to_delete.append((entity["PartitionKey"], entity["RowKey"]))

        if not DRY_RUN:
            for pk, rk in to_delete:
                table.delete_entity(partition_key=pk, row_key=rk)
        table_deleted += len(to_delete)
        print(f"[table] {table_name}: {len(to_delete)} entities {'would be ' if DRY_RUN else ''}deleted")

# ----------------------------------------------------------------- Blob Storage
blob_deleted = 0
if TARGET_CONTAINERS:
    blob_service = BlobServiceClient(
        account_url=f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    )
    for container_name in TARGET_CONTAINERS:
        container = blob_service.get_container_client(container_name)
        if not container.exists():
            print(f"[blob] {container_name}: container not found, skipped")
            continue

        to_delete = []
        for blob in container.list_blobs(name_starts_with=BLOB_PREFIX):
            # Use the server-side Last-Modified timestamp (always present, always UTC).
            if blob.last_modified and blob.last_modified < cutoff:
                to_delete.append(blob.name)

        if not DRY_RUN:
            # Batch delete: max 256 blobs per request
            for i in range(0, len(to_delete), 256):
                batch = to_delete[i:i + 256]
                results = container.delete_blobs(
                    *batch, delete_snapshots="include", raise_on_any_failure=False
                )
                for name, resp in zip(batch, results):
                    if resp.status_code not in (202, 404):
                        print(f"[blob] failed to delete {name}: HTTP {resp.status_code}")
        blob_deleted += len(to_delete)
        print(f"[blob] {container_name}: {len(to_delete)} blobs {'would be ' if DRY_RUN else ''}deleted")

print(
    f"Done. tables={len(TARGET_TABLES)} entities={table_deleted}, "
    f"containers={len(TARGET_CONTAINERS)} blobs={blob_deleted}, "
    f"account={AZURE_STORAGE_ACCOUNT_NAME}, dry_run={DRY_RUN}"
)
