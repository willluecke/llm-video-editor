from __future__ import annotations

import base64
import fcntl
import json
import os
import pty
import shutil
import signal
import struct
import subprocess
import termios
import threading
import uuid
from pathlib import Path
from typing import Any


MAX_BUFFER_BYTES = 2_000_000

OPERATOR_INSTRUCTIONS = """You are the Operator embedded inside LLM Video Editor. You may discuss and review edits normally. When the user asks you to change the open timeline, do not directly edit .llmve files. Authenticate local API calls with the header `X-LLMVE-Token: $LLM_VIDEO_EDITOR_TOKEN`. First GET `$LLM_VIDEO_EDITOR_URL/api/operator/context` and read its project_id, revision, episode_template, and template_status when present. POST structured actions to `$LLM_VIDEO_EDITOR_URL/api/operator/actions` as JSON with exactly this envelope: {\"project_id\":\"...\",\"base_revision\":1,\"batch_id\":\"stable-unique-id\",\"actions\":[...]}. Reuse the same batch_id when retrying the same requested change; the server applies and saves it exactly once before acknowledging it. Supported actions: add_clip(asset_id, track_id, timeline_start, source_start?, source_end?); replace_clip(clip_id, asset_id), which preserves the encoded slot timing and matching production audio; move_clip(clip_id, track_id?, timeline_start); trim_clip(clip_id, source_start?, source_end?); delete_clip(clip_id); set_clip(clip_id, patch); split_clip(clip_id, time); add_track(kind); unlink_audio(clip_id); add_title(text, timeline_start?, duration?); add_caption(text, timeline_start?, duration?); set_export_preset(preset). Do not modify protected template tracks. If context says dirty=true, ask the user to save manual changes before posting actions. After posting, summarize what changed. Never open, close, reload, or manage browser tabs or editor windows, and never stop or restart the editor server. UI lifecycle is controlled only by the user; post actions without navigating the app."""


def find_codex() -> Path | None:
    local = Path.home() / ".local" / "bin" / "codex"
    if local.is_file():
        return local
    discovered = shutil.which("codex")
    return Path(discovered) if discovered else None


