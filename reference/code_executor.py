"""
Code Executor - 抽象介面層 (Phase 3)
====================================================================
把「執行 Python code」這件事抽象成 Protocol,讓未來搬遷 Foundry Hosted
Agent 時可以「換實作」而不是「改架構」。

對應設計文件:
- 主文件 v3 §17.2 CodeExecutor 介面
- 主文件 v3 §5.7 Cancel 機制 (cooperative cancellation via SIGTERM + 30s grace)
- 補篇 §5.3 Phase 3 開工建議

階段劃分:
- 階段 1 (現在):LocalSubprocessExecutor — 用 asyncio.create_subprocess_exec
  在 ACA 本機跑 script。
- 階段 2 (POC 通過後):HostedAgentExecutor — 透過 Responses API 委派
  給 Foundry Hosted Agent。本階段不實作。

VERSION: 1.4
2026.09.27 George : S3 — MI 閘門(MI_GATE_ENABLED,預設開;實際生效需 SUBPROCESS_UID_SANDBOX)
- 腳本 env 一律拿掉平台 MI(IDENTITY_* / MSI_*);execute(mi_scopes=) 有可放行的資源時
  改指向 mi_proxy.MiProxy,header 為本次專用、結束即撤銷。

VERSION: 1.3
2026.09.26 George : S2 — per-execution uid sandbox(SUBPROCESS_UID_SANDBOX,預設關)
- 每次 execute 配一個不重用的 uid;執行前 work_dir 交給該 uid(0700),結束後殺光該 uid
  行程、清掉 symlink / 特殊檔 / 多重連結檔,再收回 root。
- timeout / cancel 改對 process group 送訊號(start_new_session)。

VERSION: 1.2
2026.09.26 George : S1 — subprocess env 過濾
- build_subprocess_env():行程 env 先拿掉 SUBPROCESS_ENV_DENYLIST(平台 secret)再疊上本輪注入值。
- filter_caller_env():供 core_handler 剔除會蓋掉平台 token、改寫 interpreter / loader,
  或值含本次請求 Bearer token 的 caller key。
- SUBPROCESS_ENV_FILTER_ENABLED=false 可整組關閉(預設開啟)。

VERSION: 1.1
2026.05.18 George : Phase 3 — subprocess 同步→非同步重構 + cancel 實作
- subprocess.run() → asyncio.create_subprocess_exec() + wait_for(communicate())
- LocalSubprocessExecutor.execute() 加 session_id 參數,進入時把 Process
  註冊到 self._running_procs[session_id],try/finally 結束時移除。
- LocalSubprocessExecutor.cancel(session_id) 從 placeholder 改為實作:
  SIGTERM → 30s grace period → SIGKILL,對應主文件 §5.7.2。
- CodeExecutor Protocol 的 execute() 簽章也加 session_id (保持 Protocol
  與實作一致)。
- 既有 v10.x 行為 100% 保留:stdout 截斷 / content_error pattern 11 條 /
  succeeded_count override (cond_a + cond_b) / glob diff 偵測新檔案 /
  PATH 補償 / ExecutionResult 完整欄位。
- 清掉 v9.0 既有 side effect:os.environ["PATH"] = new_path (line 213)。
  George 確認沒有依賴方,清掉避免 process-level 污染。
- Timeout 路徑改為自己 kill + drain (2s 給 stream drain),對齊 stdlib
  TimeoutExpired 的清理行為。

VERSION: 1.0
2026.05.12 George : Phase 0 初版 — 從 execute_code() 抽出
  subprocess 邏輯封裝為 LocalSubprocessExecutor,維持既有行為。
"""

import asyncio
import glob
import itertools
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Collection, Dict, List, Optional, Protocol, Set, Tuple

from mi_proxy import MI_ENV_KEYS

logger = logging.getLogger(__name__)


# ============================================================================
# 共用常數 (從 code_agent_hosted.py 搬過來,避免循環 import)
# ============================================================================

# v9.1 stdout 截斷閾值 — 防止巨量 API 回應塞爆 agent thread context
MAX_STDOUT_LENGTH = 5000

# Phase 0 沿用 code_agent_hosted.py 既有的 OUTPUT_EXTENSIONS 集合。
# 這裡定義同一份 fallback,實際使用以 code_agent_hosted.py 為準
# (透過 dependency injection 或常數共享,Phase 0 先就地宣告)。
OUTPUT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".html", ".csv", ".tsv",
    ".xlsx", ".xls", ".docx", ".pdf", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".parquet", ".zip", ".log",
}


# ============================================================================
# Subprocess 環境過濾
# ============================================================================

# 平台 secret:user code 沒有正當理由讀取,一律不傳進 subprocess。
# ⚠️ 同樣 uid 下 /proc/1/environ 仍讀得到原值,這裡只擋「直接從 env 拿」。
SUBPROCESS_ENV_DENYLIST = frozenset({
    "OBO_CLIENT_SECRET",
    "TEAMS_NOTIFY_WEBHOOK_URL",
    "LOGIC_APP_SKILL_REVIEW_URL",
})

# 會在 script 執行前改變 interpreter / dynamic loader 行為的名稱,呼叫端不得設定。
_RESERVED_EXEC_ENV_NAMES = frozenset({"PATH"})
_RESERVED_EXEC_ENV_PREFIXES = ("PYTHON", "LD_")

SUBPROCESS_ENV_FILTER_ENABLED = os.environ.get(
    "SUBPROCESS_ENV_FILTER_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")


