# My Claude Local Agent

A private, local Windows desktop client for the Anthropic Claude API. Claude
acts as a general-purpose autonomous agent with access to:

- **File management** - read/write/delete text files inside sandboxed workspace folders
- **PowerShell** - run shell commands (npm, pip, git, arduino-cli, esptool, etc.)
- **Python execution** - run ad-hoc Python scripts (pandas/openpyxl/python-docx/pdfplumber available)
- **ESP32 serial monitor** - start/stop/read a background USB serial connection
- Ordinary chat, research, and long-form writing when no tool is needed

Every destructive or execution-capable tool call is intercepted by a modal
approval dialog unless you've pre-approved that action type. Conversations
auto-save to disk and can be reopened from the Chats tab.

---

## Setup on a new (virgin) Windows 11 machine

### 1. Copy the app files
Copy this entire folder to the new machine. At minimum you need these six files:

| File | Purpose |
|---|---|
| `my_claude_agent_app.py` | The application itself |
| `requirements.txt` | List of required Python packages |
| `Setup-NewComputer.bat` | **One-shot setup** - installs Python (if missing), packages, and the taskbar shortcut |
| `run_my_claude_agent.bat` | Double-click launcher (no console window) |
| `Create-TaskbarShortcut.ps1` | Used by the setup script to create the taskbar shortcut (can also be re-run on its own later) |
| `README.md` | This file |

You do **not** need to copy `agent_config.json` or the `conversations/`
folder from this machine - those are personal data/settings specific to this
install and will be created fresh (or you can copy them too if you want to
carry over your saved chats and settings).

### 2. Run the setup script
Double-click **`Setup-NewComputer.bat`**. It will:
1. Install Python 3.12 silently, only if no Python install is found on PATH
   (skips this step entirely if Python 3.9+ is already installed).
2. Install all required packages from `requirements.txt`.
3. Create the `My Claude Local Agent.lnk` taskbar shortcut.

If Python had to be freshly installed and the script reports it still can't
find `python` on PATH afterwards, just close the window, open a new Command
Prompt, and run `Setup-NewComputer.bat` again - the second run picks up
where it left off.

### 3. Pin the shortcut (optional)
Right-click the newly created `My Claude Local Agent.lnk` and choose
**Pin to taskbar**.

> **Note:** Windows copies a pinned shortcut's target into its own internal
> location the moment you pin it. If you ever need to change the shortcut's
> target again later (e.g. after moving the app folder), either unpin and
> re-pin it fresh, or edit the pinned copy directly (found under
> `%APPDATA%\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\`) -
> editing the `.lnk` file in this folder alone will not update an
> already-pinned icon. Re-running `Create-TaskbarShortcut.ps1` only updates
> the copy in this folder for the same reason.

The shortcut targets `pythonw.exe` (not `python.exe`) specifically so no
black console window appears alongside the app.

### 4. Launch it
Double-click `run_my_claude_agent.bat`, the pinned taskbar icon, or run
directly:
```powershell
python my_claude_agent_app.py
```

### 5. Enter your Anthropic API key
On first launch, go to the **Setup** tab, paste your API key
(`sk-ant-...`), and click **Connect / Test Key**. Check "Remember key on
disk" if you want it to auto-connect on future launches.

### 6. Add a workspace
Go to the **Workspaces** tab and click **+ Add Workspace** to point Claude at
a folder it's allowed to read/write/execute in. You can add more than one and
Claude will ask which to use (or you can tell it explicitly).

---

## A note on Python versions

`Setup-NewComputer.bat` installs Python 3.12 if nothing is found, but the app
also works fine on an existing Python 3.9+ install. One difference: the
optional auto-compaction feature (Setup tab checkbox, for very long
conversations) requires `anthropic>=1.4.0`, which itself requires Python
3.10+. On Python 3.9, pip installs the newest compatible 0.x release of
`anthropic` instead, and the app automatically falls back to working without
compaction - fully functional otherwise. Upgrade to Python 3.10+ later and
run `pip install --upgrade anthropic` if you want compaction.

---

## App features at a glance

**Setup tab** - API key, model selection, max output tokens, auto-compaction
toggle, billing method, pre-approval checkboxes for each tool type.

**Workspaces tab** - Add/remove sandboxed folders Claude can operate in.

**ESP32 tab** - Toggle the serial monitor panel, pick COM port/baud rate,
start/stop the connection.

**Chats tab** - Start a new chat, browse/reopen/rename/delete saved
conversations, and view/restore/permanently-delete trashed conversations.

**Keyboard shortcuts:** `Ctrl+Enter` = send message, `Shift+Enter` = new
line in the input box.

---

## Troubleshooting

**Setup script fails to install packages:** try running it again - transient
network hiccups during pip installs are the most common cause.

**App won't start / import errors:** confirm `pip install -r requirements.txt`
completed without errors, and that you're running Python 3.9+.

**Two windows appear on launch:** make sure you're launching via
`run_my_claude_agent.bat` or a shortcut that targets `pythonw.exe` (not
`python.exe`) - `python.exe` always opens an extra console window.

**Where are my conversations saved?** In the `conversations/` folder next to
the app, as human-readable JSON files. Deleted ones move to
`conversations/.trash/` and can be restored from the Chats tab's Trash view.

**Context window / conversation too large:** start a new chat from the Chats
tab - the old one stays saved and can still be reopened to review.
