"""
My Claude Local Agent - General-Purpose Desktop AI Agent (ESP32 / Python & Web Dev /
Financial Analysis / Document Writing)
=====================================================================================

A private, local desktop client for the Anthropic Claude API that turns Claude
into an autonomous agent capable of, across one or more sandboxed "workspace"
folders:
  - Reading / writing / deleting text files (code, markdown, config, CSV, etc.)
  - Running arbitrary PowerShell commands (npm/pip/git/arduino-cli/esptool/etc.)
  - Running Python scripts it writes itself, using this app's own interpreter
    (so pandas/openpyxl/python-docx/pdfplumber are available for financial
    analysis, Excel/Word report generation, and PDF/CSV parsing)
  - Compiling/flashing an ESP32 and monitoring its USB serial port
  - General chat, research, and long-form writing when no tool is needed

Every destructive or execution-capable tool call is intercepted by a modal
"Approval Guard" dialog before it is allowed to run, unless you have marked
that specific tool as auto-approved for the session (or it is an inherently
read-only action).

Conversations are auto-saved to disk after every turn and can be resumed from
the "Chats" tab in the sidebar. Multiple workspace folders can be configured
in the "Workspaces" tab, and Claude selects among them by alias. The serial
monitor panel can be hidden entirely when you are not doing ESP32 work.

--------------------------------------------------------------------------
REQUIRED PACKAGES (run this once in an elevated / normal PowerShell prompt
on the Windows 11 machine, ideally inside a virtual environment):

    pip install --upgrade customtkinter anthropic pyserial pandas openpyxl python-docx pdfplumber

Python 3.9+ is recommended. Tested against:
    customtkinter >= 5.2
    anthropic     >= 1.4.0  (older versions lack client.beta.messages.create's
                             context_management parameter used for automatic
                             conversation compaction - the app detects this at
                             runtime and falls back gracefully, but "pip install
                             --upgrade anthropic" gets you the compaction feature)
    pyserial      >= 3.5
    pandas, openpyxl, python-docx, pdfplumber (latest)

Optional external tools that "run_shell_command" may be asked to invoke for
ESP32 work are assumed to already be installed and on the Windows PATH (they
are NOT bundled with this script):
    - arduino-cli   (https://arduino.github.io/arduino-cli/)
    - esptool.py    (pip install esptool)
    - node/npm      (for React/web front-end work)
--------------------------------------------------------------------------

Run with:
    python my_claude_agent_app.py
"""

# =========================================================================
# Imports
# =========================================================================
import os
import sys
import json
import time
import copy
import uuid
import threading
import subprocess
import traceback
import tempfile
import shutil
import collections
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    serial = None
    list_ports = None

try:
    import anthropic
except ImportError:
    anthropic = None


# =========================================================================
# Constants / configuration defaults
# =========================================================================
APP_TITLE = "My Claude Local Agent"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "agent_config.json")
CONVERSATIONS_DIR = os.path.join(SCRIPT_DIR, "conversations")
TRASH_DIR = os.path.join(CONVERSATIONS_DIR, ".trash")

DEFAULT_MODEL = "claude-sonnet-5"
AVAILABLE_MODELS = [
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1",
]

DEFAULT_BAUD = 115200
DEFAULT_MAX_TOKENS = 8192
DEFAULT_SHELL_TIMEOUT = 300     # seconds, for run_shell_command
DEFAULT_PYTHON_TIMEOUT = 120    # seconds, for run_python_code
SERIAL_BUFFER_LINES = 1000
MAX_TOOL_ITERATIONS = 25        # guard against runaway tool loops

# Claude's context window (input + output combined) as of this writing. If a
# future model supports a larger window this may need raising, but erring
# conservative here just means the warning below fires a bit early - it never
# fires late.
MODEL_CONTEXT_WINDOW = 200000
# Reserve room below the hard limit for the system prompt, tool schemas, the
# next response's max_tokens, and the roughness of the estimate below - so we
# refuse locally with a clear message before the API does with a cryptic one.
CONTEXT_SAFETY_MARGIN = 12000


def _rough_token_estimate(obj):
    """Very rough token-count estimate (~1 token per ~3.5 characters) used only
    to decide, locally, whether a request is about to exceed the model's
    context window - NOT an accurate token count, and not used for billing.
    Real tokenization varies by model and content type; this exists purely to
    fail fast with a clear message instead of wasting a doomed API call."""
    if isinstance(obj, str):
        text = obj
    else:
        try:
            text = json.dumps(obj, ensure_ascii=False)
        except Exception:
            text = str(obj)
    return len(text) // 3

# =========================================================================
# Server-side context compaction (beta)
# https://platform.claude.com/docs/en/build-with-claude/compaction
# Automatically summarizes older conversation content on Anthropic's servers
# once input tokens cross a threshold, so a long-running conversation can
# keep going instead of eventually hitting the raw "prompt is too long" error.
# Requires the 'anthropic' package to expose client.beta.messages.create()
# with a context_management parameter (checked at call time; falls back to a
# plain call with a one-time notice if the installed SDK is too old).
# =========================================================================
COMPACTION_BETA_HEADER = "compact-2026-01-12"
COMPACTION_STRATEGY_TYPE = "compact_20260112"

# Per Anthropic's compatibility table (Fable 5/5.1, Mythos 5/5.1/Preview,
# Opus 4.6-5, Sonnet 4.6-5). Haiku models are NOT supported - for those (or
# any model string not in this set, e.g. a future/custom one), the app falls
# back to a plain, non-compacting call plus the local pre-flight size check
# above as a safety net.
COMPACTION_SUPPORTED_MODELS = {
    "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-fable-5", "claude-fable-5-1",
    "claude-mythos-5", "claude-mythos-5-1", "claude-mythos-preview",
}

# This app always sends `tools` (it's fundamentally a tool-using agent), and
# Anthropic's docs specifically warn that with tools defined, the model can
# call a tool during the internal summarization step instead of writing a
# summary (producing a compaction block with content: null) unless told not
# to. This is their documented mitigation, verbatim.
COMPACTION_TOOL_SAFE_INSTRUCTIONS = (
    "Summarize the transcript inside <summary></summary> tags. Include the "
    "information needed to continue the task in the next context window - "
    "state, next steps, key decisions, file paths, and anything else "
    "load-bearing. Do not call any tools while writing this summary; "
    "respond with text only."
)

SYSTEM_PROMPT = (
    "You are Claude acting as a local, general-purpose autonomous assistant running "
    "on the user's own Windows 11 PC, with direct tool access to their filesystem, "
    "a PowerShell shell, a Python interpreter, and (for ESP32 hardware work) a USB "
    "serial port. You are used for a wide range of tasks: ESP32/embedded firmware "
    "development and flashing; building Python desktop GUI apps; building web "
    "front-ends (commonly React); financial analysis and reporting (reading CSV/PDF "
    "inputs, producing Excel/Word outputs); general writing in Word or Markdown; "
    "and ordinary conversation, research, or brainstorming.\n\n"
    "Billing: This application uses the Anthropic API. If the user has an active "
    "monthly subscription on their Anthropic account, it will be used automatically. "
    "Otherwise, requests are billed at the per-API-call rate.\n\n"
    "Tools available to you:\n"
    "  - manage_local_file: read/write/delete TEXT files (source code, markdown, "
    "CSV, config, etc.) inside a configured workspace folder. Do NOT use this to "
    "write binary files such as .xlsx/.docx/.pdf - use run_python_code for those.\n"
    "  - run_shell_command: run a PowerShell command line (npm, pip, git, "
    "arduino-cli, esptool.py, etc.) with the working directory set to a configured "
    "workspace folder. Output is streamed live and returned in full.\n"
    "  - run_python_code: run a Python script inside a configured workspace folder, "
    "using the same interpreter this app runs on (pandas, openpyxl, python-docx, "
    "and pdfplumber are available). Use this for financial analysis, generating "
    ".xlsx or .docx files, parsing .pdf/.csv inputs, or any other computational "
    "task. Have the script write output files directly to disk with ordinary "
    "Python file I/O - relative paths resolve inside the workspace's folder.\n"
    "  - control_serial_monitor: start/stop the background USB serial monitor or "
    "read its buffer. ESP32 hardware work only.\n\n"
    "Multiple named workspaces (folders) may be configured; pass the correct "
    "workspace alias whenever more than one exists, and ask the user to add a "
    "workspace if none exist or none fit the current task. When the user attaches "
    "a file through the UI, their message will contain one or more markers of the "
    "form '[Attached: filename.ext (workspace: alias)]' - the file has already "
    "been copied into that workspace's folder under exactly that filename; use "
    "manage_local_file (text) or run_python_code (binary, e.g. pdfplumber/pandas/"
    "python-docx) to read it directly rather than asking the user to paste its "
    "contents. Every write, delete, "
    "shell command, Python execution, or serial-control action requires explicit "
    "human approval before it runs - if a call is denied, adapt your plan instead "
    "of repeating the same call verbatim. Briefly explain what you are about to do "
    "before issuing a tool call, and summarize results in plain language "
    "afterwards. For ordinary conversation, analysis, or writing that does not "
    "need the filesystem or code execution, just respond directly without tools."
)

# =========================================================================
# Anthropic tool schema definitions (workspace enum is injected at request
# time by build_tools(), once the current list of configured workspaces is
# known - see build_tools() below).
# =========================================================================
BASE_TOOLS = [
    {
        "name": "manage_local_file",
        "description": (
            "Read, write, or delete a TEXT file (source code, markdown, CSV, "
            "config, etc.) inside a configured, sandboxed workspace folder. All "
            "filenames are resolved relative to that workspace's root; any path "
            "that would resolve outside of it (e.g. via '..' traversal or an "
            "absolute path elsewhere) is rejected. Do not use this for binary "
            "files such as .xlsx/.docx/.pdf - generate those with run_python_code "
            "instead, writing the file directly to disk from the script."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": (
                        "Alias of the configured workspace/folder to operate in. "
                        "If omitted, the first configured workspace is used."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "delete"],
                    "description": "The file operation to perform.",
                },
                "filename": {
                    "type": "string",
                    "description": "Relative filename/path within the workspace, e.g. 'src/main.py'.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write. Required when action is 'write'. Ignored otherwise.",
                },
            },
            "required": ["action", "filename"],
        },
    },
    {
        "name": "run_shell_command",
        "description": (
            "Runs a PowerShell command line, with the working directory set to a "
            "configured workspace folder. Useful for npm/node builds, pip/venv "
            "management, git, arduino-cli compile/upload, esptool.py flashing, or "
            "any other CLI tool already installed on the Windows PATH. "
            "stdout/stderr are streamed live into the tool log and the full "
            "combined output plus exit code is returned once the process finishes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": (
                        "Alias of the workspace whose folder is used as the working "
                        "directory. If omitted, the first configured workspace is used."
                    ),
                },
                "command": {
                    "type": "string",
                    "description": (
                        "The full PowerShell command line to execute, e.g. "
                        "\"npm install && npm run build\" or "
                        "\"arduino-cli upload -p COM3 --fqbn esp32:esp32:esp32s3 .\""
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to let the process run before it is killed. Default 300.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_python_code",
        "description": (
            "Executes a block of Python source code as a standalone script inside "
            "a configured workspace folder, using the same Python interpreter this "
            "app runs on (pandas, openpyxl, python-docx, and pdfplumber are "
            "available, alongside the standard library). Use this for financial "
            "analysis, generating .xlsx or .docx report files, parsing .pdf/.csv "
            "inputs, or any other computational task - have the script write "
            "output files directly to disk using ordinary Python file I/O; "
            "relative paths resolve inside the workspace folder. stdout/stderr "
            "are streamed live and the full output plus exit code is returned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": (
                        "Alias of the workspace to run the code in (used as the "
                        "working directory). If omitted, the first configured "
                        "workspace is used."
                    ),
                },
                "code": {
                    "type": "string",
                    "description": "The full Python source code to execute.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to let the script run before it is killed. Default 120.",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "control_serial_monitor",
        "description": (
            "Starts or stops the background serial monitor thread connected to an "
            "ESP32's USB serial port, or reads back the most recently buffered "
            "lines without stopping the monitor. Incoming data, while the monitor "
            "is running, is streamed live into the serial panel of the GUI "
            "(when it is visible). ESP32 hardware work only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "read_buffer"],
                },
                "port": {
                    "type": "string",
                    "description": "COM port to use, e.g. 'COM3'. If omitted, uses the port selected in the sidebar.",
                },
                "baud": {
                    "type": "integer",
                    "description": "Baud rate. Defaults to 115200 if omitted.",
                },
            },
            "required": ["action"],
        },
    },
]

