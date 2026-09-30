import os
import re
from datetime import datetime, timezone, timedelta

from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential


def _needs_info(*codes):
    print(f"[NEEDS_INFO] missing={','.join(codes)}")
    print("請提供或更正 Azure Storage 保留清理所需的 request 欄位後再試一次。")
    raise SystemExit(0)


def _to_name_list(value, missing_code):
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        _needs_info(missing_code)
    return [item.strip() for item in value]


def parse_retention_days(rule):
    if not isinstance(rule, str):
        _needs_info("RETENTION_RULE")
    match = re.search(r"(\d+)", rule)
    if not match:
        _needs_info("RETENTION_RULE")
    return int(match.group(1))


def main():
    request_inputs = {
        "TARGET_TABLES": None,
        "TARGET_CONTAINERS": None,
        "BLOB_PREFIX": None,
        "RETENTION_RULE": None,
        "DRY_RUN": None,
    }

    target_tables = _to_name_list(request_inputs.get("TARGET_TABLES"), "TARGET_TABLES")
    target_containers = _to_name_list(
        request_inputs.get("TARGET_CONTAINERS"), "TARGET_CONTAINERS"
    )
    blob_prefix = request_inputs.get("BLOB_PREFIX")
    retention_rule = request_inputs.get("RETENTION_RULE")
    dry_run = request_inputs.get("DRY_RUN")

    if not target_tables and not target_containers:
        _needs_info("TARGET_TABLES_OR_TARGET_CONTAINERS")
    if blob_prefix is not None and not isinstance(blob_prefix, str):
        _needs_info("BLOB_PREFIX")
    if retention_rule is None:
        _needs_info("RETENTION_RULE")
    if dry_run is None:
        dry_run = False
    elif not isinstance(dry_run, bool):
        _needs_info("DRY_RUN")

    retention_days = parse_retention_days(retention_rule)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    azure_storage_account_name = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    credential = DefaultAzureCredential()

    table_deleted = 0
    if target_tables:
        table_service = TableServiceClient(
            endpoint=(
                f"https://{azure_storage_account_name}.table.core.windows.net"
            ),
            credential=credential,
        )
        for table_name in target_tables:
            table = table_service.get_table_client(table_name)
            to_delete = []

            # Do NOT use list_entities(filter=...) — filter in code instead.
            for entity in table.list_entities():
                raw = entity.get("updated_at") or entity.get("UpdatedAt")
                if not raw:
                    continue
                try:
                    timestamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                if timestamp < cutoff:
                    to_delete.append((entity["PartitionKey"], entity["RowKey"]))

            if not dry_run:
                for partition_key, row_key in to_delete:
                    table.delete_entity(partition_key=partition_key, row_key=row_key)
            table_deleted += len(to_delete)

    blob_deleted = 0
    containers_not_found = []
    if target_containers:
        blob_service = BlobServiceClient(
            account_url=(
                f"https://{azure_storage_account_name}.blob.core.windows.net"
            ),
            credential=credential,
        )
        for container_name in target_containers:
            container = blob_service.get_container_client(container_name)
            if not container.exists():
                containers_not_found.append(container_name)
                continue

            to_delete = []
            for blob in container.list_blobs(name_starts_with=blob_prefix):
                # Use the server-side Last-Modified timestamp (always present, always UTC).
                if blob.last_modified and blob.last_modified < cutoff:
                    to_delete.append(blob.name)

            if not dry_run:
                # Batch delete: max 256 blobs per request
                for index in range(0, len(to_delete), 256):
                    batch = to_delete[index:index + 256]
                    results = container.delete_blobs(
                        *batch,
                        delete_snapshots="include",
                        raise_on_any_failure=False,
                    )
                    for name, response in zip(batch, results):
                        if response.status_code not in (202, 404):
                            print(
                                f"[blob] failed to delete {name}: "
                                f"HTTP {response.status_code}"
                            )
            blob_deleted += len(to_delete)

    action = "預覽到" if dry_run else "已處理"
    missing_containers_note = ""
    if containers_not_found:
        missing_containers_note = (
            "；找不到並略過的 container：" + ", ".join(containers_not_found)
        )
    print(
        f"{action}截止於 {cutoff.date().isoformat()} 之前的 Azure Storage 資料："
        f"在 {len(target_tables)} 個 Table 中共有 {table_deleted} 個實體，"
        f"在 {len(target_containers)} 個 Blob container 中共有 {blob_deleted} 個 blobs"
        f"{'可刪除' if dry_run else '已刪除'}。"
        f"帳戶為 {azure_storage_account_name}{missing_containers_note}。"
    )


if __name__ == "__main__":
    main()