def build_subprocess_env(
    env_vars: Dict[str, str], sandbox_home: Optional[str] = None
) -> Dict[str, str]:
    """行程 env 去掉 denylist → 疊上本輪注入值 → PATH 補償(Linux-only)。

    sandbox_home:uid sandbox 模式下,把家目錄 / 暫存 / 快取導到 work_dir
    (sandbox uid 沒有 /etc/passwd 項目,也寫不進 /root)。
    """
    env = os.environ.copy()
    if SUBPROCESS_ENV_FILTER_ENABLED:
        for key in SUBPROCESS_ENV_DENYLIST:
            env.pop(key, None)
    env.update(env_vars)  # OBO tokens / user-provided credentials

    extra_paths = [p for p in ("/usr/bin", "/usr/local/bin") if os.path.exists(p)]
    if extra_paths:
        env["PATH"] = os.pathsep.join(extra_paths) + os.pathsep + env.get("PATH", "")

    if sandbox_home:
        env.update({
            "HOME": sandbox_home,
            "TMPDIR": sandbox_home,
            "MPLCONFIGDIR": os.path.join(sandbox_home, ".mpl"),
            "XDG_CACHE_HOME": os.path.join(sandbox_home, ".cache"),
            "USER": "sandbox",
            "LOGNAME": "sandbox",
        })
    return env


# ============================================================================
# Per-execution uid sandbox(S2)
# ============================================================================
# 容器主程式以 root 執行;開啟後每次 execute 以一個新的 uid 跑腳本,讓腳本讀不到
# /proc/1/environ(平台 secret)與其他 session 的 work_dir。2026-09-26 已在 ACA 實測可行。
# Managed Identity 的隔離見下方 MI_GATE_ENABLED(S3)。

SUBPROCESS_UID_SANDBOX = os.environ.get(
    "SUBPROCESS_UID_SANDBOX", "false"
).strip().lower() in ("true", "1", "yes", "on")

# S3:沒有 S2 時腳本讀得到 /proc/1/environ 的真 IDENTITY_HEADER,閘門可被繞過
_MI_GATE_RAW = os.environ.get("MI_GATE_ENABLED")
MI_GATE_ENABLED = (_MI_GATE_RAW or "true").strip().lower() in ("true", "1", "yes", "on")
MI_GATE_EXPLICIT = _MI_GATE_RAW is not None

_SANDBOX_UID_BASE = 200_000
_KILL_UID_ATTEMPTS = 5


def _live_pids_of_uid(uid: int) -> List[int]:
    """real uid == uid 且非 zombie 的行程。"""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status") as f:
                status = f.read()
        except OSError:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        if fields.get("State", "").split()[:1] == ["Z"]:
            continue
        if fields.get("Uid", "-1").split()[0] == str(uid):
            pids.append(int(entry))
    return pids


def _kill_uid(uid: int) -> None:
    """殺掉該 uid 的所有行程(含 setsid 逃出 process group 的)。

    以該 uid 身分呼叫 kill(-1):kernel 一次掃過所有可送訊號的行程,fork 再快也逃不掉。
    殺不乾淨就 raise —— 還有 sandbox 行程活著時,接下來的 root 檔案處理不安全。
    """
    for _ in range(_KILL_UID_ATTEMPTS):
        if not _live_pids_of_uid(uid):
            return
        subprocess.run(
            [sys.executable, "-S", "-c", "import os, signal; os.kill(-1, signal.SIGKILL)"],
            user=uid, group=uid, extra_groups=[], env={}, cwd="/",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        time.sleep(0.05)
    remaining = _live_pids_of_uid(uid)
    if remaining:
        raise RuntimeError(f"sandbox uid {uid} still has {len(remaining)} live process(es)")


def _sanitize_and_own(work_dir: str, uid: int) -> None:
    """把 work_dir 整棵交給 uid(0700),途中移除 symlink / 非一般檔 / 多重連結檔。

    這棵樹曾經對 sandbox 可寫,之後 root 會在裡面寫 script、讀產出上傳;
    留下的 symlink 會讓 root 讀寫到樹外(例如把 /proc/1/environ 當產出上傳)。
    呼叫前必須確定沒有 sandbox 行程還活著。
    """
    for root, dirs, files in os.walk(work_dir):
        for name in list(dirs) + files:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            mode = st.st_mode
            unsafe = (
                stat.S_ISLNK(mode)
                or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode))
                or (stat.S_ISREG(mode) and st.st_nlink > 1)
            )
            if unsafe:
                os.unlink(path)
                if name in dirs:
                    dirs.remove(name)
                continue
            os.chown(path, uid, uid, follow_symlinks=False)
    os.chown(work_dir, uid, uid, follow_symlinks=False)
    os.chmod(work_dir, 0o700)


def _harden_shared_dirs() -> None:
    """sandbox 開啟時:work_dir 父目錄不給列舉,materialize 根目錄搶先建成 root 0700。"""
    targets = [
        (os.getcwd(), 0o711),
        (os.environ.get("SKILLS_MATERIALIZE_BASE", "/tmp/openclaw_skills"), 0o700),
    ]
    for path, mode in targets:
        try:
            if path == "/":
                continue
            os.makedirs(path, exist_ok=True)
            os.chmod(path, mode)
        except OSError as e:
            logger.error(f"[Sandbox] failed to chmod {path} to {oct(mode)}: {e}")


def _apply_mi_gate(
    env: Dict[str, str], proxy, mi_scopes: Optional[Collection[str]], label: str
) -> Optional[str]:
    """拿掉平台 MI;有可放行的 scope 時改指向 proxy。回傳要在結束時撤銷的 header。"""
    for key in MI_ENV_KEYS:
        env.pop(key, None)
    if mi_scopes is None:
        logger.warning(f"[MIGate] {label}: no skill context (static mode) — Managed Identity denied")
        return None
    if not mi_scopes:
        logger.info(f"[MIGate] {label}: no loaded skill declares mi_scopes — Managed Identity denied")
        return None
    if proxy is None:
        logger.error(f"[MIGate] {label}: MI proxy not running — Managed Identity denied")
        return None
    header = proxy.grant(mi_scopes, label)
    if header is None:
        return None
    env["IDENTITY_ENDPOINT"] = proxy.endpoint
    env["IDENTITY_HEADER"] = header
    logger.info(f"[MIGate] {label}: Managed Identity via proxy for {sorted(mi_scopes)}")
    return header