def codex_status() -> dict[str, Any]:
    executable = find_codex()
    if not executable:
        return {"available": False, "authenticated": False, "label": "Codex is not installed"}
    try:
        result = subprocess.run(
            [str(executable), "login", "status"],
            text=True,
            capture_output=True,
            timeout=8,
        )
        detail = (result.stdout + result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": True, "authenticated": False, "label": str(error)}
    authenticated = result.returncode == 0 and "logged in" in detail.lower()
    if "chatgpt" in detail.lower():
        label = "ChatGPT subscription"
    elif authenticated:
        label = "Codex authenticated"
    else:
        label = "Codex sign-in required"
    return {
        "available": True,
        "authenticated": authenticated,
        "label": label,
        "version": _codex_version(executable),
    }


def _codex_version(executable: Path) -> str:
    try:
        result = subprocess.run([str(executable), "--version"], text=True, capture_output=True, timeout=5)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class TerminalSession:
    def __init__(
        self,
        root: Path,
        columns: int,
        rows: int,
        base_url: str = "http://127.0.0.1:8765",
        api_token: str = "",
        full_access: bool = False,
    ):
        executable = find_codex()
        if not executable:
            raise RuntimeError("Codex CLI is not installed")
        self.id = uuid.uuid4().hex
        self.root = root.resolve()
        self._master_fd, slave_fd = pty.openpty()
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._base_cursor = 0
        self._closed = False
        self._trust_answered = False
        self.resize(columns, rows)
        environment = os.environ.copy()
        environment.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "TERM_PROGRAM": "LLMVideoEditor",
            "LLM_VIDEO_EDITOR_URL": base_url,
            "LLM_VIDEO_EDITOR_TOKEN": api_token,
        })
        command = [str(executable)]
        if full_access:
            command.append("--yolo")
        command.extend([
            "-C",
            str(self.root),
            "--config",
            "check_for_update_on_startup=false",
            "--config",
            f"developer_instructions={json.dumps(OPERATOR_INSTRUCTIONS)}",
        ])
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.root,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=environment,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        self._reader = threading.Thread(target=self._read_output, name=f"codex-terminal-{self.id[:8]}", daemon=True)
        self._reader.start()

    @property
    def alive(self) -> bool:
        return not self._closed and self.process.poll() is None

    def _append(self, data: bytes) -> None:
        accept_trust = False
        with self._lock:
            self._buffer.extend(data)
            if (
                not self._trust_answered
                and b"directory?" in self._buffer
                and b"Yes, continue" in self._buffer
            ):
                self._trust_answered = True
                accept_trust = True
            excess = len(self._buffer) - MAX_BUFFER_BYTES
            if excess > 0:
                del self._buffer[:excess]
                self._base_cursor += excess
        if accept_trust and self.alive:
            os.write(self._master_fd, b"\r")

    def _read_output(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self._master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self._append(chunk)
        finally:
            exit_code = self.process.poll()
            if exit_code not in (None, 0) and not self._closed:
                self._append(f"\r\n\x1b[31mCodex exited with status {exit_code}.\x1b[0m\r\n".encode())

    def output(self, cursor: int) -> dict[str, Any]:
        with self._lock:
            cursor = max(cursor, self._base_cursor)
            offset = min(max(0, cursor - self._base_cursor), len(self._buffer))
            data = bytes(self._buffer[offset:])
            next_cursor = self._base_cursor + len(self._buffer)
        return {
            "data": base64.b64encode(data).decode(),
            "cursor": next_cursor,
            "alive": self.alive,
        }

    def write(self, data: str) -> None:
        if not self.alive:
            raise RuntimeError("Codex terminal is not running")
        os.write(self._master_fd, data.encode())

    def resize(self, columns: int, rows: int) -> None:
        columns = max(24, min(int(columns), 300))
        rows = max(8, min(int(rows), 120))
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def interrupt(self) -> None:
        if self.alive:
            os.write(self._master_fd, b"\x03")

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
        try:
            os.close(self._master_fd)
        except OSError:
            pass


class TerminalManager:
    def __init__(
        self,
        root: Path,
        base_url: str = "http://127.0.0.1:8765",
        api_token: str = "",
        full_access: bool = False,
    ):
        self.root = root.resolve()
        self.base_url = base_url
        self.api_token = api_token
        self.full_access = full_access
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()

    def start(self, columns: int = 50, rows: int = 30) -> TerminalSession:
        with self._lock:
            self._prune_dead_locked()
            if len(self._sessions) >= 3:
                oldest_id = next(iter(self._sessions))
                self._sessions.pop(oldest_id).stop()
            session = TerminalSession(self.root, columns, rows, self.base_url, self.api_token, self.full_access)
            self._sessions[session.id] = session
            return session

    def attach_or_start(self, columns: int = 50, rows: int = 30) -> tuple[TerminalSession, bool]:
        with self._lock:
            self._prune_dead_locked()
            if self._sessions:
                newest_id = next(reversed(self._sessions))
                session = self._sessions[newest_id]
                session.resize(columns, rows)
                return session, True
            session = TerminalSession(self.root, columns, rows, self.base_url, self.api_token, self.full_access)
            self._sessions[session.id] = session
            return session, False

    def restart(self, columns: int = 50, rows: int = 30) -> TerminalSession:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()
        return self.start(columns, rows)

    def _prune_dead_locked(self) -> None:
        dead = [session_id for session_id, session in self._sessions.items() if not session.alive]
        for session_id in dead:
            self._sessions.pop(session_id).stop()

    def get(self, session_id: str) -> TerminalSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise FileNotFoundError("Codex terminal session not found")
        return session

    def stop(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if not session:
            raise FileNotFoundError("Codex terminal session not found")
        session.stop()

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()
