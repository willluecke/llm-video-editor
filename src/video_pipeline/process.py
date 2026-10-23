from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CommandRunner:
    dry_run: bool = False
    commands: list[list[str]] = field(default_factory=list)

    def require(self, executable: str) -> str:
        found = shutil.which(executable)
        if not found and not self.dry_run:
            raise RuntimeError(f"Required executable not found: {executable}")
        return found or executable

    def run(self, command: list[str], cwd: Path | None = None) -> None:
        self.commands.append(command)
        print("$", shlex.join(command))
        if not self.dry_run:
            subprocess.run(command, cwd=cwd, check=True)

    def capture(self, command: list[str]) -> str:
        self.commands.append(command)
        if self.dry_run:
            return "{}"
        return subprocess.run(command, check=True, text=True, capture_output=True).stdout

    def capture_combined(self, command: list[str]) -> str:
        self.commands.append(command)
        if self.dry_run:
            return ""
        result = subprocess.run(command, check=True, text=True, capture_output=True)
        return result.stdout + result.stderr