# Tool actions that are inherently read-only / non-destructive and therefore
# do not need to interrupt the user with an approval dialog every time.
AUTO_APPROVE_SAFE_ACTIONS = {
    ("manage_local_file", "read"),
    ("control_serial_monitor", "read_buffer"),
}


# Required-field lookup per tool, used to detect a tool_use block whose input
# JSON was cut off mid-generation (stop_reason "max_tokens") before it ever
# became valid/complete - see _tool_input_is_incomplete() below.
TOOL_REQUIRED_FIELDS = {t["name"]: t["input_schema"].get("required", []) for t in BASE_TOOLS}


def _tool_input_is_incomplete(tool_name, tool_input):
    """True if a required field for this tool is missing or blank."""
    required = TOOL_REQUIRED_FIELDS.get(tool_name, [])
    for field in required:
        value = (tool_input or {}).get(field)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return True
    return False


def build_tools(workspace_aliases):
    """
    Returns a deep copy of BASE_TOOLS with the 'workspace' parameter's enum
    populated from the currently configured workspace aliases, so Claude is
    steered toward valid choices instead of guessing folder names.
    """
    tools = copy.deepcopy(BASE_TOOLS)
    for tool in tools:
        props = tool["input_schema"]["properties"]
        if "workspace" in props:
            if workspace_aliases:
                props["workspace"]["enum"] = list(workspace_aliases)
            else:
                props["workspace"].pop("enum", None)
    return tools


