from __future__ import annotations

import ast
from pathlib import Path

from backend.material_patch import MAX_CHANGED_RATIO, _logic_lines, changed_ratio, core_call_signatures, patch_defects, rewrite_reasons

ORIGINAL = """import os
import requests

ROOM_ID = os.environ["ROOM_ID"]
session = requests.Session()
response = session.get(f"https://graph.example.invalid/rooms/{ROOM_ID}", timeout=10)
print(f"Room: {response.text}")
"""

# Edge-only: the input moves onto a flag and stdout becomes one JSON document.
ADAPTED = """import argparse
import json
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--room-id", required=True)
ROOM_ID = parser.parse_args().room_id
session = requests.Session()
response = session.get(f"https://graph.example.invalid/rooms/{ROOM_ID}", timeout=10)
print(json.dumps({"room": response.text}))
"""


def test_signatures_follow_the_import_not_the_variable_name() -> None:
    renamed = ORIGINAL.replace("session", "client")
    assert core_call_signatures(ORIGINAL) == core_call_signatures(renamed) == {
        "requests.Session",
        "requests.Session().get",
    }


def test_only_io_stdlib_modules_count_as_external() -> None:
    code = "import json, os, subprocess\njson.dumps({})\nos.getenv('X')\nsubprocess.run(['ls'])\n"
    assert core_call_signatures(code) == {"subprocess.run"}


def test_signatures_propagate_through_with_and_for() -> None:
    code = """import pyodbc
with pyodbc.connect("dsn") as conn:
    for row in conn.cursor().execute("select 1"):
        row.close()
"""
    assert core_call_signatures(code) == {
        "pyodbc.connect",
        "pyodbc.connect().cursor",
        "pyodbc.connect().cursor().execute",
        "pyodbc.connect().cursor().execute()[].close",
    }


def test_an_edge_only_adaptation_is_not_a_rewrite() -> None:
    assert patch_defects(ORIGINAL, ADAPTED) == []
    assert rewrite_reasons(ORIGINAL, ADAPTED) == []


def test_moving_code_into_main_is_not_counted_as_changing_it() -> None:
    body = "\n".join(f"    {line}" if line else "" for line in ORIGINAL.splitlines()[3:])
    wrapped = "import os\nimport requests\n\ndef main():\n" + body + "\n\nmain()\n"
    assert changed_ratio(ORIGINAL, wrapped) == 0
    assert rewrite_reasons(ORIGINAL, wrapped) == []


def test_dropping_an_external_call_is_a_rewrite() -> None:
    after = ORIGINAL.replace('response = session.get(f"https://graph.example.invalid/rooms/{ROOM_ID}", timeout=10)', 'response = None')
    reasons = rewrite_reasons(ORIGINAL, after)
    assert any("`requests.Session().get`" in reason for reason in reasons)


def test_changing_most_lines_is_a_rewrite() -> None:
    after = """import requests
import os

rid = os.environ.get("ROOM_ID", "")
s = requests.Session()
r = s.get("https://graph.example.invalid/rooms/" + rid)
print(r.text)
"""
    assert changed_ratio(ORIGINAL, after) > MAX_CHANGED_RATIO
    assert any("of the original lines" in reason for reason in rewrite_reasons(ORIGINAL, after))


def test_an_unparseable_original_cannot_be_adapted() -> None:
    reasons = rewrite_reasons("print(\n", "print()\n")
    assert reasons and "original code material is not valid Python" in reasons[0]


def test_patch_defects_catch_a_no_op_and_broken_output() -> None:
    assert patch_defects(ORIGINAL, ORIGINAL) == ["The patch changes nothing."]
    assert "not valid Python" in patch_defects(ORIGINAL, "def (\n")[0]


def test_argparse_and_json_edges_of_the_storage_cleanup_are_not_a_rewrite() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    before = (fixtures / "storage_cleanup_material.py").read_text(encoding="utf-8")
    after = (fixtures / "storage_cleanup_script.py").read_text(encoding="utf-8")

    assert changed_ratio(before, after) < 0.15
    assert rewrite_reasons(before, after) == []


SHORT = """import os
import requests

TARGET = os.environ.get("TARGET")
response = requests.delete(f"https://api.example.invalid/items/{TARGET}", timeout=10)
print(f"deleted {TARGET}: {response.status_code}")
"""


def test_input_rebinding_and_exit_handling_are_edges() -> None:
    after = """import argparse
import json
import requests

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--target", default="")
try:
    args = parser.parse_args()
except SystemExit:
    print("[NEEDS_INFO] missing=TARGET")
    raise SystemExit(0)
TARGET = str(args.target).strip()
response = requests.delete(f"https://api.example.invalid/items/{TARGET}", timeout=10)
print(json.dumps({"target": TARGET, "status_code": response.status_code}))
"""
    assert changed_ratio(SHORT, after) == 0


def test_a_business_call_inside_an_edge_statement_still_counts() -> None:
    before = SHORT.replace(
        "response = requests.delete(", "def purge():\n    return requests.delete("
    ).replace(
        'print(f"deleted {TARGET}: {response.status_code}")',
        "response = purge()\nprint(response.status_code)",
    )
    after = before.replace("response = purge()\nprint(response.status_code)", "print(json.dumps(purge().status_code))")

    assert "print(response.status_code)" not in _logic_lines(before, ast.parse(before), frozenset())
    assert "print(json.dumps(purge().status_code))" in _logic_lines(after, ast.parse(after), frozenset())