def filter_caller_env(
    credentials: Dict[str, str],
    protected: Collection[str],
    forbidden_values: Collection[str] = (),
) -> Tuple[Dict[str, str], List[str]]:
    """剔除呼叫端 credentials 中會蓋掉 protected(平台已注入)、使用保留名稱,
    或值含有 forbidden_values(例如本次請求的 Bearer token)的 key。

    Returns:
        (保留的 credentials, 被剔除的 key 清單)
    """
    if not SUBPROCESS_ENV_FILTER_ENABLED:
        return dict(credentials), []
    secrets = [s for s in forbidden_values if s]
    blocked = [
        key for key, value in credentials.items()
        if key in protected
        or key in _RESERVED_EXEC_ENV_NAMES
        or key.startswith(_RESERVED_EXEC_ENV_PREFIXES)
        or (isinstance(value, str) and any(s in value for s in secrets))
    ]
    kept = {k: v for k, v in credentials.items() if k not in blocked}
    return kept, blocked


# ============================================================================
# Content-error patterns
# ============================================================================
# 2026.07.20 George : content_error pattern 拆成 HARD / SOFT 兩組。
# 動機:框架用這些 pattern 掃 raw_stdout 判斷「returncode=0 但內容其實出錯」。
# 但 raw_stdout 通常也含腳本正常 print 出來的 JSON 結果 payload,裡頭的英文資料
# (如會議主題 "Japan West policy exception") 會誤命中 \bexception\b / \bfailed\b /
# Error: 這類自然語言 pattern → 任務其實成功 (已寫入 Azure SQL) 卻被降級成
# content_error。
#
# 拆法:
# - HARD (結構化錯誤訊號):任何情況都掃。API 錯誤 JSON "error":、HTTP 4xx/5xx、
#   缺套件等,即使包在 JSON payload 裡也該視為錯誤。
# - SOFT (自然語言字):只在 stdout「不是」單一 JSON 文件時才掃。當整段 stdout 是
#   合法 JSON (dict/list) 時,這些字幾乎必然是資料內容而非診斷訊息,故略過以免誤判。
#   若 stdout 摻雜 log 雜訊使 json.loads 失敗,則退回掃全部 pattern,行為與舊版
#   一致 (不退化)。
#
# 2026.09.23 George : 由 list 改為 dict(pattern → 給終端使用者看的描述)。
# 唯一動機是呈現層 — 斷路器 advisory 原本直接把裸正則印給使用者
# (如 "命中訊號:\b(?:status[_ ]?code|statusCode|...)"),再疊上 MCP 的雙層 JSON
# 轉義後可讀性歸零。掃描邏輯與 pattern 內容一字未動:下方 execute() 以
# list(HARD_ERROR_PATTERNS) 取 key,dict 保序故行為與改動前完全相同。

HARD_ERROR_PATTERNS: Dict[str, str] = {
    r'"error":': '回應 JSON 含 "error" 欄位',
    r'"status":\s*".*error"': '回應 JSON 的 status 欄位值含 error',
    r'\bHTTP[/ ]\d(?:\.\d)?\s+(?:4\d{2}|5\d{2})\b': '輸出含 HTTP 4xx/5xx 狀態列',
    r'\b(?:status[_ ]?code|statusCode|status|code)\s*[:=]\s*(?:4\d{2}|5\d{2})\b': '輸出含 status_code / code 等於 4xx 或 5xx',
    r'\b(?:4\d{2}|5\d{2})\s+(?:Unauthorized|Forbidden|Not\s+Found|Internal\s+Server\s+Error|Bad\s+Request|Bad\s+Gateway|Service\s+Unavailable|Gateway\s+Timeout|Conflict|Too\s+Many\s+Requests)\b': '輸出含 401 Unauthorized / 502 Bad Gateway 這類狀態片語',
    r'Missing dependencies?:': '輸出含「缺少相依套件」訊息',
    r'ModuleNotFoundError': '輸出含 ModuleNotFoundError',
    r'ImportError': '輸出含 ImportError',
}

SOFT_ERROR_PATTERNS: Dict[str, str] = {
    r'Error:': '輸出中出現 "Error:" 字樣',
    r'\bfailed\b': '輸出中出現 "failed" 字樣',
    r'\bexception\b': '輸出中出現 "exception" 字樣',
}

# 附加在描述後的分組註記 — 讓看到 advisory 的人有依據自行研判誤判傾向。
_HARD_CAVEAT = '(結構化錯誤訊號)'
_SOFT_CAVEAT = '(此字常見於查詢回傳的資料內容)'


def describe_pattern(pattern: str) -> str:
    """把 error pattern 正則轉成給終端使用者看的描述。

    查不到的 pattern 原樣回傳 — 未來有人臨時加 pattern、或舊 state 反序列化帶進
    已移除的 pattern 時,行為退化成改動前的樣子而不是拋錯或吐空字串。
    """
    if pattern in HARD_ERROR_PATTERNS:
        return f"{HARD_ERROR_PATTERNS[pattern]}{_HARD_CAVEAT}"
    if pattern in SOFT_ERROR_PATTERNS:
        return f"{SOFT_ERROR_PATTERNS[pattern]}{_SOFT_CAVEAT}"
    return pattern


# ============================================================================
# Result / Status types
# Phase 0 保留 code_agent_hosted.py v10.1 的 ExecutionStatus / ExecutionResult
# 結構,只是搬位置。CodingAgent loop 仍消費 .agent_message 字串。
# ============================================================================

class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    CONTENT_ERROR = "content_error"   # returncode=0 但 stdout 含 error pattern
    FAILED = "failed"                  # returncode != 0
    TIMEOUT = "timeout"
    EXCEPTION = "exception"            # subprocess 本身拋例外