# =========================================================================
# Tool implementation: local sandboxed file management
# =========================================================================
def manage_local_file(action, filename, content, workspace_dir):
    """
    Read/write/delete a file strictly inside `workspace_dir`.

    Security: both `workspace_dir` and the resolved target path are
    normalized with os.path.abspath(). The target must be equal to, or a
    child of, the sandbox root, or the operation is refused. This blocks
    '../' traversal, absolute-path escapes, and symlink-style tricks that
    resolve elsewhere.
    """
    if not workspace_dir:
        return "Error: No workspace directory was resolved for this operation."

    base = os.path.abspath(workspace_dir)
    if not os.path.isdir(base):
        return f"Error: Workspace directory does not exist: {base}"

    candidate = os.path.abspath(os.path.join(base, filename or ""))

    # Sandbox enforcement: candidate must be base itself or live under base.
    if candidate != base and not candidate.startswith(base + os.sep):
        return (
            f"Error: Access denied. '{filename}' resolves to '{candidate}', "
            f"which is outside the sandboxed workspace directory '{base}'."
        )

    try:
        if action == "read":
            if not os.path.isfile(candidate):
                return f"Error: File not found: {filename}"
            with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            return data if data else "(file is empty)"

        elif action == "write":
            if content is None:
                return "Error: 'content' is required for a write action."
            parent = os.path.dirname(candidate)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(candidate, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            return f"Successfully wrote {len(content)} characters to '{filename}'."

        elif action == "delete":
            if not os.path.isfile(candidate):
                return f"Error: File not found: {filename}"
            os.remove(candidate)
            return f"Successfully deleted '{filename}'."

        else:
            return f"Error: Unknown action '{action}'. Must be read, write, or delete."

    except Exception as exc:
        return f"Error performing '{action}' on '{filename}': {exc}"


# =========================================================================
# Shared subprocess streaming helper (used by run_shell_command and
# run_python_code)
# =========================================================================
def _stream_subprocess(argv, cwd, on_output_line, timeout_seconds):
    working_dir = cwd if (cwd and os.path.isdir(cwd)) else None

    try:
        proc = subprocess.Popen(
            argv,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except FileNotFoundError as exc:
        return f"Error: executable not found: {exc}"
    except Exception as exc:
        return f"Error starting process: {exc}"

    output_lines = []
    start_time = time.time()
    timed_out = False

    try:
        for line in iter(proc.stdout.readline, ""):
            if line == "":
                break
            line = line.rstrip("\r\n")
            output_lines.append(line)
            on_output_line(line)
            if time.time() - start_time > timeout_seconds:
                timed_out = True
                proc.kill()
                msg = f"[TIMEOUT after {timeout_seconds}s - process killed]"
                output_lines.append(msg)
                on_output_line(msg)
                break
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass

    if not timed_out:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            output_lines.append("[Process did not exit cleanly and was killed]")

    exit_line = (
        f"[Process exited with code {proc.returncode}]"
        if not timed_out else "[Process was terminated due to timeout]"
    )
    output_lines.append(exit_line)
    on_output_line(exit_line)

    return "\n".join(output_lines)


# =========================================================================
# Tool implementation: PowerShell command runner
# =========================================================================
def run_shell_command(command, cwd, on_output_line, timeout_seconds=DEFAULT_SHELL_TIMEOUT):
    if not command or not command.strip():
        return "Error: No command was provided."

    on_output_line(f"$ {command}")

    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    return _stream_subprocess(argv, cwd, on_output_line, timeout_seconds)


# =========================================================================
# Tool implementation: ad-hoc Python script runner
# =========================================================================
def run_python_code(code, cwd, on_output_line, timeout_seconds=DEFAULT_PYTHON_TIMEOUT):
    if not code or not code.strip():
        return "Error: No code was provided."

    tmp_dir = os.path.join(cwd, ".agent_tmp") if (cwd and os.path.isdir(cwd)) else tempfile.gettempdir()
    try:
        os.makedirs(tmp_dir, exist_ok=True)
    except Exception:
        tmp_dir = tempfile.gettempdir()

    tmp_path = os.path.join(tmp_dir, f"snippet_{int(time.time() * 1000)}.py")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as exc:
        return f"Error writing temporary script: {exc}"

    on_output_line(f"$ python {os.path.basename(tmp_path)}")

    python_exe = sys.executable or "python"
    result = _stream_subprocess([python_exe, tmp_path], cwd, on_output_line, timeout_seconds)

    try:
        os.remove(tmp_path)
    except Exception:
        pass  # best-effort cleanup only

    return result


# =========================================================================
# Tool implementation: background serial monitor
# =========================================================================
class SerialManager:
    """
    Owns a single background thread that continuously reads from a pyserial
    connection and pushes each line both into a rolling buffer (for the
    read_buffer tool action) and into the GUI's serial panel via a
    thread-safe callback.
    """

    def __init__(self, on_line_callback, on_status_callback):
        self._on_line = on_line_callback        # (line: str) -> None, thread-safe
        self._on_status = on_status_callback     # (status: str) -> None, thread-safe
        self._ser = None
        self._thread = None
        self._stop_event = threading.Event()
        self._buffer = collections.deque(maxlen=SERIAL_BUFFER_LINES)
        self._buffer_lock = threading.Lock()

    @property
    def is_running(self):
        return self._ser is not None and self._ser.is_open and self._thread is not None and self._thread.is_alive()

    def start(self, port, baud=DEFAULT_BAUD):
        if serial is None:
            return "Error: pyserial is not installed. Run: pip install pyserial"
        if self.is_running:
            return f"Serial monitor is already running on {self._ser.port}."
        if not port:
            return "Error: No COM port specified or selected in the sidebar."

        try:
            self._ser = serial.Serial(port=port, baudrate=baud, timeout=1)
        except Exception as exc:
            self._ser = None
            return f"Error: Failed to open {port} @ {baud} baud: {exc}"

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self._on_status(f"Connected: {port} @ {baud} baud")
        return f"Serial monitor started on {port} @ {baud} baud."

    def stop(self):
        if not self.is_running and self._ser is None:
            return "Serial monitor was not running."
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            if self._ser is not None and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass
        self._ser = None
        self._thread = None
        self._on_status("Disconnected")
        return "Serial monitor stopped."

    def read_buffer(self):
        with self._buffer_lock:
            lines = list(self._buffer)
        return "\n".join(lines) if lines else "(serial buffer is empty)"

    def _read_loop(self):
        while not self._stop_event.is_set():
            try:
                if self._ser is None or not self._ser.is_open:
                    break
                raw = self._ser.readline()
                if raw:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        with self._buffer_lock:
                            self._buffer.append(line)
                        self._on_line(line)
                # if raw is empty, readline() simply timed out (timeout=1) - loop again
            except Exception as exc:
                self._on_line(f"[Serial error: {exc}]")
                self._on_status("Error - disconnected")
                break


# =========================================================================
# Approval dialog (modal) - the "User Approval Guard"
# =========================================================================
class ApprovalDialog(ctk.CTkToplevel):
    """
    Modal dialog that blocks (via a threading.Event owned by the caller)
    until the user clicks Approve or Deny. Must be created on the main/GUI
    thread - the calling worker thread schedules this via root.after(0, ...)
    and then blocks on the Event.
    """

    def __init__(self, master, tool_name, tool_input, on_result):
        super().__init__(master)
        self.title("Tool Approval Required")
        self.geometry("560x420")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()  # modal: block interaction with the main window
        self.protocol("WM_DELETE_WINDOW", self._deny)  # closing the window = deny

        self._on_result = on_result
        self._always_var = ctk.BooleanVar(value=False)

        header = ctk.CTkLabel(
            self,
            text=f"Claude wants to run: {tool_name}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#FFB300",
        )
        header.pack(padx=16, pady=(16, 4), anchor="w")

        subtitle = ctk.CTkLabel(
            self,
            text="Review the parameters below before allowing this action.",
            font=ctk.CTkFont(size=12),
        )
        subtitle.pack(padx=16, pady=(0, 8), anchor="w")

        box = ctk.CTkTextbox(self, width=520, height=220, font=("Consolas", 12))
        box.pack(padx=16, pady=4, fill="both", expand=True)
        try:
            pretty = json.dumps(tool_input, indent=2, ensure_ascii=False)
        except Exception:
            pretty = str(tool_input)
        box.insert("1.0", pretty)
        box.configure(state="disabled")

        chk = ctk.CTkCheckBox(
            self,
            text=f"Always allow '{tool_name}' for the rest of this session",
            variable=self._always_var,
        )
        chk.pack(padx=16, pady=(8, 8), anchor="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(padx=16, pady=(0, 16), fill="x")

        deny_btn = ctk.CTkButton(
            btn_frame, text="Deny", fg_color="#8B2E2E", hover_color="#6E2424", command=self._deny
        )
        deny_btn.pack(side="left", expand=True, fill="x", padx=(0, 8))

        approve_btn = ctk.CTkButton(
            btn_frame, text="Approve", fg_color="#2E8B57", hover_color="#256E46", command=self._approve
        )
        approve_btn.pack(side="left", expand=True, fill="x", padx=(8, 0))

        self.after(50, self.lift)
        self.after(50, self.focus_force)

    def _approve(self):
        self._on_result(True, self._always_var.get())
        self.grab_release()
        self.destroy()

    def _deny(self):
        self._on_result(False, False)
        self.grab_release()
        self.destroy()


# =========================================================================
# Add-workspace dialog (modal)
# =========================================================================
class AddWorkspaceDialog(ctk.CTkToplevel):
    """Small modal dialog to add a new (alias -> folder) workspace entry."""

    def __init__(self, master, on_result):
        super().__init__(master)
        self.title("Add Workspace")
        self.geometry("440x220")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._on_result = on_result

        ctk.CTkLabel(self, text="Alias (short name Claude will use):").pack(padx=16, pady=(16, 4), anchor="w")
        self.alias_entry = ctk.CTkEntry(self, placeholder_text="e.g. esp32-firmware, financials")
        self.alias_entry.pack(padx=16, fill="x")

        ctk.CTkLabel(self, text="Folder path:").pack(padx=16, pady=(12, 4), anchor="w")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=16, fill="x")
        self.path_var = ctk.StringVar()
        ctk.CTkEntry(row, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="...", width=32, command=self._browse).pack(side="left", padx=(6, 0))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(padx=16, pady=20, fill="x")
        ctk.CTkButton(
            btn_row, text="Cancel", fg_color="#555555", hover_color="#444444", command=self._cancel
        ).pack(side="left", expand=True, fill="x", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Add", command=self._add).pack(side="left", expand=True, fill="x", padx=(8, 0))

        self.after(50, self.lift)
        self.after(50, self.focus_force)

    def _browse(self):
        chosen = filedialog.askdirectory(parent=self)
        if chosen:
            self.path_var.set(chosen)

    def _add(self):
        alias = self.alias_entry.get().strip()
        path = self.path_var.get().strip()
        if not alias or not path:
            messagebox.showerror("Add Workspace", "Both an alias and a folder path are required.", parent=self)
            return
        if not os.path.isdir(path):
            messagebox.showerror("Add Workspace", f"Folder does not exist:\n{path}", parent=self)
            return
        self._on_result(alias, os.path.abspath(path))
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.grab_release()
        self.destroy()


# =========================================================================
# Choose-workspace dialog (modal) - used when attaching a file and more than
# one workspace is configured
# =========================================================================
class ChooseWorkspaceDialog(ctk.CTkToplevel):
    def __init__(self, master, aliases, on_choice):
        super().__init__(master)
        self.title("Choose Workspace")
        self.geometry("360x180")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._on_choice = on_choice

        ctk.CTkLabel(
            self, text="Attach the file(s) to which workspace?", wraplength=320
        ).pack(padx=16, pady=(20, 10), anchor="w")

        self.choice_var = ctk.StringVar(value=aliases[0])
        ctk.CTkOptionMenu(self, values=aliases, variable=self.choice_var).pack(padx=16, fill="x")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(padx=16, pady=20, fill="x")
        ctk.CTkButton(
            btn_row, text="Cancel", fg_color="#555555", hover_color="#444444", command=self._cancel
        ).pack(side="left", expand=True, fill="x", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Attach", command=self._confirm).pack(side="left", expand=True, fill="x", padx=(8, 0))

        self.after(50, self.lift)
        self.after(50, self.focus_force)

    def _confirm(self):
        alias = self.choice_var.get()
        self.grab_release()
        self.destroy()
        self._on_choice(alias)

    def _cancel(self):
        self.grab_release()
        self.destroy()


# =========================================================================
# Rename-conversation dialog (modal)
# =========================================================================
class RenameDialog(ctk.CTkToplevel):
    def __init__(self, master, current_title, on_result):
        super().__init__(master)
        self.title("Rename Conversation")
        self.geometry("400x160")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._on_result = on_result

        ctk.CTkLabel(self, text="New title:").pack(padx=16, pady=(16, 4), anchor="w")
        self.title_entry = ctk.CTkEntry(self, placeholder_text="Enter new title")
        self.title_entry.pack(padx=16, fill="x", pady=(0, 12))
        self.title_entry.insert(0, current_title)
        self.title_entry.select_range(0, "end")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(padx=16, pady=(0, 16), fill="x")
        ctk.CTkButton(
            btn_row, text="Cancel", fg_color="#555555", hover_color="#444444", command=self._cancel
        ).pack(side="left", expand=True, fill="x", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Rename", command=self._confirm).pack(side="left", expand=True, fill="x", padx=(8, 0))

        self.after(50, self.lift)
        self.after(50, self.focus_force)
        self.after(50, self.title_entry.focus_set)

    def _confirm(self):
        new_title = self.title_entry.get().strip()
        if new_title:
            self._on_result(new_title)
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self._on_result(None)
        self.grab_release()
        self.destroy()


# =========================================================================
# Trash dialog (modal) - view/restore/permanently-delete trashed conversations
# =========================================================================
class TrashDialog(ctk.CTkToplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.title("Trash")
        self.geometry("420x480")
        self.attributes("-topmost", True)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.app = app

        ctk.CTkLabel(self, text="Deleted Conversations", font=ctk.CTkFont(size=15, weight="bold")).pack(
            padx=16, pady=(16, 8), anchor="w"
        )

        self.list_frame = ctk.CTkScrollableFrame(self, label_text="")
        self.list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        ctk.CTkButton(self, text="Close", command=self._close).pack(padx=16, pady=(0, 16), fill="x")

        self.after(50, self.lift)
        self.after(50, self.focus_force)
        self._refresh()

    def _refresh(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()

        items = self.app._list_trash()
        if not items:
            ctk.CTkLabel(self.list_frame, text="Trash is empty.", text_color="#AAAAAA").pack(anchor="w", pady=4)
            return

        for item in items:
            row = ctk.CTkFrame(self.list_frame, fg_color="#2b2b2b")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                row, text=item["title"], anchor="w", justify="left", wraplength=170, font=("Consolas", 11)
            ).pack(side="left", fill="x", expand=True, padx=6, pady=6)
            ctk.CTkButton(
                row, text="Restore", width=64, command=lambda cid=item["id"]: self._restore(cid)
            ).pack(side="right", padx=2)
            ctk.CTkButton(
                row, text="Delete Forever", width=104, fg_color="#8B2E2E", hover_color="#6E2424",
                command=lambda cid=item["id"]: self._delete_forever(cid),
            ).pack(side="right", padx=2)

    def _restore(self, conv_id):
        self.app._restore_conversation(conv_id)
        self._refresh()

    def _delete_forever(self, conv_id):
        self.app._permanently_delete_trash_item(conv_id)
        self._refresh()

    def _close(self):
        self.grab_release()
        self.destroy()


# =========================================================================
# Main application
# =========================================================================
class ClaudeAgentApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(APP_TITLE)
        self.geometry("1400x860")
        self.minsize(1100, 650)

        # ---- runtime state ----------------------------------------------------
        self.client = None
        self.workspaces = {}                    # alias -> absolute folder path
        self.conversation = []                  # list of {"role": ..., "content": ...}
        self.current_conversation_id = self._make_conversation_id()
        self.current_conversation_title = None
        self.auto_approved_tools = set()        # tool names always allowed this session
        self.busy = False
        # Server-reported input token count from the most recent response for
        # THIS conversation (already reflects any compaction that occurred).
        # None until we've made at least one live call in this session -
        # see _context_budget_check() for why this takes priority over the
        # rough local estimate once we have it.
        self.last_known_input_tokens = None

        self.serial_manager = SerialManager(
            on_line_callback=self.safe_append_serial_line,
            on_status_callback=self.safe_set_serial_status,
        )

        # ---- build UI -----------------------------------------------------
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main_panel()

        self._load_settings()
        self._refresh_workspace_list()
        self._refresh_conversation_list()
        self._apply_serial_visibility()
        self.refresh_com_ports()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Defer maximizing until after the window has actually been realized/drawn -
        # calling self.state("zoomed") synchronously inside __init__ (before the
        # window manager has mapped the window) is unreliable on Windows and can
        # silently be ignored, leaving the window at its initial geometry.
        self.after(10, self._maximize_on_start)
        # Auto-connect with previously saved API key if available
        self.after(100, self._auto_connect_if_key_available)
        # Switch to Chats tab on startup (deferred to avoid blocking GUI)
        self.after(50, self._switch_to_chats_tab)

    def _maximize_on_start(self):
        """Maximize the window on launch. 'zoomed' is the normal Windows/some-Linux-WM
        approach; if that's not supported by the current window manager, fall back to
        the X11 '-zoomed' attribute, and otherwise just leave the initial geometry."""
        try:
            self.update_idletasks()
            self.state("zoomed")
        except Exception:
            try:
                self.attributes("-zoomed", True)
            except Exception:
                pass

    @staticmethod
    def _make_conversation_id():
        return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    # ------------------------------------------------------------------
    # Sidebar: settings panel (tabbed)
    # ------------------------------------------------------------------
    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=320, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)

        title = ctk.CTkLabel(sidebar, text=APP_TITLE, font=ctk.CTkFont(size=20, weight="bold"))
        title.pack(padx=16, pady=(20, 10), anchor="w")

        self.sidebar_tabs = ctk.CTkTabview(sidebar, width=290)
        self.sidebar_tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.sidebar_tabs.add("Setup")
        self.sidebar_tabs.add("Workspaces")
        self.sidebar_tabs.add("ESP32")
        self.sidebar_tabs.add("Chats")

        self._build_setup_tab(self.sidebar_tabs.tab("Setup"))
        self._build_workspaces_tab(self.sidebar_tabs.tab("Workspaces"))
        self._build_esp32_tab(self.sidebar_tabs.tab("ESP32"))
        self._build_chats_tab(self.sidebar_tabs.tab("Chats"))

    def _build_setup_tab(self, tab):
        # Create a scrollable frame to hold all setup content
        scroll_frame = ctk.CTkScrollableFrame(tab, label_text="")
        scroll_frame.pack(fill="both", expand=True, padx=4, pady=4)
        scroll_frame.grid_columnconfigure(0, weight=1)
        
        pad = {"padx": 4, "pady": (10, 2)}

        ctk.CTkLabel(scroll_frame, text="Anthropic API Key", anchor="w").pack(fill="x", **pad)
        self.api_key_entry = ctk.CTkEntry(scroll_frame, show="*", placeholder_text="sk-ant-...")
        self.api_key_entry.pack(fill="x", padx=4, pady=(0, 4))
        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if env_key:
            self.api_key_entry.insert(0, env_key)

        self.remember_key_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            scroll_frame, text="Remember key on disk (plaintext)", variable=self.remember_key_var
        ).pack(fill="x", padx=4, pady=(0, 4))

        self.connect_btn = ctk.CTkButton(scroll_frame, text="Connect / Test Key", command=self.init_client)
        self.connect_btn.pack(fill="x", padx=4, pady=(2, 4))
        self.connection_status = ctk.CTkLabel(scroll_frame, text="Not connected", text_color="#AAAAAA")
        self.connection_status.pack(fill="x", padx=4, pady=(0, 8))

        ctk.CTkLabel(scroll_frame, text="Model", anchor="w").pack(fill="x", **pad)
        self.model_var = ctk.StringVar(value=DEFAULT_MODEL)
        self.model_combo = ctk.CTkComboBox(scroll_frame, values=AVAILABLE_MODELS, variable=self.model_var)
        self.model_combo.pack(fill="x", padx=4, pady=(0, 10))

        ctk.CTkLabel(scroll_frame, text="Max Output Tokens", anchor="w").pack(fill="x", **pad)
        self.max_tokens_var = ctk.StringVar(value=str(DEFAULT_MAX_TOKENS))
        ctk.CTkEntry(scroll_frame, textvariable=self.max_tokens_var).pack(fill="x", padx=4, pady=(0, 2))
        ctk.CTkLabel(
            scroll_frame, text="Raise this if long code/documents get cut off mid-generation.",
            anchor="w", justify="left", wraplength=250, text_color="#AAAAAA", font=("", 10),
        ).pack(fill="x", padx=4, pady=(0, 10))

        self.enable_compaction_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            scroll_frame, text="Auto-compact long conversations (beta)",
            variable=self.enable_compaction_var,
        ).pack(fill="x", padx=4, pady=(0, 2))
        ctk.CTkLabel(
            scroll_frame,
            text=(
                "Lets Claude summarize older context on Anthropic's servers "
                "instead of hitting the context-window limit. Only supported "
                "on Sonnet 5, Opus 5, and Fable 5.1 - ignored for other models. "
                "Adds a small extra billed step when it triggers."
            ),
            anchor="w", justify="left", wraplength=250, text_color="#AAAAAA", font=("", 10),
        ).pack(fill="x", padx=4, pady=(0, 10))

        ctk.CTkLabel(scroll_frame, text="Billing Method", anchor="w").pack(fill="x", **pad)
        self.billing_var = ctk.StringVar(value="automatic")
        billing_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        billing_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        ctk.CTkLabel(billing_frame, text="Automatic: Use subscription if active, else pay-as-you-go", 
                     font=("", 9), text_color="#AAAAAA").pack(anchor="w", padx=0, pady=1)
        ctk.CTkRadioButton(billing_frame, text="Automatic (Recommended)", variable=self.billing_var, 
                          value="automatic", command=self._update_billing_display).pack(anchor="w", padx=0, pady=2)
        
        ctk.CTkLabel(billing_frame, text="Note: Subscription is used if active on your Anthropic account", 
                     font=("", 9), text_color="#AAAAAA").pack(anchor="w", padx=20, pady=(0, 4))
        
        ctk.CTkRadioButton(billing_frame, text="Pay-as-you-go (API only)", variable=self.billing_var, 
                          value="api_only", command=self._update_billing_display).pack(anchor="w", padx=0, pady=2)
        
        self.billing_info_label = ctk.CTkLabel(scroll_frame, 
                                              text="Current billing: Using subscription if active",
                                              font=("", 9), text_color="#7CFC00")
        self.billing_info_label.pack(fill="x", padx=4, pady=(0, 10))

        ctk.CTkLabel(scroll_frame, text="Pre-approve actions (set & forget)", anchor="w").pack(fill="x", **pad)
        self.preapprove_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self.preapprove_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        # Pre-approval checkboxes
        self.preapprove_read_var = ctk.BooleanVar(value=True)
        self.preapprove_write_var = ctk.BooleanVar(value=False)
        self.preapprove_shell_var = ctk.BooleanVar(value=False)
        self.preapprove_python_var = ctk.BooleanVar(value=False)
        self.preapprove_serial_var = ctk.BooleanVar(value=False)
        
        ctk.CTkCheckBox(self.preapprove_frame, text="manage_local_file (read)", variable=self.preapprove_read_var, state="disabled").pack(anchor="w", padx=0, pady=2)
        ctk.CTkCheckBox(self.preapprove_frame, text="manage_local_file (write)", variable=self.preapprove_write_var, command=self._update_preapprovals).pack(anchor="w", padx=0, pady=2)
        ctk.CTkCheckBox(self.preapprove_frame, text="run_shell_command", variable=self.preapprove_shell_var, command=self._update_preapprovals).pack(anchor="w", padx=0, pady=2)
        ctk.CTkCheckBox(self.preapprove_frame, text="run_python_code", variable=self.preapprove_python_var, command=self._update_preapprovals).pack(anchor="w", padx=0, pady=2)
        ctk.CTkCheckBox(self.preapprove_frame, text="control_serial_monitor", variable=self.preapprove_serial_var, command=self._update_preapprovals).pack(anchor="w", padx=0, pady=2)

        ctk.CTkLabel(scroll_frame, text="Active auto-approvals (session)", anchor="w").pack(fill="x", **pad)
        self.auto_approved_label = ctk.CTkLabel(
            scroll_frame, text="(read-only actions always allowed)",
            anchor="w", justify="left", wraplength=250, text_color="#AAAAAA", font=("", 9),
        )
        self.auto_approved_label.pack(fill="x", padx=4, pady=(0, 6))
        ctk.CTkButton(scroll_frame, text="Reset auto-approvals", command=self.reset_auto_approvals).pack(fill="x", padx=4, pady=(0, 8))
        
        # Create a footer frame for the Save Settings button (sticky to bottom)
        footer_frame = ctk.CTkFrame(tab, fg_color="transparent")
        footer_frame.pack(fill="x", padx=4, pady=(4, 8), side="bottom")
        ctk.CTkButton(footer_frame, text="Save Settings", command=self._save_settings).pack(fill="x", padx=0)

    def _build_workspaces_tab(self, tab):
        ctk.CTkLabel(
            tab, text="Folders Claude can read/write/execute in.",
            anchor="w", justify="left", wraplength=250, text_color="#AAAAAA",
        ).pack(fill="x", padx=4, pady=(10, 6))

        self.workspace_list_frame = ctk.CTkScrollableFrame(tab, label_text="")
        self.workspace_list_frame.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        ctk.CTkButton(tab, text="+ Add Workspace", command=self._open_add_workspace_dialog).pack(fill="x", padx=4, pady=(0, 8))

    def _build_esp32_tab(self, tab):
        pad = {"padx": 4, "pady": (10, 2)}

        self.show_serial_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab, text="Show Serial Monitor panel", variable=self.show_serial_var,
            command=self._apply_serial_visibility,
        ).pack(fill="x", padx=4, pady=(10, 10))

        ctk.CTkLabel(tab, text="ESP32 COM Port", anchor="w").pack(fill="x", **pad)
        com_row = ctk.CTkFrame(tab, fg_color="transparent")
        com_row.pack(fill="x", padx=4, pady=(0, 4))
        self.com_port_var = ctk.StringVar(value="COM3")
        self.com_port_combo = ctk.CTkComboBox(com_row, values=["COM3"], variable=self.com_port_var)
        self.com_port_combo.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(com_row, text="Refresh", width=70, command=self.refresh_com_ports).pack(side="left", padx=(6, 0))

        baud_row = ctk.CTkFrame(tab, fg_color="transparent")
        baud_row.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(baud_row, text="Baud:").pack(side="left")
        self.baud_var = ctk.StringVar(value=str(DEFAULT_BAUD))
        ctk.CTkEntry(baud_row, textvariable=self.baud_var, width=90).pack(side="left", padx=(6, 0))

        serial_btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        serial_btn_row.pack(fill="x", padx=4, pady=(0, 8))
        self.serial_start_btn = ctk.CTkButton(serial_btn_row, text="Start Monitor", command=self.manual_start_serial)
        self.serial_start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.serial_stop_btn = ctk.CTkButton(
            serial_btn_row, text="Stop", fg_color="#8B2E2E", hover_color="#6E2424", command=self.manual_stop_serial
        )
        self.serial_stop_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

    def _build_chats_tab(self, tab):
        ctk.CTkButton(tab, text="+ New Chat", command=self._start_new_chat).pack(fill="x", padx=4, pady=(10, 8))

        self.conversation_list_frame = ctk.CTkScrollableFrame(tab, label_text="Saved Conversations")
        self.conversation_list_frame.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        ctk.CTkButton(
            tab, text="Trash", fg_color="#555555", hover_color="#444444", command=self._open_trash_dialog
        ).pack(fill="x", padx=4, pady=(0, 8))

    # ------------------------------------------------------------------
    # Main panel: chat (left) + serial monitor (right, toggleable)
    # ------------------------------------------------------------------
    def _build_main_panel(self):
        self.main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=2)
        self.main.grid_columnconfigure(1, weight=1)
        self.main.grid_rowconfigure(0, weight=1)

        # ---------------- Chat (left) ----------------
        self.chat_frame = ctk.CTkFrame(self.main)
        self.chat_frame.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=10)
        self.chat_frame.grid_rowconfigure(1, weight=1)
        self.chat_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.chat_frame, text="Chat", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )

        self.chat_box = ctk.CTkTextbox(self.chat_frame, wrap="word", state="disabled", font=("Consolas", 13))
        self.chat_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        # Configure color tags on the underlying tkinter Text widget.
        self.chat_box._textbox.tag_config("user", foreground="#4FA8FF")
        self.chat_box._textbox.tag_config("assistant", foreground="#B5E61D")
        self.chat_box._textbox.tag_config("tool", foreground="#9A9A9A")
        self.chat_box._textbox.tag_config("error", foreground="#FF6B6B")
        self.chat_box._textbox.tag_config("system", foreground="#FFB300")

        input_row = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        input_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 10))
        input_row.grid_columnconfigure(0, weight=1)

        # Multi-line text input with automatic wrapping and height that grows with content
        self.input_text = ctk.CTkTextbox(input_row, wrap="word", height=60, font=("Consolas", 13))
        self.input_text.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.input_text.bind("<Control-Return>", self.on_send)  # Ctrl+Enter to send
        self.input_text.bind("<Shift-Return>", lambda e: None)  # Shift+Enter for new line

        button_frame = ctk.CTkFrame(input_row, fg_color="transparent")
        button_frame.grid(row=0, column=1, columnspan=2, sticky="ew", padx=0)
        button_frame.grid_columnconfigure(0, weight=1)

        self.attach_button = ctk.CTkButton(
            button_frame, text="+", width=40, fg_color="#555555", hover_color="#444444", command=self._on_attach_file
        )
        self.attach_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.send_button = ctk.CTkButton(button_frame, text="Send", width=90, command=self.on_send)
        self.send_button.grid(row=0, column=1, sticky="ew", padx=0)

        self.status_label = ctk.CTkLabel(self.chat_frame, text="Ready", text_color="#AAAAAA", anchor="w")
        self.status_label.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 6))

        # ---------------- Serial monitor (right, toggleable) ----------------
        self.serial_frame = ctk.CTkFrame(self.main)
        self.serial_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        self.serial_frame.grid_rowconfigure(1, weight=1)
        self.serial_frame.grid_columnconfigure(0, weight=1)

        header_row = ctk.CTkFrame(self.serial_frame, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        ctk.CTkLabel(header_row, text="Serial Monitor", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        self.serial_status_label = ctk.CTkLabel(header_row, text="Disconnected", text_color="#AAAAAA")
        self.serial_status_label.pack(side="right")

        self.serial_box = ctk.CTkTextbox(self.serial_frame, wrap="none", state="disabled", font=("Consolas", 12))
        self.serial_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)

        clear_row = ctk.CTkFrame(self.serial_frame, fg_color="transparent")
        clear_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        ctk.CTkButton(clear_row, text="Clear Log", fg_color="#555555", hover_color="#444444", command=self.clear_serial_log).pack(fill="x")

    def _apply_serial_visibility(self):
        show = self.show_serial_var.get()
        if show:
            self.serial_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
            self.chat_frame.grid_configure(column=0, columnspan=1, padx=(10, 5))
            self.main.grid_columnconfigure(0, weight=2)
            self.main.grid_columnconfigure(1, weight=1)
        else:
            self.serial_frame.grid_remove()
            self.chat_frame.grid_configure(column=0, columnspan=2, padx=10)
            self.main.grid_columnconfigure(0, weight=1)
            self.main.grid_columnconfigure(1, weight=0)

    # ==================================================================
    # Settings persistence (non-sensitive by default)
    # ==================================================================
    def _load_settings(self):
        cfg = {}
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}

        self.model_var.set(cfg.get("model", DEFAULT_MODEL))
        self.max_tokens_var.set(str(cfg.get("max_tokens", DEFAULT_MAX_TOKENS)))
        self.enable_compaction_var.set(bool(cfg.get("enable_compaction", True)))
        self.com_port_var.set(cfg.get("com_port", "COM3"))
        self.baud_var.set(str(cfg.get("baud", DEFAULT_BAUD)))
        self.show_serial_var.set(bool(cfg.get("show_serial", False)))
        
        # Load billing method preference
        self.billing_var.set(cfg.get("billing_method", "automatic"))
        
        # Load pre-approval settings (default to True for convenience)
        self.preapprove_write_var.set(bool(cfg.get("preapprove_write", True)))
        self.preapprove_shell_var.set(bool(cfg.get("preapprove_shell", True)))
        self.preapprove_python_var.set(bool(cfg.get("preapprove_python", True)))
        self.preapprove_serial_var.set(bool(cfg.get("preapprove_serial", True)))
        self._update_preapprovals()

        workspaces = cfg.get("workspaces") or {}
        if not workspaces:
            # Backward compatibility with the earlier single-folder version.
            old_dir = cfg.get("project_dir")
            if old_dir and os.path.isdir(old_dir):
                workspaces = {"default": old_dir}
        if not workspaces:
            # First run / nothing configured: give a sensible starting point.
            workspaces = {"default": os.getcwd()}
        self.workspaces = {alias: path for alias, path in workspaces.items() if os.path.isdir(path)}

        if cfg.get("remember_key") and cfg.get("api_key"):
            self.remember_key_var.set(True)
            self.api_key_entry.delete(0, "end")
            self.api_key_entry.insert(0, cfg["api_key"])

    def _save_settings(self):
        cfg = {
            "model": self.model_var.get(),
            "max_tokens": self.max_tokens_var.get(),
            "enable_compaction": self.enable_compaction_var.get(),
            "com_port": self.com_port_var.get(),
            "baud": self.baud_var.get(),
            "show_serial": self.show_serial_var.get(),
            "workspaces": self.workspaces,
            "remember_key": self.remember_key_var.get(),
            "preapprove_write": self.preapprove_write_var.get(),
            "preapprove_shell": self.preapprove_shell_var.get(),
            "preapprove_python": self.preapprove_python_var.get(),
            "preapprove_serial": self.preapprove_serial_var.get(),
            "billing_method": self.billing_var.get(),
        }
        if self.remember_key_var.get():
            cfg["api_key"] = self.api_key_entry.get().strip()
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            self.append_tool_log(f"Settings saved to {CONFIG_PATH}")
        except Exception as exc:
            messagebox.showerror("Save Settings", f"Could not save settings: {exc}")

    def _on_close(self):
        try:
            if self.serial_manager.is_running:
                self.serial_manager.stop()
        except Exception:
            pass
        self.destroy()

    # ==================================================================
    # Sidebar callbacks - general
    # ==================================================================
    def refresh_com_ports(self):
        ports = []
        if list_ports is not None:
            try:
                ports = [p.device for p in list_ports.comports()]
            except Exception:
                ports = []
        if not ports:
            ports = ["COM3"]
        self.com_port_combo.configure(values=ports)
        if self.com_port_var.get() not in ports:
            self.com_port_var.set(ports[0])

    def reset_auto_approvals(self):
        self.auto_approved_tools.clear()
        self.auto_approved_label.configure(text="(none - read-only actions are always pre-approved)")

    def _update_billing_display(self):
        """Update the billing display based on selection."""
        if self.billing_var.get() == "automatic":
            self.billing_info_label.configure(
                text="Current billing: Using subscription if active (recommended)",
                text_color="#7CFC00"
            )
        else:
            self.billing_info_label.configure(
                text="Current billing: Pay-as-you-go (API only - charges per request)",
                text_color="#FFD700"
            )

    def _update_preapprovals(self):
        """Update the set of pre-approved tools based on checkboxes."""
        self.auto_approved_tools.clear()
        # manage_local_file read is always pre-approved
        if self.preapprove_write_var.get():
            self.auto_approved_tools.add("manage_local_file (write)")
        if self.preapprove_shell_var.get():
            self.auto_approved_tools.add("run_shell_command")
        if self.preapprove_python_var.get():
            self.auto_approved_tools.add("run_python_code")
        if self.preapprove_serial_var.get():
            self.auto_approved_tools.add("control_serial_monitor")
        self.safe_update_auto_approved_label()


    def manual_start_serial(self):
        port = self.com_port_var.get()
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = DEFAULT_BAUD
        result = self.serial_manager.start(port, baud)
        self.append_tool_log(result)

    def manual_stop_serial(self):
        result = self.serial_manager.stop()
        self.append_tool_log(result)

    def clear_serial_log(self):
        self.serial_box.configure(state="normal")
        self.serial_box.delete("1.0", "end")
        self.serial_box.configure(state="disabled")

    # ==================================================================
    # Workspaces
    # ==================================================================
    def _open_add_workspace_dialog(self):
        def on_result(alias, path):
            self.workspaces[alias] = path
            self._refresh_workspace_list()
        AddWorkspaceDialog(self, on_result)

    def _remove_workspace(self, alias):
        if alias in self.workspaces:
            del self.workspaces[alias]
            self._refresh_workspace_list()

    def _refresh_workspace_list(self):
        for widget in self.workspace_list_frame.winfo_children():
            widget.destroy()

        if not self.workspaces:
            ctk.CTkLabel(
                self.workspace_list_frame, text="No workspaces yet. Add one below.", text_color="#AAAAAA"
            ).pack(anchor="w", pady=4)
            return

        for alias, path in self.workspaces.items():
            row = ctk.CTkFrame(self.workspace_list_frame, fg_color="#2b2b2b")
            row.pack(fill="x", pady=3)
            text = f"{alias}\n{path}"
            ctk.CTkLabel(
                row, text=text, justify="left", anchor="w", wraplength=190, font=("Consolas", 10)
            ).pack(side="left", fill="x", expand=True, padx=6, pady=6)
            ctk.CTkButton(
                row, text="\u2715", width=26, fg_color="#8B2E2E", hover_color="#6E2424",
                command=lambda a=alias: self._remove_workspace(a),
            ).pack(side="right", padx=4)

    def _resolve_workspace(self, alias):
        """Returns (abs_path, None) on success or (None, error_string) on failure."""
        if not self.workspaces:
            return None, "Error: No workspaces are configured. Add one in the Workspaces tab first."
        if not alias:
            alias = next(iter(self.workspaces))
        if alias not in self.workspaces:
            return None, f"Error: Unknown workspace '{alias}'. Configured workspaces: {', '.join(self.workspaces.keys())}"
        path = self.workspaces[alias]
        if not os.path.isdir(path):
            return None, f"Error: Workspace '{alias}' folder no longer exists: {path}"
        return path, None

    def _build_system_prompt(self):
        if self.workspaces:
            ws_lines = "\n".join(f"  - {alias}: {path}" for alias, path in self.workspaces.items())
            ws_text = f"\n\nConfigured workspaces (folders you may read/write/execute within):\n{ws_lines}"
        else:
            ws_text = "\n\nNo workspaces are currently configured; ask the user to add one before attempting file, shell, or Python operations."
        return SYSTEM_PROMPT + ws_text

    # ==================================================================
    # File attachments (copies files into a workspace, references them in chat)
    # ==================================================================
    def _on_attach_file(self):
        if not self.workspaces:
            messagebox.showerror(
                "Attach File", "No workspaces are configured. Add one in the Workspaces tab first."
            )
            return
        paths = filedialog.askopenfilenames(title="Select file(s) to attach")
        if not paths:
            return
        if len(self.workspaces) == 1:
            alias = next(iter(self.workspaces))
            self._copy_attachments_to_workspace(paths, alias)
        else:
            ChooseWorkspaceDialog(
                self, list(self.workspaces.keys()),
                on_choice=lambda alias: self._copy_attachments_to_workspace(paths, alias),
            )

    def _copy_attachments_to_workspace(self, paths, alias):
        workspace_dir = self.workspaces.get(alias)
        if not workspace_dir or not os.path.isdir(workspace_dir):
            messagebox.showerror("Attach File", f"Workspace '{alias}' is not valid.")
            return

        copied = []
        for src in paths:
            try:
                dest_name = os.path.basename(src)
                dest_path = os.path.join(workspace_dir, dest_name)
                # Don't silently overwrite an existing file with different content.
                if os.path.exists(dest_path):
                    base, ext = os.path.splitext(dest_name)
                    i = 1
                    while os.path.exists(dest_path):
                        dest_name = f"{base}_{i}{ext}"
                        dest_path = os.path.join(workspace_dir, dest_name)
                        i += 1
                shutil.copy2(src, dest_path)
                copied.append(dest_name)
            except Exception as exc:
                messagebox.showerror("Attach File", f"Failed to copy '{src}':\n{exc}")

        if not copied:
            return

        refs = " ".join(f"[Attached: {name} (workspace: {alias})]" for name in copied)
        current = self.input_text.get("1.0", "end-1c")
        new_text = (current + " " + refs).strip() if current else refs
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", new_text)
        self.input_text.focus_set()

        self.append_tool_log(f"Attached {len(copied)} file(s) to workspace '{alias}': {', '.join(copied)}")

    # ==================================================================
    # Conversations (persistence + Chats tab)
    # ==================================================================
    def _ensure_conversations_dir(self):
        os.makedirs(CONVERSATIONS_DIR, exist_ok=True)

    def _start_new_chat(self):
        self.current_conversation_id = self._make_conversation_id()
        self.current_conversation_title = None
        self.conversation = []
        self.last_known_input_tokens = None
        self._clear_chat_box()
        self._refresh_conversation_list()

    def _clear_chat_box(self):
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", "end")
        self.chat_box.configure(state="disabled")

    def _save_current_conversation(self):
        if not self.conversation:
            return
        self._ensure_conversations_dir()
        if not self.current_conversation_title:
            first_user = next(
                (m["content"] for m in self.conversation if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "New conversation",
            )
            self.current_conversation_title = (first_user[:60] + "...") if len(first_user) > 60 else first_user
        data = {
            "id": self.current_conversation_id,
            "title": self.current_conversation_title,
            "model": self.model_var.get(),
            "workspaces": dict(self.workspaces),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "messages": self.conversation,
        }
        path = os.path.join(CONVERSATIONS_DIR, f"{self.current_conversation_id}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            self.safe_append_system_message(f"Warning: failed to save conversation: {exc}")

    def _list_conversations(self):
        self._ensure_conversations_dir()
        items = []
        try:
            filenames = os.listdir(CONVERSATIONS_DIR)
        except Exception:
            filenames = []
        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(CONVERSATIONS_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items.append({
                    "id": data.get("id", fname[:-5]),
                    "title": data.get("title", "(untitled)"),
                    "updated_at": data.get("updated_at", ""),
                })
            except Exception:
                continue
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return items

    def _open_conversation(self, conv_id):
        """Load conversation in background thread to keep GUI responsive."""
        # Show loading indicator immediately
        self.status_label.configure(text="Loading conversation...")
        # Load on background thread to prevent GUI freezing
        thread = threading.Thread(target=self._load_conversation_background, args=(conv_id,), daemon=True)
        thread.start()

    def _load_conversation_background(self, conv_id):
        """Load conversation data from disk on background thread."""
        path = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            self.safe_set_status("Failed to load conversation")
            self.after(0, lambda: messagebox.showerror("Open Conversation", f"Failed to load conversation: {exc}"))
            return
        
        # Update state on main thread via safe callback
        self.current_conversation_id = data.get("id", conv_id)
        self.current_conversation_title = data.get("title")
        self.conversation = data.get("messages", [])
        # This is a different conversation's history; any real token count we
        # had was for the PREVIOUS one and does not apply here. Reset to None
        # so the next send falls back to local estimation / trusts compaction
        # (see _context_budget_check()) until we've made a live call on this
        # conversation in this session.
        self.last_known_input_tokens = None

        # Restore workspaces
        saved_workspaces = data.get("workspaces") or {}
        missing_paths = []
        for alias, ws_path in saved_workspaces.items():
            self.workspaces[alias] = ws_path
            if not os.path.isdir(ws_path):
                missing_paths.append(f"{alias}: {ws_path}")

        # Schedule all UI updates to run on main thread
        self.after(0, self._finish_opening_conversation, missing_paths, bool(saved_workspaces))

    def _finish_opening_conversation(self, missing_paths, has_workspaces):
        """Finish opening conversation on main thread after background load."""
        if has_workspaces:
            self._refresh_workspace_list()
        
        self._replay_conversation_to_chatbox()
        self._refresh_conversation_list()
        self.safe_set_status("Ready")
        
        if missing_paths:
            self.append_tool_log(
                "Warning: this conversation's workspace folder(s) no longer exist on disk - "
                + "; ".join(missing_paths)
            )

    def _ensure_trash_dir(self):
        os.makedirs(TRASH_DIR, exist_ok=True)

    def _rename_conversation(self, conv_id, current_title):
        """Open a dialog to rename a conversation."""
        def on_result(new_title):
            if new_title and new_title != current_title:
                self._update_conversation_title(conv_id, new_title)
        
        RenameDialog(self, current_title, on_result)

    def _update_conversation_title(self, conv_id, new_title):
        """Update the title of a saved conversation on disk."""
        path = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["title"] = new_title
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            if self.current_conversation_id == conv_id:
                self.current_conversation_title = new_title
            self._refresh_conversation_list()
        except Exception as exc:
            messagebox.showerror("Rename Conversation", f"Failed to rename: {exc}")

    def _delete_conversation(self, conv_id):
        title = next((item["title"] for item in self._list_conversations() if item["id"] == conv_id), conv_id)
        if not messagebox.askyesno(
            "Delete Conversation",
            f"Move \"{title}\" to Trash?\n\nYou can restore it later from the Trash view "
            "(Chats tab), or it can be permanently deleted from there.",
            icon="warning",
        ):
            return

        self._ensure_trash_dir()
        src = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
        dst = os.path.join(TRASH_DIR, f"{conv_id}.json")
        try:
            if os.path.isfile(src):
                shutil.move(src, dst)
        except Exception as exc:
            messagebox.showerror("Delete Conversation", f"Failed to delete: {exc}")
            return

        if self.current_conversation_id == conv_id:
            self._start_new_chat()
        self._refresh_conversation_list()

    def _list_trash(self):
        self._ensure_trash_dir()
        items = []
        try:
            filenames = os.listdir(TRASH_DIR)
        except Exception:
            filenames = []
        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(TRASH_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items.append({
                    "id": data.get("id", fname[:-5]),
                    "title": data.get("title", "(untitled)"),
                    "updated_at": data.get("updated_at", ""),
                })
            except Exception:
                continue
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return items

    def _restore_conversation(self, conv_id):
        src = os.path.join(TRASH_DIR, f"{conv_id}.json")
        dst = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
        try:
            if os.path.isfile(src):
                shutil.move(src, dst)
        except Exception as exc:
            messagebox.showerror("Restore Conversation", f"Failed to restore: {exc}")
            return
        self._refresh_conversation_list()

    def _permanently_delete_trash_item(self, conv_id):
        if not messagebox.askyesno(
            "Delete Forever",
            "Permanently delete this conversation? This cannot be undone.",
            icon="warning",
        ):
            return
        path = os.path.join(TRASH_DIR, f"{conv_id}.json")
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception as exc:
            messagebox.showerror("Delete Forever", f"Failed to delete: {exc}")

    def _open_trash_dialog(self):
        TrashDialog(self, self)

    def _replay_conversation_to_chatbox(self):
        self._clear_chat_box()
        segments = []
        for msg in self.conversation:
            role = msg.get("role")
            content = msg.get("content")
            if role == "user":
                if isinstance(content, str):
                    segments.append((f"You: {content}\n\n", "user"))
                elif isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_result":
                            txt = block.get("content", "")
                            if not isinstance(txt, str):
                                txt = str(txt)
                            segments.append((f"[TOOL RESULT] {txt[:300]}\n", "tool"))
            elif role == "assistant" and isinstance(content, list):
                texts = [b.get("text", "") for b in content if b.get("type") == "text" and b.get("text", "").strip()]
                if texts:
                    segments.append(("Claude: " + "\n".join(texts) + "\n\n", "assistant"))
                for b in content:
                    if b.get("type") == "tool_use":
                        segments.append((
                            f"[TOOL] Called: {b.get('name')}({json.dumps(b.get('input', {}), ensure_ascii=False)})\n",
                            "tool",
                        ))
                    elif b.get("type") == "compaction":
                        summary = b.get("content")
                        if summary:
                            segments.append((f"[SYSTEM] Conversation compacted here: {summary[:300]}\n", "system"))
                        else:
                            segments.append((
                                "[SYSTEM] A compaction occurred here but returned no summary.\n", "system"
                            ))
        self._append_chat_batch(segments)

    def _refresh_conversation_list(self):
        for widget in self.conversation_list_frame.winfo_children():
            widget.destroy()

        items = self._list_conversations()
        if not items:
            ctk.CTkLabel(
                self.conversation_list_frame, text="No saved conversations yet.", text_color="#AAAAAA"
            ).pack(anchor="w", pady=4)
            return

        for item in items:
            is_current = item["id"] == self.current_conversation_id
            row = ctk.CTkFrame(self.conversation_list_frame, fg_color="#1f4060" if is_current else "#2b2b2b")
            row.pack(fill="x", pady=3)
            label = ctk.CTkLabel(row, text=item["title"], anchor="w", justify="left", wraplength=120, font=("Consolas", 11))
            label.pack(side="left", fill="x", expand=True, padx=6, pady=6)
            label.bind("<Button-1>", lambda e, cid=item["id"]: self._open_conversation(cid))
            ctk.CTkButton(
                row, text="Edit", width=40, fg_color="#555555", hover_color="#666666",
                command=lambda cid=item["id"], title=item["title"]: self._rename_conversation(cid, title),
            ).pack(side="right", padx=2)
            ctk.CTkButton(
                row, text="X", width=24, fg_color="#8B2E2E", hover_color="#6E2424",
                command=lambda cid=item["id"]: self._delete_conversation(cid),
            ).pack(side="right", padx=2)

    def init_client(self):
        if anthropic is None:
            messagebox.showerror("Missing dependency", "The 'anthropic' package is not installed.\nRun: pip install anthropic")
            return False
        key = self.api_key_entry.get().strip()
        if not key:
            messagebox.showerror("API Key Required", "Please enter your Anthropic API key first.")
            return False
        try:
            self.client = anthropic.Anthropic(api_key=key)
            self.connection_status.configure(text="Key set (verified on first message)", text_color="#7CFC00")
            return True
        except Exception as exc:
            self.client = None
            self.connection_status.configure(text="Failed to initialize client", text_color="#FF6B6B")
            messagebox.showerror("Connection Error", str(exc))
            return False

    # ==================================================================
    # Thread-safe GUI append helpers
    # (Call these from ANY thread; they marshal onto the main loop via after())
    # ==================================================================

    def _auto_connect_if_key_available(self):
        """Auto-connect to Anthropic on startup if a key was previously saved."""
        if self.api_key_entry.get().strip() and self.client is None:
            self.init_client()


    def _switch_to_chats_tab(self):
        """Switch to Chats tab after UI is fully initialized (deferred)."""
        try:
            self.sidebar_tabs.set("Chats")
        except Exception:
            # If tab switch fails, silently ignore - UI is still functional
            pass

    def _start_thinking_indicator(self):
        """Show an animated thinking indicator while Claude works."""
        self._thinking_frame_index = 0
        self._show_thinking_frame()

    def _show_thinking_frame(self):
        """Animate the thinking indicator (pulsing dots)."""
        if not self.busy:
            return
        frames = [
            "[Thinking.]",
            "[Thinking..]",
            "[Thinking...]",
            "[Thinking   ]",
        ]
        frame = frames[self._thinking_frame_index % len(frames)]
        self.safe_set_status(frame)
        self._thinking_frame_index += 1
        if self.busy:
            self.after(500, self._show_thinking_frame)

    def safe_append_serial_line(self, line):
        self.after(0, self._append_serial_line, line)

    def safe_set_serial_status(self, status):
        self.after(0, self._set_serial_status, status)

    def safe_append_user_message(self, text):
        self.after(0, self._append_chat, f"You: {text}\n\n", "user")

    def safe_append_assistant_message(self, text):
        self.after(0, self._append_chat, f"Claude: {text}\n\n", "assistant")

    def safe_append_tool_log(self, text):
        self.after(0, self._append_chat, f"[TOOL] {text}\n", "tool")

    def safe_append_system_message(self, text):
        self.after(0, self._append_chat, f"[SYSTEM] {text}\n", "system")

    def safe_set_status(self, text):
        # NOTE: CustomTkinter's .configure() signature is (require_redraw=False,
        # **kwargs) - unlike classic tkinter, it does NOT accept a dict as a
        # positional argument to set options. self.after(0, widget.configure,
        # {"text": text}) silently passes the dict into require_redraw instead
        # (no exception - a dict is just truthy) and the text is never actually
        # updated. Always call configure with an explicit keyword argument.
        def _update():
            self.status_label.configure(text=text)
        self.after(0, _update)

    def safe_set_send_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        def _update():
            self.send_button.configure(state=state)
            self.input_text.configure(state=state)
            self.attach_button.configure(state=state)
        self.after(0, _update)

    def safe_update_auto_approved_label(self):
        def _update():
            if self.auto_approved_tools:
                self.auto_approved_label.configure(text=", ".join(sorted(self.auto_approved_tools)))
            else:
                self.auto_approved_label.configure(text="(none - read-only actions are always pre-approved)")
        self.after(0, _update)

    def safe_refresh_conversation_list(self):
        self.after(0, self._refresh_conversation_list)

    # These are safe to call directly ONLY from the main thread.
    def append_tool_log(self, text):
        self._append_chat(f"[TOOL] {text}\n", "tool")

    def append_user_message(self, text):
        self._append_chat(f"You: {text}\n\n", "user")

    def _append_chat(self, text, tag):
        self.chat_box.configure(state="normal")
        self.chat_box._textbox.insert("end", text, tag)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _append_chat_batch(self, segments):
        """Insert several (text, tag) segments in a single enabled/disabled
        transaction and scroll only once at the end. Calling _append_chat once
        per message (as the old replay code did) means a full widget
        state-toggle + scroll-into-view recalculation per message; for a
        several-hundred-KB conversation with hundreds of turns that adds up to
        a multi-second freeze on the main thread. Batching cuts that down to
        one reconfigure and one scroll regardless of how many messages there
        are."""
        if not segments:
            return
        self.chat_box.configure(state="normal")
        for text, tag in segments:
            self.chat_box._textbox.insert("end", text, tag)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _append_serial_line(self, line):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.serial_box.configure(state="normal")
        self.serial_box.insert("end", f"[{timestamp}] {line}\n")
        self.serial_box.configure(state="disabled")
        self.serial_box.see("end")

    def _set_serial_status(self, status):
        color = "#7CFC00" if "Connected" in status else ("#FF6B6B" if "Error" in status else "#AAAAAA")
        self.serial_status_label.configure(text=status, text_color=color)

    # ==================================================================
    # Chat send / agent loop
    # ==================================================================
    def _estimate_current_context_tokens(self):
        """Rough LOCAL estimate of the current conversation's size (chars/3).
        Only meaningful as a fallback when we have no real server-reported
        number yet - see _context_budget_check() for why. Once compaction has
        pruned the effective server-side context, this keeps growing forever
        (it measures our full raw local history) and stops being a valid
        proxy for what will actually be sent."""
        system_prompt = self._build_system_prompt()
        tools = build_tools(list(self.workspaces.keys()))
        return (
            _rough_token_estimate(system_prompt)
            + _rough_token_estimate(tools)
            + _rough_token_estimate(self.conversation)
        )

    def _context_budget_check(self):
        """
        Returns (over_budget: bool, estimated_tokens: int or None, is_real: bool).

        Prefers self.last_known_input_tokens - the server's own reported
        input_tokens from the most recent response for THIS conversation,
        which already reflects any server-side compaction that occurred -
        over our own rough local estimate. This matters because once
        compaction starts pruning the effective context, our local estimate
        (based on the full raw conversation, which only ever grows) is no
        longer a meaningful proxy for what will actually be sent; using the
        real number avoids blocking sends that compaction would have handled
        fine, while still correctly catching the case where compaction is
        silently failing (the real number stays high despite it being
        enabled).

        When we have no real number yet (fresh session, or a conversation
        just reopened from disk) and compaction is available for the current
        model, we trust it rather than blocking based on a local estimate
        that doesn't account for it at all.
        """
        model = self.model_var.get()
        use_compaction = self.enable_compaction_var.get() and model in COMPACTION_SUPPORTED_MODELS

        if self.last_known_input_tokens is not None:
            estimated = self.last_known_input_tokens
            over = estimated > MODEL_CONTEXT_WINDOW - CONTEXT_SAFETY_MARGIN
            return over, estimated, True

        if use_compaction:
            # No real measurement yet this session - let the API and
            # compaction handle it rather than second-guessing with a local
            # estimate that ignores compaction entirely.
            return False, None, False

        estimated = self._estimate_current_context_tokens()
        over = estimated > MODEL_CONTEXT_WINDOW - CONTEXT_SAFETY_MARGIN
        return over, estimated, False

    def _record_response_usage(self, response):
        """Captures the server-reported input token count after a response,
        for use by _context_budget_check() on the next turn."""
        try:
            self.last_known_input_tokens = response.usage.input_tokens
        except Exception:
            pass

    def on_send(self, event=None):
        if self.busy:
            return
        text = self.input_text.get("1.0", "end-1c").strip()
        if not text:
            return

        if self.client is None:
            if not self.init_client():
                return

        over_budget, estimated, is_real = self._context_budget_check()
        if over_budget:
            detail = "server-reported" if is_real else "estimated"
            self.append_tool_log(
                f"This conversation is too large for the model's context window "
                f"(~{estimated:,} {detail} tokens, limit {MODEL_CONTEXT_WINDOW:,}) "
                "and can no longer accept new messages. Start a '+ New Chat' from "
                "the Chats tab to continue - this conversation stays saved and can "
                "still be reopened to review."
            )
            return

        self.input_text.delete("1.0", "end")
        self.append_user_message(text)
        self.busy = True
        self.safe_set_send_enabled(False)
        self.safe_set_status("Thinking...")
        self._start_thinking_indicator()

        thread = threading.Thread(target=self._run_conversation_turn, args=(text,), daemon=True)
        thread.start()

    def _run_conversation_turn(self, user_text):
        """Runs entirely on a background thread; all GUI touches go through safe_* helpers."""
        try:
            self.conversation.append({"role": "user", "content": user_text})

            consecutive_truncated_tool_calls = 0
            for _ in range(MAX_TOOL_ITERATIONS):
                try:
                    max_tokens = int(self.max_tokens_var.get())
                except (ValueError, AttributeError):
                    max_tokens = DEFAULT_MAX_TOKENS

                # A large tool result (a big file read, a verbose script's
                # output) can push the conversation over budget mid-turn, even
                # if it was fine when the turn started. Check again before
                # every API call in this loop, not just once at the top of
                # on_send. Uses the same real-number-first logic as
                # on_send (see _context_budget_check()) so an active
                # compaction path isn't blocked by our own stale local guess.
                over_budget, estimated, is_real = self._context_budget_check()
                if over_budget:
                    detail = "server-reported" if is_real else "estimated"
                    self.safe_append_system_message(
                        f"Stopping: this conversation is at ~{estimated:,} {detail} "
                        f"tokens, over the model's {MODEL_CONTEXT_WINDOW:,}-token "
                        "context window. It can no longer accept new messages - "
                        "start a '+ New Chat' from the Chats tab to continue."
                    )
                    break

                model = self.model_var.get()
                use_compaction = self.enable_compaction_var.get() and model in COMPACTION_SUPPORTED_MODELS

                try:
                    if use_compaction:
                        try:
                            response = self.client.beta.messages.create(
                                betas=[COMPACTION_BETA_HEADER],
                                model=model,
                                max_tokens=max_tokens,
                                system=self._build_system_prompt(),
                                tools=build_tools(list(self.workspaces.keys())),
                                messages=self.conversation,
                                context_management={
                                    "edits": [
                                        {
                                            "type": COMPACTION_STRATEGY_TYPE,
                                            "instructions": COMPACTION_TOOL_SAFE_INSTRUCTIONS,
                                        }
                                    ]
                                },
                            )
                        except (AttributeError, TypeError) as sdk_exc:
                            # Installed 'anthropic' package predates the beta compaction
                            # surface (no client.beta.messages.create / context_management
                            # parameter) - fall back to a plain call rather than crashing.
                            self.safe_append_system_message(
                                "Note: the installed 'anthropic' package doesn't support "
                                f"context compaction yet ({sdk_exc}); continuing without "
                                "it. Run: pip install --upgrade anthropic"
                            )
                            response = self.client.messages.create(
                                model=model,
                                max_tokens=max_tokens,
                                system=self._build_system_prompt(),
                                tools=build_tools(list(self.workspaces.keys())),
                                messages=self.conversation,
                            )
                    else:
                        response = self.client.messages.create(
                            model=model,
                            max_tokens=max_tokens,
                            system=self._build_system_prompt(),
                            tools=build_tools(list(self.workspaces.keys())),
                            messages=self.conversation,
                        )
                except Exception as api_exc:
                    self.safe_append_system_message(f"API error: {api_exc}")
                    break

                # Record the server's real input-token count for this
                # conversation before anything else - this is what
                # _context_budget_check() will use on the next call, and it
                # already reflects any compaction that just happened.
                self._record_response_usage(response)

                content_blocks = response.content

                # Surface compaction events: the docs warn that with tools defined
                # (which this app always sends), the model can occasionally call a
                # tool during the internal summarization step instead of writing a
                # summary, leaving content: None. We still pass the block back to
                # the API exactly as documented either way - just tell the user
                # what happened, since a null summary means older context may have
                # been dropped without anything useful to replace it.
                for cb in content_blocks:
                    if getattr(cb, "type", None) == "compaction":
                        if getattr(cb, "content", None):
                            self.safe_append_system_message(
                                "Conversation was automatically compacted to stay "
                                "within the context window (older messages summarized "
                                "server-side; the full history remains in this saved "
                                "conversation file)."
                            )
                        else:
                            self.safe_append_system_message(
                                "Warning: a context compaction triggered but returned "
                                "no summary (the model may have tried to call a tool "
                                "instead of summarizing). Earlier context may have been "
                                "dropped without a usable replacement."
                            )

                text_parts = [b.text for b in content_blocks if getattr(b, "type", None) == "text" and b.text.strip()]
                if text_parts:
                    self.safe_append_assistant_message("\n".join(text_parts))

                # Anthropic SDK message objects aren't directly JSON-serializable
                # dicts; convert content blocks to plain dicts for re-submission.
                self.conversation.append({"role": "assistant", "content": self._blocks_to_dicts(content_blocks)})

                # IMPORTANT: decide whether to run tools based on the actual presence
                # of tool_use blocks, not on stop_reason alone. If the response hits
                # the max_tokens limit while a tool_use block was being generated,
                # stop_reason is "max_tokens" (not "tool_use") even though a tool_use
                # block is present in content - checking stop_reason alone would skip
                # it and leave a tool_use with no matching tool_result, permanently
                # corrupting the conversation history for every future turn.
                tool_use_blocks = [b for b in content_blocks if getattr(b, "type", None) == "tool_use"]

                if not tool_use_blocks:
                    if response.stop_reason == "max_tokens":
                        self.safe_append_system_message(
                            "Note: the response was truncated at the max_tokens limit."
                        )
                    break

                if response.stop_reason == "max_tokens":
                    self.safe_append_system_message(
                        "Note: response hit the max_tokens limit while a tool call was "
                        "being generated; attempting to run the tool call(s) that were captured."
                    )

                # Defense in depth: no matter what happens while processing tool calls
                # (a bug, a dialog error, etc.), every tool_use id above MUST end up
                # with a matching tool_result appended below, or the next API call
                # will be rejected the same way. Track failures instead of letting
                # an exception skip the append.
                tool_result_blocks = []
                loop_error = None
                any_truncated_this_round = False
                try:
                    for block in tool_use_blocks:
                        tool_name = block.name
                        tool_input = block.input or {}

                        # A tool_use whose input JSON was cut off mid-generation (because
                        # the response hit max_tokens) parses back as missing/empty required
                        # fields. Running it is pointless (e.g. run_python_code with no
                        # code) and asking the user to approve a call that can't succeed is
                        # unhelpful - skip execution/approval entirely and tell Claude why,
                        # instead of the previous behavior of blindly retrying identically
                        # up to MAX_TOOL_ITERATIONS times.
                        if response.stop_reason == "max_tokens" and _tool_input_is_incomplete(tool_name, tool_input):
                            any_truncated_this_round = True
                            result_text = (
                                "This tool call was cut off before its input finished "
                                "generating (the response hit the max_tokens limit) and was "
                                "NOT run. Generate shorter code/commands, or split the task "
                                "into multiple smaller tool calls."
                            )
                            is_error = True
                            self.safe_append_tool_log(
                                f"[TRUNCATED] {tool_name}: input was incomplete - skipped, not executed."
                            )
                        else:
                            self.safe_append_tool_log(
                                f"Requested: {tool_name}({json.dumps(tool_input, ensure_ascii=False)})"
                            )

                            approved = self._get_approval(tool_name, tool_input)
                            if approved:
                                result_text = self._execute_tool(tool_name, tool_input)
                                is_error = False
                            else:
                                result_text = "The user denied permission to run this tool. Do not repeat this exact call."
                                is_error = True

                            self.safe_append_tool_log(
                                f"{'DENIED' if is_error else 'Result'} [{tool_name}]: {result_text[:1500]}"
                            )

                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                            "is_error": is_error,
                        })
                except Exception as inner_exc:
                    loop_error = inner_exc
                    # Fill in a tool_result for any tool_use block that didn't get one
                    # before the exception hit, so the pairing stays complete.
                    handled_ids = {b["tool_use_id"] for b in tool_result_blocks}
                    for block in tool_use_blocks:
                        if block.id not in handled_ids:
                            tool_result_blocks.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Tool execution aborted due to an internal error: {inner_exc}",
                                "is_error": True,
                            })
                finally:
                    # Always append, even on failure, so every tool_use above has a match.
                    self.conversation.append({"role": "user", "content": tool_result_blocks})

                if loop_error is not None:
                    self.safe_append_system_message(f"Internal error while running tool(s): {loop_error}")
                    break

                if any_truncated_this_round:
                    consecutive_truncated_tool_calls += 1
                else:
                    consecutive_truncated_tool_calls = 0

                if consecutive_truncated_tool_calls >= 2:
                    self.safe_append_system_message(
                        "Stopping: two responses in a row were truncated by the max_tokens "
                        "limit while generating a tool call. Try raising 'Max Output Tokens' "
                        "in the Setup tab, or ask for a smaller piece of work at a time."
                    )
                    break
                # otherwise loop again so Claude can see the tool result(s) and continue

            self.safe_set_status("Ready")

        except Exception:
            err = traceback.format_exc()
            self.safe_append_system_message(f"Unexpected error:\n{err}")
            self.safe_set_status("Error")
        finally:
            self.busy = False
            self.safe_set_send_enabled(True)
            self._save_current_conversation()
            self.safe_refresh_conversation_list()

    @staticmethod
    def _blocks_to_dicts(content_blocks):
        """Convert Anthropic SDK content blocks into plain JSON-able dicts."""
        out = []
        for b in content_blocks:
            btype = getattr(b, "type", None)
            if btype == "text":
                out.append({"type": "text", "text": b.text})
            elif btype == "tool_use":
                out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif btype == "compaction":
                # Explicit handling (rather than relying solely on the generic
                # model_dump() fallback below) so this app's on-disk conversation
                # format for compaction blocks is predictable regardless of SDK
                # version. Must be passed back to the API exactly as received on
                # the next request - see COMPACTION_* constants near the top.
                entry = {"type": "compaction", "content": getattr(b, "content", None)}
                encrypted = getattr(b, "encrypted_content", None)
                if encrypted is not None:
                    entry["encrypted_content"] = encrypted
                out.append(entry)
            else:
                # Fall back to model_dump if the SDK provides it (pydantic models).
                if hasattr(b, "model_dump"):
                    out.append(b.model_dump())
                else:
                    out.append({"type": btype, "data": str(b)})
        return out

    # ------------------------------------------------------------------
    # Approval guard
    # ------------------------------------------------------------------
    def _is_pre_approved(self, tool_name, tool_input):
        action = (tool_input or {}).get("action")
        
        # Check if the exact tool name is in auto-approved list
        if tool_name in self.auto_approved_tools:
            return True
        
        # Check if (tool_name, action) is in auto-approved list (for write actions, etc.)
        if f"{tool_name} ({action})" in self.auto_approved_tools:
            return True
        
        # Check inherently safe actions
        if (tool_name, action) in AUTO_APPROVE_SAFE_ACTIONS:
            return True
        
        return False

    def _get_approval(self, tool_name, tool_input):
        """Blocks the calling (worker) thread until the user responds in the GUI."""
        if self._is_pre_approved(tool_name, tool_input):
            return True

        event = threading.Event()
        result_holder = {"approved": False}

        def on_result(approved, always):
            result_holder["approved"] = approved
            if approved and always:
                self.auto_approved_tools.add(tool_name)
                self.safe_update_auto_approved_label()
            event.set()

        def show_dialog():
            ApprovalDialog(self, tool_name, tool_input, on_result)

        self.after(0, show_dialog)
        event.wait()  # blocks the worker thread only, GUI stays responsive
        return result_holder["approved"]

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------
    def _execute_tool(self, tool_name, tool_input):
        try:
            if tool_name == "manage_local_file":
                root, err = self._resolve_workspace(tool_input.get("workspace"))
                if err:
                    return err
                return manage_local_file(
                    action=tool_input.get("action"),
                    filename=tool_input.get("filename"),
                    content=tool_input.get("content"),
                    workspace_dir=root,
                )

            elif tool_name == "run_shell_command":
                root, err = self._resolve_workspace(tool_input.get("workspace"))
                if err:
                    return err
                timeout_seconds = int(tool_input.get("timeout_seconds", DEFAULT_SHELL_TIMEOUT))
                return run_shell_command(
                    command=tool_input.get("command", ""),
                    cwd=root,
                    on_output_line=self.safe_append_tool_log,
                    timeout_seconds=timeout_seconds,
                )

            elif tool_name == "run_python_code":
                root, err = self._resolve_workspace(tool_input.get("workspace"))
                if err:
                    return err
                timeout_seconds = int(tool_input.get("timeout_seconds", DEFAULT_PYTHON_TIMEOUT))
                return run_python_code(
                    code=tool_input.get("code", ""),
                    cwd=root,
                    on_output_line=self.safe_append_tool_log,
                    timeout_seconds=timeout_seconds,
                )

            elif tool_name == "control_serial_monitor":
                action = tool_input.get("action")
                port = tool_input.get("port") or self.com_port_var.get()
                baud = int(tool_input.get("baud") or self.baud_var.get() or DEFAULT_BAUD)

                if action == "start":
                    return self.serial_manager.start(port, baud)
                elif action == "stop":
                    return self.serial_manager.stop()
                elif action == "read_buffer":
                    return self.serial_manager.read_buffer()
                else:
                    return f"Error: Unknown control_serial_monitor action '{action}'."

            else:
                return f"Error: Unknown tool '{tool_name}'."

        except Exception as exc:
            return f"Tool execution error: {exc}"


# =========================================================================
# Entry point
# =========================================================================
def main():
    if sys.platform != "win32":
        print("Warning: this application is designed for Windows 11 "
              "(PowerShell execution and COM-port serial access). "
              "It will still run, but shell/hardware tools will likely fail.")
    app = ClaudeAgentApp()
    app.mainloop()


if __name__ == "__main__":
    main()