@dataclass
class ExecutionResult:
    """execute() 的結構化回傳值。

    agent_message 是給 agent loop 消費的純字串(維持 v10.0 字串格式)。
    其餘欄位供 DEBUG_MODE bundle upload 與未來 eval pipeline 使用。
    """
    status: ExecutionStatus
    agent_message: str                           # agent loop 消費這個
    raw_stdout: str                              # 未截斷原始 stdout
    raw_stderr: str                              # 未截斷原始 stderr
    returncode: int
    script_path: str                             # 實際寫入的 script_v{n}.py 路徑
    script_code: str                             # 已 strip markdown fence 的純 code
    execution_count: int                         # state.execution_count 的快照
    truncated: bool = False                      # stdout 是否被截斷過
    error_pattern_hits: Dict[str, int] = field(default_factory=dict)
    override_applied: bool = False
    succeeded_count: int = 0
    # 2026.06.06 George: stdout 含 [NEEDS_INFO] marker 時為 True。
    # 腳本以 [NEEDS_INFO] 行 + SystemExit(0) 回報「需使用者補輸入」,
    # caller 可據此設下游 needs_input flag,修正 turn 被記成 success 的語意落差。
    needs_input: bool = False
    # Phase 0 新增:本次執行新產出的檔案路徑列表
    # 由 executor 負責偵測(用 glob diff 邏輯),回傳給 caller 由 OutputFileStore 上傳
    new_output_files: list = field(default_factory=list)


# ============================================================================
# CodeExecutor Protocol
# ============================================================================

class CodeExecutor(Protocol):
    """執行 Python code 的抽象介面。

    Phase 0 階段只有一個實作 LocalSubprocessExecutor。
    階段 2 才會出現 HostedAgentExecutor。
    """

    async def execute(
        self,
        code: str,
        session_id: str,
        work_dir: str,
        execution_count: int,
        env_vars: Dict[str, str],
        timeout: int = 120,
        mi_scopes: Optional[Collection[str]] = None,
        script_relpath: Optional[str] = None,
        argv: Optional[List[str]] = None,
    ) -> ExecutionResult:
        """執行 code,回傳結構化結果。

        Args:
            code: Python 程式碼 (可含 markdown fence,內部會 strip)
            session_id: 應用層 session ID。Phase 3 新增。
                       用於 _running_procs[session_id] 註冊,讓
                       cancel(session_id) 能找到對應的 Process 物件。
            work_dir: 執行目錄 (script 會寫到這裡,新檔案也會偵測這裡)
            execution_count: 本次是 session 的第幾次執行
                            (用於命名 script_v{N}.py)
            env_vars: 注入給 subprocess 的環境變數
                     (含 user_data 裡的 OBO tokens / API keys)
            timeout: 單輪 subprocess 超時 (秒),預設 120。
                     ⚠️ 這是「單一 cell 抓卡死」的上限,與 core_handler 的
                     ADAPTIVE_TIMEOUT_SECONDS (80s 同步等待窗口) 是不同概念、
                     無賽跑關係。caller 實際傳入值來自
                     CODE_EXECUTION_TIMEOUT_SECONDS 環境變數。
                     詳見 code_agent_hosted.py 該變數宣告處的完整說明。
            mi_scopes: 本輪已載入 skill 宣告的 metadata.mi_scopes 聯集。
                     None = 沒有 skill context(static 模式)。只在 MI_GATE_ENABLED 時生效。
            script_relpath: script 型 skill 的腳本落地位置(相對 work_dir);code 原樣寫入
            argv: 腳本命令列參數(僅 script_relpath 模式使用)

        Returns:
            ExecutionResult — 包含 agent_message (給 LLM 看的字串)
                              + 完整 raw_stdout/stderr 等 debug 用欄位
                              + new_output_files (本次產生的檔案路徑列表)
        """
        ...

    async def cancel(self, session_id: str) -> bool:
        """嘗試取消當前 session 正在執行的 code。

        對應主文件 §5.7.2 cooperative cancellation:
        - SIGTERM 通知 subprocess 優雅收尾
        - 30 秒 grace period
        - 逾時則 SIGKILL 強制結束

        Phase 3 caller:cancel_pending_task MCP tool。

        Returns:
            True if cancellation was issued; False if nothing to cancel.
        """
        ...


# ============================================================================
# LocalSubprocessExecutor — 階段 1 實作
# ============================================================================

class LocalSubprocessExecutor:
    """本機 subprocess 執行 (從 code_agent_hosted.py execute_code() 搬出來)。

    Phase 0 (零變更):
    - subprocess.run() 的呼叫參數、env 處理、PATH 補償、glob diff
      偵測新檔案、stdout 截斷、content_error pattern 判斷、succeeded
      override 等,全部維持 v10.x 既有行為。

    Phase 3 (本版本):
    - subprocess.run() → asyncio.create_subprocess_exec() + wait_for(communicate())。
      動機:cancel 機制的前置條件 — subprocess.run() 是 blocking call,
      無法從外部中斷;改用 async subprocess 才能讓 cancel(session_id) 在
      另一個 coroutine 中觸發 SIGTERM。
    - execute() 簽章加 session_id,內部把 Process 物件註冊到
      self._running_procs[session_id],try/finally 結束時移除。
    - cancel() 從 placeholder 改為實作 SIGTERM → 30s grace → SIGKILL。
    - 清掉 os.environ["PATH"] = new_path 這行 v9.0 既有 process-level
      side effect (env["PATH"] 仍保留,給 subprocess 用)。

    其他既有行為 100% 保留:
    - script 命名 script_v{N}.py
    - stdout > 5000 chars 截斷 (頭尾各 2000)
    - content_error pattern 11 條
    - succeeded_count override (cond_a: ≥3 且 >error*5,cond_b: ≥1 且 ≤2)
    - glob diff 偵測新檔案 (篩 OUTPUT_EXTENSIONS)
    - ExecutionResult 各狀態的 agent_message 格式
    """

    def __init__(self):
        # Phase 3: _running_procs key=session_id, value=asyncio.subprocess.Process
        # execute() 進入時註冊,try/finally 結束時移除,cancel() 從這裡查 Process。
        # 同一 session 重入 execute() (理論上不會發生,因為 turn loop 序列化)
        # 走 overwrite 策略並 log warning,見 execute() 內註解。
        self._running_procs: Dict[str, asyncio.subprocess.Process] = {}
        # sandbox 模式下的 proc 是 process group leader,訊號要送給整個 group
        self._group_pids: Set[int] = set()
        self._uid_counter = itertools.count(_SANDBOX_UID_BASE)

        if SUBPROCESS_UID_SANDBOX:
            if os.geteuid() != 0:
                logger.critical(
                    "[Sandbox] SUBPROCESS_UID_SANDBOX=true but process is not root — "
                    "every execution will fail"
                )
            _harden_shared_dirs()
            logger.info("[Sandbox] per-execution uid sandbox ENABLED")

        self._mi_gate = False
        self._mi_proxy = None
        if MI_GATE_ENABLED:
            if SUBPROCESS_UID_SANDBOX:
                self._mi_gate = True
            elif MI_GATE_EXPLICIT:
                logger.critical(
                    "[MIGate] MI_GATE_ENABLED=true requires SUBPROCESS_UID_SANDBOX=true — "
                    "gate NOT enabled; scripts keep the platform Managed Identity"
                )
            else:
                logger.warning(
                    "[MIGate] inactive until SUBPROCESS_UID_SANDBOX=true — "
                    "scripts keep the platform Managed Identity"
                )

    async def start_mi_proxy(self) -> None:
        if not self._mi_gate:
            return
        from mi_proxy import MiProxy, load_allowlist

        allowlist = load_allowlist()
        try:
            proxy = MiProxy(allowlist)
            await proxy.start()
        except Exception as e:
            logger.critical(
                f"[MIGate] MI proxy failed to start ({type(e).__name__}: {e}) — "
                "Managed Identity is denied to every script",
                exc_info=True,
            )
            return
        self._mi_proxy = proxy
        logger.info(f"[MIGate] ENABLED, proxy on 127.0.0.1:{proxy.port}, allowlist={sorted(allowlist)}")

    async def close(self) -> None:
        if self._mi_proxy is not None:
            await self._mi_proxy.stop()
            self._mi_proxy = None

    def _signal(self, proc: asyncio.subprocess.Process, sig_name: str) -> None:
        if proc.pid in self._group_pids:
            os.killpg(proc.pid, getattr(signal, sig_name))
        elif sig_name == "SIGKILL":
            proc.kill()
        else:
            proc.terminate()

    async def execute(
        self,
        code: str,
        session_id: str,
        work_dir: str,
        execution_count: int,
        env_vars: Dict[str, str],
        timeout: int = 120,
        mi_scopes: Optional[Collection[str]] = None,
        script_relpath: Optional[str] = None,
        argv: Optional[List[str]] = None,
    ) -> ExecutionResult:
        """執行 code,回傳結構化結果。

        script_relpath 有值時(script 型 skill):code 原樣寫到 work_dir 下該路徑,
        不剝 markdown fence、不用 script_v{N}.py,並以 argv 當命令列參數執行。

        Phase 3 變更摘要 (與 Phase 0 對照):
        1. 簽章新增 session_id (第二個位置參數),用於 _running_procs 註冊。
        2. subprocess.run() → asyncio.create_subprocess_exec() + wait_for。
        3. timeout 路徑改為手動 kill + drain (取代 stdlib 自動處理)。
        4. 進入時把 Process 註冊到 _running_procs,try/finally 結束時移除。
        5. os.environ["PATH"] mutation 已移除 (Step 3 內)。

        其他既有 v10.x 行為 100% 保留 (見 class docstring)。
        """
        # ──────────────────────────────────────────────────────────
        # Step 1: Strip markdown fence (與既有邏輯相同)
        # ──────────────────────────────────────────────────────────
        if script_relpath is None:
            code = code.strip()
            for prefix in ["```python", "```"]:
                if code.startswith(prefix):
                    code = code[len(prefix):]
            if code.endswith("```"):
                code = code[:-3]
            code = code.strip()
            script_path = os.path.join(work_dir, f"script_v{execution_count}.py")
        else:
            script_path = os.path.join(work_dir, script_relpath)

        # S2: 先把 work_dir 清乾淨並交給本次 uid,之後 root 才在裡面寫 script
        sandbox_uid: Optional[int] = None
        if SUBPROCESS_UID_SANDBOX:
            sandbox_uid = next(self._uid_counter)
            try:
                await asyncio.to_thread(_sanitize_and_own, work_dir, sandbox_uid)
            except Exception as e:
                logger.error(f"[Sandbox] prepare failed for uid={sandbox_uid}: {e}", exc_info=True)
                return ExecutionResult(
                    status=ExecutionStatus.EXCEPTION,
                    agent_message=f"❌ EXECUTION FAILED: sandbox prepare error: {e}",
                    raw_stdout="", raw_stderr=traceback.format_exc(), returncode=-1,
                    script_path=script_path, script_code=code,
                    execution_count=execution_count,
                )

        # ──────────────────────────────────────────────────────────
        # Step 2: 寫入 script 檔案
        # 命名規則 script_v{N}.py 維持既有,供 debug bundle 與 audit 用
        # ──────────────────────────────────────────────────────────
        if script_relpath is not None:
            os.makedirs(os.path.dirname(script_path), exist_ok=True)
        with open(script_path, "w", encoding="utf-8", newline="" if script_relpath else None) as f:
            f.write(code)

        # ──────────────────────────────────────────────────────────
        # Step 3: 組 env (denylist + user_data 注入 + PATH 補償)
        # ──────────────────────────────────────────────────────────
        env = build_subprocess_env(
            env_vars, sandbox_home=work_dir if sandbox_uid is not None else None
        )

        # ──────────────────────────────────────────────────────────
        # Step 4: 記錄 work_dir 執行前的檔案集合 (供 glob diff)
        # ──────────────────────────────────────────────────────────
        files_before = set(glob.glob(os.path.join(work_dir, '*')))

        # ──────────────────────────────────────────────────────────
        # Step 5: 共用 result factory (與既有 _make_result 相同)
        # 確保 ExecutionResult 欄位完整,避免遺漏 script_code / execution_count
        # ──────────────────────────────────────────────────────────
        def _make_result(
            status: ExecutionStatus,
            agent_message: str,
            raw_stdout: str = "",
            raw_stderr: str = "",
            returncode: int = -1,
            truncated: bool = False,
            error_pattern_hits: Optional[Dict[str, int]] = None,
            override_applied: bool = False,
            succeeded_count: int = 0,
            needs_input: bool = False,
            new_output_files: Optional[list] = None,
        ) -> ExecutionResult:
            return ExecutionResult(
                status=status,
                agent_message=agent_message,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
                returncode=returncode,
                script_path=script_path,
                script_code=code,
                execution_count=execution_count,
                truncated=truncated,
                error_pattern_hits=error_pattern_hits or {},
                override_applied=override_applied,
                succeeded_count=succeeded_count,
                needs_input=needs_input,
                new_output_files=new_output_files or [],
            )

        # ──────────────────────────────────────────────────────────
        # Step 6: 跑 subprocess + 處理各種結果
        # 2026.05.18 George : Phase 3 — 從 subprocess.run 改成
        # asyncio.create_subprocess_exec + wait_for(communicate())。
        #
        # 重構動機 (對應主文件 §5.7 cancel 機制):
        # - subprocess.run() 是 blocking call,無法從外部中斷。
        # - 改 asyncio 後,cancel(session_id) 可從另一個 coroutine 中
        #   呼叫 proc.terminate() 觸發 SIGTERM 中斷正在跑的 subprocess。
        #
        # 既有 v10.x 行為 100% 保留:
        # - returncode 判讀邏輯不變
        # - stdout 截斷 (5000 char,頭尾各 2000)
        # - content_error pattern 11 條
        # - succeeded_count override (cond_a + cond_b)
        # - glob diff 偵測新檔案
        # - 各狀態 agent_message 格式
        #
        # _running_procs lifecycle 管理:
        # - 進入時註冊 self._running_procs[session_id] = proc
        # - try/finally 結束時無條件 pop,避免 leak
        # - 同 session 重入 (理論上不會發生,turn loop 序列化) 走 overwrite
        #   並 log warning;這代表上游 bug,但 executor 不主動拒絕
        # ──────────────────────────────────────────────────────────

        # 同 session 重入檢查 (理論上不會發生)
        if session_id in self._running_procs:
            logger.warning(
                f"[Execution] session={session_id} already has a running proc "
                f"in _running_procs (overwriting). This indicates upstream "
                f"concurrency control failure — should not happen if §5.5 "
                f"parallel control is honored."
            )

        spawn_kwargs = {}
        if sandbox_uid is not None:
            spawn_kwargs = dict(
                user=sandbox_uid, group=sandbox_uid, extra_groups=[],
                umask=0o077, start_new_session=True,
            )

        reclaimed = sandbox_uid is None

        async def _reclaim() -> None:
            # 殺光 sandbox 行程 → 清樹 → 收回 root;之後 root 才能安全讀產出
            nonlocal reclaimed
            if reclaimed:
                return
            await asyncio.to_thread(_kill_uid, sandbox_uid)
            await asyncio.to_thread(_sanitize_and_own, work_dir, 0)
            reclaimed = True

        mi_header = None
        if self._mi_gate:
            mi_header = _apply_mi_gate(
                env, self._mi_proxy, mi_scopes, f"{session_id}/v{execution_count}"
            )

        proc = None  # 給 except 路徑使用,確保 NameError 不會發生
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script_path, *(argv or []),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=env,
                **spawn_kwargs,
            )
            if sandbox_uid is not None:
                self._group_pids.add(proc.pid)
            self._running_procs[session_id] = proc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # ──────────────────────────────────────────────────
                # TIMEOUT 路徑:對齊 stdlib subprocess.run(timeout=...) 行為
                # stdlib 在 TimeoutExpired 時會自動 kill + drain,並把
                # partial output 塞進 exception 物件。asyncio 沒有這個自動
                # 行為,需要手動處理。
                # ──────────────────────────────────────────────────
                logger.warning(
                    f"[Execution] v{execution_count} subprocess timeout after "
                    f"{timeout}s, killing and draining streams"
                )

                # Kill (SIGKILL,不像 cancel 路徑給 grace period — 已 timeout
                # 不該再拖)
                try:
                    self._signal(proc, "SIGKILL")
                except ProcessLookupError:
                    # 罕見:proc 在 kill() 與檢查之間自己結束了
                    pass

                # Drain stream (2 秒上限,對齊 stdlib 行為)
                partial_stdout = ""
                partial_stderr = ""
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=2.0,
                    )
                    partial_stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
                    partial_stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
                except asyncio.TimeoutError:
                    # Drain 也 timeout — partial output 拿不到了
                    logger.warning(
                        f"[Execution] v{execution_count} stream drain timed "
                        f"out after kill, partial output unavailable"
                    )

                await _reclaim()
                return _make_result(
                    status=ExecutionStatus.TIMEOUT,
                    agent_message=f"❌ EXECUTION FAILED: Timeout after {timeout}s",
                    raw_stdout=partial_stdout,
                    raw_stderr=partial_stderr,
                    returncode=-1,
                )

            # 正常完成 (returncode 0 或非 0,但沒 timeout)
            raw_stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            raw_stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

            await _reclaim()

            # 偵測本次新產生的 output 檔案 (glob diff)
            files_after = set(glob.glob(os.path.join(work_dir, '*')))
            new_files = [
                f for f in (files_after - files_before)
                if os.path.splitext(f)[1].lower() in OUTPUT_EXTENSIONS
            ]

            if proc.returncode == 0:
                # ──────────────────────────────────────────────────
                # returncode == 0 分支: 處理 stdout 截斷 + content_error 判斷
                # ──────────────────────────────────────────────────
                agent_stdout = raw_stdout
                stdout_truncated = False
                if len(agent_stdout) > MAX_STDOUT_LENGTH:
                    # v9.1 截斷邏輯:保留頭尾各 2000 字元,中間省略
                    head_len = 2000
                    tail_len = 2000
                    omitted = len(raw_stdout) - head_len - tail_len
                    agent_stdout = (
                        raw_stdout[:head_len]
                        + f"\n\n... ⚠️ OUTPUT TRUNCATED: 省略了中間 {omitted} 字元 "
                        + f"(原始長度 {len(raw_stdout)} 字元) ...\n\n"
                        + raw_stdout[-tail_len:]
                    )
                    stdout_truncated = True
                    logger.warning(
                        f"[Execution] stdout truncated: "
                        f"{len(raw_stdout)} → {len(agent_stdout)} chars"
                    )

                # v9.1/v10.2 content_error pattern 偵測
                # HARD / SOFT 分組的理由見檔案上方 HARD_ERROR_PATTERNS 定義處。
                hard_error_patterns = list(HARD_ERROR_PATTERNS)
                soft_error_patterns = list(SOFT_ERROR_PATTERNS)

                # 判斷整段 stdout 是否為單一 JSON payload (dict/list)。
                # 只有純 JSON (沒有前後 log 雜訊) 才會 parse 成功。
                is_json_payload = False
                stripped_stdout = raw_stdout.strip()
                if stripped_stdout:
                    try:
                        parsed_stdout = json.loads(stripped_stdout)
                        is_json_payload = isinstance(parsed_stdout, (dict, list))
                    except (json.JSONDecodeError, ValueError):
                        is_json_payload = False

                if is_json_payload:
                    content_error_patterns = hard_error_patterns
                    logger.info(
                        "[Execution] stdout is a single JSON payload — "
                        "suppressing soft error patterns (exception/failed/Error:) "
                        "to avoid false positives on data content"
                    )
                else:
                    content_error_patterns = hard_error_patterns + soft_error_patterns

                # 對 raw_stdout 做 match (v10.1 修正:避免截斷邊界破壞 word boundary)
                pattern_hits: Dict[str, int] = {}
                for p in content_error_patterns:
                    matches = re.findall(p, raw_stdout, re.IGNORECASE)
                    if matches:
                        pattern_hits[p] = len(matches)

                has_content_error = bool(pattern_hits)
                error_hit_count = sum(pattern_hits.values())

                # 2026.06.06 George: needs-info marker 免疫
                # ────────────────────────────────────────────────────────────
                # 腳本以 [NEEDS_INFO] 行 + SystemExit(0) 回報「需使用者補輸入」。
                # 此時退出碼為 0,但缺漏的 var 名 / region 字串可能碰巧命中上面的
                # 禁字 pattern(如含 'failed'、4xx code)。只要 stdout 任一行以
                # [NEEDS_INFO] 開頭,即視為 needs-info 意圖,跳過 content_error 降級,
                # 並標記 needs_input。否則訊息會被降成 ❌ CONTENT_ERROR、掉回失敗
                # 路徑,最後在 HITL 分支被換成籠統提示而遺失真實缺漏清單。
                needs_input_signal = any(
                    line.lstrip().startswith("[NEEDS_INFO]")
                    for line in raw_stdout.splitlines()
                )
                if needs_input_signal:
                    if has_content_error:
                        logger.info(
                            "[Execution] [NEEDS_INFO] marker present — skipping "
                            f"content_error downgrade (pattern_hits={error_hit_count})"
                        )
                    has_content_error = False

                # v10.2 succeeded override 邏輯
                # 場景:list models 回 100 筆 succeeded,某個 model 名稱碰巧含 error 字
                # 場景:Fabric Data Agent run completed,但 debug dump 的 GUID 湊出 HTTP code
                success_signal_patterns = [
                    r'"status":\s*"succeeded"',
                    r'"status":\s*"completed"',
                    r'✅\s*Final status:\s*completed',
                    r'"state":\s*"Succeeded"',
                    r'"provisioningState":\s*"Succeeded"',
                ]
                succeeded_count = 0
                override_applied = False
                if has_content_error:
                    for p in success_signal_patterns:
                        succeeded_count += len(re.findall(p, raw_stdout, re.IGNORECASE))

                    # 條件 A: 大量 succeeded 輾壓 error
                    cond_a = succeeded_count >= 3 and succeeded_count > error_hit_count * 5
                    # 條件 B: 明確權威成功訊號 + 低雜訊 (v10.2 新增)
                    cond_b = succeeded_count >= 1 and error_hit_count <= 2

                    if cond_a or cond_b:
                        logger.info(
                            f"[Execution] Overriding content_error "
                            f"(cond_a={cond_a}, cond_b={cond_b}): "
                            f"succeeded_count={succeeded_count}, "
                            f"error_hits={error_hit_count}, "
                            f"treating as normal API response"
                        )
                        has_content_error = False
                        override_applied = True

                if has_content_error:
                    # CONTENT_ERROR: returncode=0 但 stdout 含未被 override 的 error 訊號
                    return _make_result(
                        status=ExecutionStatus.CONTENT_ERROR,
                        agent_message=f"❌ EXECUTION COMPLETED BUT WITH ERRORS:\n\n{agent_stdout}",
                        raw_stdout=raw_stdout,
                        raw_stderr=raw_stderr,
                        returncode=0,
                        truncated=stdout_truncated,
                        error_pattern_hits=pattern_hits,
                        succeeded_count=succeeded_count,
                        new_output_files=new_files,
                    )

                # SUCCESS path
                truncated_note = " (⚠️ OUTPUT_TRUNCATED)" if stdout_truncated else ""
                return _make_result(
                    status=ExecutionStatus.SUCCESS,
                    agent_message=f"✅ EXECUTION SUCCESSFUL{truncated_note}:\n\n{agent_stdout}",
                    raw_stdout=raw_stdout,
                    raw_stderr=raw_stderr,
                    returncode=0,
                    truncated=stdout_truncated,
                    error_pattern_hits=pattern_hits,
                    override_applied=override_applied,
                    succeeded_count=succeeded_count,
                    needs_input=needs_input_signal,
                    new_output_files=new_files,
                )
            else:
                # FAILED: returncode != 0
                full_error = raw_stderr or raw_stdout or "Unknown error"
                truncated_error = (
                    f"...\n{full_error[-500:]}" if len(full_error) > 500 else full_error
                )
                logger.error(f"[Execution] v{execution_count} ❌ FAILED")
                return _make_result(
                    status=ExecutionStatus.FAILED,
                    agent_message=f"❌ EXECUTION FAILED (code {proc.returncode}):\n\n{truncated_error}",
                    raw_stdout=raw_stdout,
                    raw_stderr=raw_stderr,
                    returncode=proc.returncode,
                    truncated=False,
                    new_output_files=new_files,
                )

        except Exception as e:
            # 涵蓋 create_subprocess_exec 本身可能拋的例外
            # (FileNotFoundError、PermissionError 等),以及上面正常路徑
            # 內未預期的例外。
            # 注意:asyncio.TimeoutError 已經在內層 try 處理掉,不會傳到這裡。
            logger.error(
                f"[Execution] v{execution_count} exception during subprocess "
                f"execution: {type(e).__name__}: {e}",
                exc_info=True,
            )
            return _make_result(
                status=ExecutionStatus.EXCEPTION,
                agent_message=f"❌ EXECUTION FAILED: {str(e)}",
                raw_stderr=traceback.format_exc(),
                returncode=-1,
            )
        finally:
            if mi_header is not None:
                self._mi_proxy.revoke(mi_header)
            # 無條件清掉 _running_procs[session_id],避免 leak。
            # 即使 cancel() 已經在另一個 coroutine 中先 pop 走,這裡 pop
            # default=None 也安全。
            self._running_procs.pop(session_id, None)
            if proc is not None:
                self._group_pids.discard(proc.pid)
            if not reclaimed:
                try:
                    await _reclaim()
                except Exception as e:
                    logger.error(
                        f"[Sandbox] reclaim failed for uid={sandbox_uid}, work_dir left "
                        f"owned by sandbox uid: {e}",
                        exc_info=True,
                    )

    async def cancel(self, session_id: str) -> bool:
        """Cooperative cancellation:SIGTERM → 30s grace → SIGKILL。

        對應主文件 §5.7.2 設計:
        - 先 SIGTERM 讓 subprocess 有機會優雅收尾 (e.g. flush 檔案、釋放
          資源)
        - 30 秒 grace period
        - 逾時 SIGKILL 強制結束,Job Store 端附 `cancel_warning:
          subprocess_force_killed` (主文件 §5.7.4),由 caller 處理

        Phase 3 caller:cancel_pending_task MCP tool。雙路徑設計
        (主文件 §13.4 + 本檔案 Q4):
        - 路徑 1:execute() 正在跑,本 method 中斷它,觸發 execute() 的
          asyncio.TimeoutError 或 returncode != 0 路徑,讓 turn loop 收到
          結果並收尾
        - 路徑 2:turn 間沒 proc 在跑,本 method 回 False (no-op),cancel
          flag 已由 caller 寫入 Job Store,turn loop 下個 iteration 的
          checkpoint 會偵測到並退出

        ⚠️ 並發語意:
        本 method 與 execute() 是不同的 coroutine。proc.terminate() / kill()
        是 atomic 的 syscall,沒有 race 問題;_running_procs[session_id] 的
        get/pop 也是 atomic 的 dict op。execute() 的 finally 區塊也會 pop
        _running_procs[session_id],兩邊都 pop default=None,沒有 race。

        Args:
            session_id: 要取消的 session ID。

        Returns:
            True 表示有發送中斷訊號(SIGTERM 或 SIGKILL),
            False 表示沒有找到對應的 running proc (no-op)。
        """
        proc = self._running_procs.get(session_id)
        if proc is None:
            logger.info(
                f"[Cancel] session={session_id}: no running proc found "
                f"(already finished or never started)"
            )
            return False

        # 若 proc 已經自己結束 (returncode 不為 None),也視為 no-op
        if proc.returncode is not None:
            logger.info(
                f"[Cancel] session={session_id}: proc already exited with "
                f"returncode={proc.returncode}"
            )
            return False

        # 1. SIGTERM (優雅收尾請求)
        try:
            self._signal(proc, "SIGTERM")
            logger.info(
                f"[Cancel] session={session_id}: SIGTERM sent (pid={proc.pid})"
            )
        except ProcessLookupError:
            # Proc 在 terminate() 與檢查之間自己結束了
            logger.info(
                f"[Cancel] session={session_id}: proc already gone before "
                f"SIGTERM (ProcessLookupError)"
            )
            return False

        # 2. 30 秒 grace period
        try:
            await asyncio.wait_for(proc.wait(), timeout=30.0)
            logger.info(
                f"[Cancel] session={session_id}: proc exited gracefully "
                f"after SIGTERM (returncode={proc.returncode})"
            )
            return True
        except asyncio.TimeoutError:
            # 3. SIGKILL (強制結束,subprocess 沒有優雅收尾的權利了)
            logger.warning(
                f"[Cancel] session={session_id}: SIGTERM grace expired after "
                f"30s, sending SIGKILL (pid={proc.pid})"
            )
            try:
                self._signal(proc, "SIGKILL")
            except ProcessLookupError:
                # Proc 在 SIGTERM 與 SIGKILL 之間結束了
                logger.info(
                    f"[Cancel] session={session_id}: proc exited between "
                    f"SIGTERM and SIGKILL"
                )
                return True

            # 等 SIGKILL 生效 (一般 < 1s)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
                logger.info(
                    f"[Cancel] session={session_id}: proc force-killed "
                    f"(returncode={proc.returncode})"
                )
            except asyncio.TimeoutError:
                # SIGKILL 都不行?系統有問題,記 error
                logger.error(
                    f"[Cancel] session={session_id}: proc did not exit even "
                    f"after SIGKILL — system may be unresponsive"
                )

            return True