from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .settings_bridge import settings
from .schemas import (
    AgentDefinition,
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
    Conversation,
    ConversationCreate,
    ConversationDetail,
    Message,
    SkillDefinition,
    SkillDefinitionCreate,
    WorkflowDefinition,
    WorkflowDefinitionCreate,
    WorkflowDefinitionUpdate,
    WorkflowTemplate,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


class SQLitePlaygroundStore:
    def __init__(self, db_path: Path | None = None) -> None:
        app_home = Path(settings.APP_HOME).resolve()
        bundled_skills_root = Path(settings.BUNDLED_SKILLS_ROOT).resolve()
        self.app_home = app_home
        self.db_path = db_path or (app_home / "data" / "agent_playground.db")
        self.skills_root = app_home / "skills"
        self.bundled_skills_root = bundled_skills_root
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.skills_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _iter_skill_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for root in (self.bundled_skills_root, self.skills_root):
            normalized = str(root.resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            roots.append(root)
        return roots

    def _resolve_skill_root_for_path(self, skill_dir: Path) -> Path:
        resolved = skill_dir.resolve()
        for root in self._iter_skill_roots():
            try:
                resolved.relative_to(root.resolve())
                return root
            except (OSError, ValueError):
                continue
        return self.skills_root

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        ddl_fragment: str,
    ) -> None:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        columns = {row["name"] for row in rows}
        if column_name not in columns:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_fragment}")

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    model TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(
                connection,
                "agents",
                "skill_ids",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                connection,
                "agents",
                "builtin_capabilities",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    specialist_agent_ids TEXT NOT NULL,
                    router_prompt TEXT NOT NULL,
                    finalizer_enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(
                connection,
                "skills",
                "source_provider",
                "TEXT NULL",
            )
            self._ensure_column(
                connection,
                "skills",
                "source_skill_id",
                "TEXT NULL",
            )
            self._ensure_column(
                connection,
                "skills",
                "local_path",
                "TEXT NULL",
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_skills_source_unique
                ON skills(source_provider, source_skill_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    title TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    agent_name TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
                ON messages(conversation_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _rename_default_workflow_names(self) -> None:
        rename_pairs = [
            ("Default Router Demo", "Router"),
            ("Planner Executor Demo", "Planner"),
            ("Supervisor Dynamic Demo", "Supervisor"),
        ]
        with self._connect() as connection:
            for old_name, new_name in rename_pairs:
                connection.execute(
                    """
                    UPDATE workflows
                    SET name = ?
                    WHERE name = ?
                    """,
                    (new_name, old_name),
                )

    def get_app_settings_payload(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT value
                FROM app_settings
                WHERE key = 'main'
                """
            ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError, KeyError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_app_settings_payload(self, payload: dict[str, object]) -> None:
        serialized = json.dumps(payload, ensure_ascii=False)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ('main', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (serialized, now),
            )

    def _row_to_agent(self, row: sqlite3.Row) -> AgentDefinition:
        try:
            skill_ids = json.loads(row["skill_ids"])
        except (TypeError, json.JSONDecodeError, KeyError):
            skill_ids = []
        if not isinstance(skill_ids, list):
            skill_ids = []

        try:
            builtin_capabilities = json.loads(row["builtin_capabilities"])
        except (TypeError, json.JSONDecodeError, KeyError):
            builtin_capabilities = []
        if not isinstance(builtin_capabilities, list):
            builtin_capabilities = []

        normalized_capabilities: list[str] = []
        filesystem_aliases = {"filesystem", "fs_list", "fs_read", "fs_write"}
        has_filesystem = any(str(item).strip() in filesystem_aliases for item in builtin_capabilities)
        if has_filesystem:
            normalized_capabilities.append("filesystem")

        return AgentDefinition(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            system_prompt=row["system_prompt"],
            model=row["model"],
            skill_ids=skill_ids,
            builtin_capabilities=normalized_capabilities,
        )

    def _row_to_skill(self, row: sqlite3.Row) -> SkillDefinition:
        columns = set(row.keys())
        return SkillDefinition(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            instruction=row["instruction"],
            source_provider=row["source_provider"] if "source_provider" in columns else None,
            source_skill_id=row["source_skill_id"] if "source_skill_id" in columns else None,
            local_path=row["local_path"] if "local_path" in columns else None,
        )

    def _sanitize_skill_dirname(self, text: str) -> str:
        normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
        compact = "-".join(part for part in normalized.split("-") if part)
        return compact[:72] if compact else _new_id("skillpkg")

    def _find_existing_skill_dir(self, skill_id: str) -> Path | None:
        legacy_dir = self.skills_root / skill_id
        if (legacy_dir / "skill.json").exists():
            return legacy_dir

        for root in self._iter_skill_roots():
            for skill_json in root.rglob("skill.json"):
                try:
                    payload = json.loads(skill_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict) and str(payload.get("id") or "").strip() == skill_id:
                    return skill_json.parent
        return None

    def _resolve_skill_dir(self, skill_id: str, name: str) -> Path:
        slug = self._sanitize_skill_dirname(name or skill_id)
        preferred = self.skills_root / f"{slug}__{skill_id}"

        existing = self._find_existing_skill_dir(skill_id)
        if existing is None:
            return preferred
        if existing == preferred:
            return existing

        is_legacy_name = existing.parent == self.skills_root and "__" not in existing.name
        if is_legacy_name and not preferred.exists():
            try:
                existing.rename(preferred)
                return preferred
            except OSError:
                return existing
        return existing

    def _safe_relpath(self, raw_path: str) -> Path | None:
        candidate = Path(raw_path.replace("\\", "/").strip())
        if candidate.is_absolute():
            return None
        clean_parts = []
        for part in candidate.parts:
            if part in ("", "."):
                continue
            if part == "..":
                return None
            clean_parts.append(part)
        if not clean_parts:
            return None
        return Path(*clean_parts)

    def _skill_file_path(self, skill_id: str, name: str) -> Path:
        return self._resolve_skill_dir(skill_id, name) / "skill.json"

    def _normalize_tool(self, tool: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(tool, dict):
            return None

        raw_command = tool.get("command")
        if not isinstance(raw_command, list):
            return None
        command = [str(part).strip() for part in raw_command if str(part).strip()]
        if not command:
            return None

        input_schema = tool.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {
                "type": "object",
                "properties": {},
            }

        normalized: dict[str, Any] = {
            "name": str(tool.get("name") or "tool").strip() or "tool",
            "description": str(tool.get("description") or "").strip(),
            "input_schema": input_schema,
            "command": command,
            "timeout_seconds": int(tool.get("timeout_seconds") or 20),
        }
        input_mode = tool.get("input_mode")
        if isinstance(input_mode, str) and input_mode.strip():
            normalized["input_mode"] = input_mode.strip()
        default_output_dir = tool.get("default_output_dir")
        if isinstance(default_output_dir, str) and default_output_dir.strip():
            normalized["default_output_dir"] = default_output_dir.strip()
        return normalized

    def _read_existing_tool(self, skill_dir: Path) -> dict[str, Any] | None:
        skill_json = skill_dir / "skill.json"
        if not skill_json.exists():
            return None
        try:
            payload = json.loads(skill_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        tool = self._normalize_tool(payload.get("tool"))
        if tool is None:
            return None
        if self._is_legacy_stub_tool(skill_dir, tool):
            return None
        return tool

    def _is_legacy_stub_tool(self, skill_dir: Path, tool: dict[str, Any]) -> bool:
        command = tool.get("command")
        if not isinstance(command, list):
            return False
        normalized = [str(part).strip().lower() for part in command]
        if normalized != ["python", "tool.py"]:
            return False

        script_path = skill_dir / "tool.py"
        if not script_path.exists() or not script_path.is_file():
            return False
        try:
            content = script_path.read_text(encoding="utf-8")
        except OSError:
            return False
        markers = (
            "api.duckduckgo.com",
            "RelatedTopics",
            "Missing query",
        )
        return all(marker in content for marker in markers)

    def _write_skill_package_file(
        self,
        *,
        skill_id: str,
        name: str,
        description: str,
        instruction: str,
        source_provider: str | None,
        source_skill_id: str | None,
        local_path: str | None = None,
        tool: dict[str, Any] | None = None,
        package_files: dict[str, str] | None = None,
    ) -> None:
        skill_dir = self._resolve_skill_dir(skill_id, name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(package_files, dict):
            for raw_path, content in package_files.items():
                if not isinstance(raw_path, str) or not isinstance(content, str):
                    continue
                safe_path = self._safe_relpath(raw_path)
                if safe_path is None:
                    continue
                if safe_path.as_posix().lower() == "skill.json":
                    continue
                target = skill_dir / safe_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            skill_md.write_text(
                (
                    f"# {name[:80]}\n\n"
                    f"{description[:200]}\n\n"
                    "## Instruction\n\n"
                    f"{instruction}\n"
                ),
                encoding="utf-8",
            )

        payload: dict[str, Any] = {
            "id": skill_id,
            "name": name[:80],
            "description": description[:200],
            "instruction": instruction,
            "source_provider": source_provider,
            "source_skill_id": source_skill_id,
            "local_path": local_path or str(skill_dir),
        }
        normalized_tool = self._normalize_tool(tool)
        if normalized_tool is None:
            normalized_tool = self._read_existing_tool(skill_dir)
            if self._is_legacy_stub_tool(skill_dir, {"command": ["python", "tool.py"]}):
                try:
                    (skill_dir / "tool.py").unlink()
                except OSError:
                    pass
        if normalized_tool is not None:
            payload["tool"] = normalized_tool

        skill_json = self._skill_file_path(skill_id, name)
        skill_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_file_skill(self, skill_json: Path) -> SkillDefinition | None:
        try:
            payload = json.loads(skill_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        skill_id = str(payload.get("id") or "").strip()
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        instruction = str(payload.get("instruction") or "").strip()
        if not (skill_id and name and description and instruction):
            return None

        tool = payload.get("tool")
        if not isinstance(tool, dict):
            tool = None

        raw_provider = payload.get("source_provider")
        source_provider = (
            str(raw_provider).strip()
            if raw_provider not in (None, "")
            else None
        )
        raw_source_skill = payload.get("source_skill_id")
        source_skill_id = (
            str(raw_source_skill).strip()
            if raw_source_skill not in (None, "")
            else None
        )
        raw_local_path = payload.get("local_path")
        local_path = (
            str(raw_local_path).strip()
            if raw_local_path not in (None, "")
            else str(skill_json.parent)
        )

        return SkillDefinition(
            id=skill_id,
            name=name[:80],
            description=description[:200],
            instruction=instruction,
            source_provider=source_provider,
            source_skill_id=source_skill_id,
            tool=tool,
            local_path=local_path,
        )

    def _parse_skill_frontmatter(self, markdown_text: str) -> tuple[str | None, str | None]:
        text = markdown_text.lstrip("\ufeff")
        if not text.startswith("---"):
            return None, None

        marker = "\n---"
        end = text.find(marker, 3)
        if end < 0:
            return None, None

        frontmatter = text[3:end].strip("\r\n")
        name: str | None = None
        description: str | None = None

        lines = frontmatter.splitlines()
        index = 0
        while index < len(lines):
            raw_line = lines[index]
            line = raw_line.strip()
            if not line or ":" not in line:
                index += 1
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()

            if key == "name":
                if value:
                    name = value.strip("'\"")
                index += 1
                continue

            if key == "description":
                if value in {"|", ">", "|-", ">-"}:
                    block: list[str] = []
                    index += 1
                    while index < len(lines):
                        next_line = lines[index]
                        if not next_line.startswith(" ") and ":" in next_line:
                            break
                        block.append(next_line.strip())
                        index += 1
                    merged = " ".join(part for part in block if part).strip()
                    if merged:
                        description = merged
                    continue
                if value:
                    description = value.strip("'\"")
            index += 1

        return name, description

    def _read_markdown_skill(self, skill_md: Path) -> SkillDefinition | None:
        skill_dir = skill_md.parent
        if (skill_dir / "skill.json").exists():
            return None

        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            return None

        parsed_name, parsed_description = self._parse_skill_frontmatter(content)

        skill_root = self._resolve_skill_root_for_path(skill_dir)
        relative = skill_dir.relative_to(skill_root).as_posix()
        stable_hash = hashlib.sha1(relative.encode("utf-8")).hexdigest()[:12]
        skill_id = f"local_{stable_hash}"

        folder_name = skill_dir.name.replace("_", " ").strip()
        inferred_name = re.sub(r"\s+", "-", folder_name).strip("-").lower() or f"local-skill-{stable_hash[:6]}"
        name = (parsed_name or inferred_name)[:80]

        description = (parsed_description or f"Local skill from {relative}")[:200]
        instruction = content.strip()
        if not instruction:
            return None
        tool = self._infer_local_tool(
            skill_dir=skill_dir,
            skill_id=skill_id,
            skill_name=name,
            description=description,
        )

        return SkillDefinition(
            id=skill_id,
            name=name,
            description=description,
            instruction=instruction,
            source_provider="local",
            source_skill_id=relative,
            tool=tool,
            local_path=str(skill_dir),
        )

    def _infer_local_tool(
        self,
        *,
        skill_dir: Path,
        skill_id: str,
        skill_name: str,
        description: str,
    ) -> dict[str, Any] | None:
        package_json = skill_dir / "package.json"
        package_payload: dict[str, Any] | None = None
        if package_json.exists():
            try:
                raw = json.loads(package_json.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    package_payload = raw
            except (OSError, json.JSONDecodeError):
                package_payload = None

        candidates: list[Path] = [
            skill_dir / "scripts" / "generate-v2.js",
            skill_dir / "scripts" / "generate-v2-demo.js",
            skill_dir / "scripts" / "generate.js",
            skill_dir / "scripts" / "search.sh",
            skill_dir / "scripts" / "run.js",
            skill_dir / "scripts" / "run.py",
            skill_dir / "scripts" / "run.sh",
            skill_dir / "run.js",
            skill_dir / "run.py",
            skill_dir / "run.sh",
        ]
        if package_payload:
            main_entry = package_payload.get("main")
            if isinstance(main_entry, str) and main_entry.strip():
                main_path = skill_dir / main_entry.strip()
                if main_path not in candidates:
                    candidates.insert(0, main_path)

        entry = next((path for path in candidates if path.exists() and path.is_file()), None)
        if entry is None:
            return None

        try:
            rel_entry = entry.relative_to(skill_dir).as_posix()
        except ValueError:
            rel_entry = entry.name

        suffix = entry.suffix.lower()
        if suffix in {".js", ".mjs", ".cjs"}:
            command = ["node", rel_entry]
            input_mode = "argv_content"
            input_schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Main content or request text for this skill.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title for generated cards/documents.",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Optional absolute or relative output directory.",
                    },
                    "single": {
                        "type": "boolean",
                        "description": "Optional single mode when supported by script.",
                    },
                    "with_images": {
                        "type": "boolean",
                        "description": "Optional image-generation mode when supported by script.",
                    },
                    "cards": {
                        "type": "integer",
                        "description": "Optional card count for generator scripts.",
                    },
                },
                "required": ["content"],
            }
        elif suffix == ".py":
            command = ["python", rel_entry]
            input_mode = "stdin_json"
            input_schema = {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Main content or request text for this skill.",
                    },
                },
                "required": ["content"],
            }
        elif suffix == ".sh":
            command = ["bash", rel_entry]
            input_mode = "argv_json"
            input_schema = {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query text.",
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Optional time range: day/week/month/year.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Optional max result count.",
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional domain allowlist.",
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional domain denylist.",
                    },
                    "search_depth": {
                        "type": "string",
                        "description": "Optional depth: ultra-fast/fast/basic/advanced.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Fallback content; mapped to query if query is absent.",
                    },
                },
                "required": ["query"],
            }
        else:
            return None

        default_output_dir = str((self.app_home / "generated" / skill_id).resolve())
        return {
            "name": f"{skill_name}-tool",
            "description": description or f"Run local skill {skill_name}",
            "input_schema": input_schema,
            "command": command,
            "timeout_seconds": 240,
            "input_mode": input_mode,
            "default_output_dir": default_output_dir,
        }

    def _load_file_skills(self) -> dict[str, SkillDefinition]:
        loaded: dict[str, SkillDefinition] = {}
        identity_owner: dict[str, str] = {}

        def register_skill(skill: SkillDefinition) -> None:
            if skill.id in loaded:
                loaded[skill.id] = skill
                return

            for identity_key in self._skill_identity_keys(skill):
                owner_id = identity_owner.get(identity_key)
                if owner_id and owner_id in loaded:
                    return

            loaded[skill.id] = skill
            for identity_key in self._skill_identity_keys(skill):
                identity_owner[identity_key] = skill.id

        for root in self._iter_skill_roots():
            for skill_json in root.rglob("skill.json"):
                skill = self._read_file_skill(skill_json)
                if skill is None:
                    continue
                register_skill(skill)
            for skill_md in root.rglob("SKILL.md"):
                markdown_skill = self._read_markdown_skill(skill_md)
                if markdown_skill is None:
                    continue
                register_skill(markdown_skill)
        return loaded

    def _normalize_skill_ref(self, value: str) -> str:
        return str(value).strip().replace("\\", "/").strip().lower()

    def _normalize_skill_local_path(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return str(Path(raw).resolve()).replace("\\", "/").strip().lower()
        except OSError:
            return raw.replace("\\", "/").strip().lower()

    def _find_existing_skill_record(
        self,
        connection: sqlite3.Connection,
        *,
        source_provider: str | None = None,
        source_skill_id: str | None = None,
        local_path: str | None = None,
    ) -> sqlite3.Row | None:
        provider = str(source_provider or "").strip()
        source_id = str(source_skill_id or "").strip()
        normalized_path = self._normalize_skill_local_path(local_path or "")

        if provider and source_id:
            row = connection.execute(
                """
                SELECT id, name, description, instruction, source_provider, source_skill_id, local_path
                FROM skills
                WHERE source_provider = ? AND source_skill_id = ?
                """,
                (provider, source_id),
            ).fetchone()
            if row is not None:
                return row

        if normalized_path:
            rows = connection.execute(
                """
                SELECT id, name, description, instruction, source_provider, source_skill_id, local_path
                FROM skills
                WHERE local_path IS NOT NULL
                """
            ).fetchall()
            for row in rows:
                if self._normalize_skill_local_path(str(row["local_path"] or "")) == normalized_path:
                    return row
        return None

    def _upsert_skill_record(
        self,
        connection: sqlite3.Connection,
        *,
        name: str,
        description: str,
        instruction: str,
        source_provider: str | None,
        source_skill_id: str | None,
        local_path: str | None,
    ) -> str:
        existing = self._find_existing_skill_record(
            connection,
            source_provider=source_provider,
            source_skill_id=source_skill_id,
            local_path=local_path,
        )
        normalized_path = str(local_path or "").strip() or None
        if existing is not None:
            connection.execute(
                """
                UPDATE skills
                SET name = ?, description = ?, instruction = ?, source_provider = ?, source_skill_id = ?, local_path = ?
                WHERE id = ?
                """,
                (
                    name[:80],
                    description[:200],
                    instruction,
                    source_provider,
                    source_skill_id,
                    normalized_path,
                    existing["id"],
                ),
            )
            return str(existing["id"])

        skill_id = _new_id("skill")
        connection.execute(
            """
            INSERT INTO skills (
                id, name, description, instruction, source_provider, source_skill_id, local_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                name[:80],
                description[:200],
                instruction,
                source_provider,
                source_skill_id,
                normalized_path,
            ),
        )
        return skill_id

    def _skill_identity_keys(self, skill: SkillDefinition) -> set[str]:
        keys: set[str] = set()
        source_provider = str(skill.source_provider or "").strip().lower()
        source_skill_id = self._normalize_skill_ref(str(skill.source_skill_id or ""))
        if source_provider and source_skill_id:
            keys.add(f"source:{source_provider}:{source_skill_id}")

        local_path = self._normalize_skill_local_path(str(skill.local_path or ""))
        if local_path:
            keys.add(f"path:{local_path}")
        return keys

    def _skill_aliases(self, skill: SkillDefinition) -> set[str]:
        aliases: set[str] = set()
        aliases.add(self._normalize_skill_ref(skill.id))
        aliases.add(self._normalize_skill_ref(skill.name))

        if skill.source_skill_id:
            normalized_source = self._normalize_skill_ref(skill.source_skill_id)
            aliases.add(normalized_source)
            aliases.add(self._normalize_skill_ref(Path(normalized_source).name))

        local_path = str(skill.local_path or "").strip()
        if local_path:
            local_norm = self._normalize_skill_ref(local_path)
            aliases.add(local_norm)
            local_name = Path(local_path).name
            if local_name:
                aliases.add(self._normalize_skill_ref(local_name))
            for root in self._iter_skill_roots():
                try:
                    relative = Path(local_path).resolve().relative_to(root.resolve())
                    relative_norm = self._normalize_skill_ref(relative.as_posix())
                    aliases.add(relative_norm)
                    aliases.add(self._normalize_skill_ref(relative.name))
                    break
                except (OSError, ValueError):
                    continue

        return {alias for alias in aliases if alias}

    def _resolve_file_skill(
        self,
        skill_ref: str,
        file_skills: dict[str, SkillDefinition],
    ) -> SkillDefinition | None:
        raw = str(skill_ref or "").strip()
        if not raw:
            return None
        direct = file_skills.get(raw)
        if direct is not None:
            return direct

        normalized_ref = self._normalize_skill_ref(raw)
        for skill in file_skills.values():
            if normalized_ref in self._skill_aliases(skill):
                return skill
        return None

    def _migrate_and_clear_db_skills(self) -> None:
        with self._connect() as connection:
            skill_rows = connection.execute(
                """
                SELECT id, name, source_skill_id
                FROM skills
                """
            ).fetchall()

            if not skill_rows:
                return

            replacement_by_id: dict[str, str] = {}
            for row in skill_rows:
                skill_id = str(row["id"] or "").strip()
                if not skill_id:
                    continue
                source_skill_id = str(row["source_skill_id"] or "").strip()
                name = str(row["name"] or "").strip()
                replacement = source_skill_id or name
                if replacement:
                    replacement_by_id[skill_id] = replacement

            agent_rows = connection.execute(
                """
                SELECT id, skill_ids
                FROM agents
                """
            ).fetchall()
            for row in agent_rows:
                agent_id = str(row["id"] or "").strip()
                if not agent_id:
                    continue
                try:
                    raw_refs = json.loads(row["skill_ids"])
                except (TypeError, json.JSONDecodeError):
                    raw_refs = []
                if not isinstance(raw_refs, list):
                    raw_refs = []

                changed = False
                migrated_refs: list[str] = []
                seen: set[str] = set()
                for ref in raw_refs:
                    ref_text = str(ref or "").strip()
                    if not ref_text:
                        continue
                    migrated = replacement_by_id.get(ref_text, ref_text)
                    if migrated != ref_text:
                        changed = True
                    if migrated not in seen:
                        seen.add(migrated)
                        migrated_refs.append(migrated)

                if changed:
                    connection.execute(
                        """
                        UPDATE agents
                        SET skill_ids = ?
                        WHERE id = ?
                        """,
                        (json.dumps(migrated_refs), agent_id),
                    )

            connection.execute("DELETE FROM skills")

    def _row_to_workflow(self, row: sqlite3.Row) -> WorkflowDefinition:
        try:
            specialist_agent_ids = json.loads(row["specialist_agent_ids"])
        except (TypeError, json.JSONDecodeError):
            specialist_agent_ids = []

        if not isinstance(specialist_agent_ids, list):
            specialist_agent_ids = []

        return WorkflowDefinition(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            specialist_agent_ids=specialist_agent_ids,
            router_prompt=row["router_prompt"],
            finalizer_enabled=bool(row["finalizer_enabled"]),
        )

    def list_agents(self) -> list[AgentDefinition]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, description, system_prompt, model, skill_ids, builtin_capabilities
                FROM agents
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return [self._row_to_agent(row) for row in rows]

    def get_agent(self, agent_id: str) -> AgentDefinition | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, description, system_prompt, model, skill_ids, builtin_capabilities
                FROM agents
                WHERE id = ?
                """,
                (agent_id,),
            ).fetchone()
        return self._row_to_agent(row) if row else None

    def create_agent(self, payload: AgentDefinitionCreate) -> AgentDefinition:
        agent = AgentDefinition(id=_new_id("agent"), **payload.model_dump())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agents (
                    id, name, description, system_prompt, model, skill_ids, builtin_capabilities
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent.id,
                    agent.name,
                    agent.description,
                    agent.system_prompt,
                    agent.model,
                    json.dumps(agent.skill_ids),
                    json.dumps(agent.builtin_capabilities),
                ),
            )
        return agent

    def update_agent(self, agent_id: str, payload: AgentDefinitionUpdate) -> AgentDefinition | None:
        if self.get_agent(agent_id) is None:
            return None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agents
                SET name = ?, description = ?, system_prompt = ?, model = ?, skill_ids = ?, builtin_capabilities = ?
                WHERE id = ?
                """,
                (
                    payload.name,
                    payload.description,
                    payload.system_prompt,
                    payload.model,
                    json.dumps(payload.skill_ids),
                    json.dumps(payload.builtin_capabilities),
                    agent_id,
                ),
            )
        return self.get_agent(agent_id)

    def agent_usage_workflows(self, agent_id: str) -> list[WorkflowDefinition]:
        workflows = self.list_workflows()
        return [
            workflow
            for workflow in workflows
            if agent_id in (workflow.specialist_agent_ids or [])
        ]

    def delete_agent(self, agent_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM agents
                WHERE id = ?
                """,
                (agent_id,),
            )
            return cursor.rowcount > 0

    def list_skills(self) -> list[SkillDefinition]:
        file_skills = self._load_file_skills()
        return sorted(file_skills.values(), key=lambda item: (item.name.lower(), item.id))

    def get_skill(self, skill_id: str) -> SkillDefinition | None:
        file_skills = self._load_file_skills()
        return self._resolve_file_skill(skill_id, file_skills)

    def get_skills_by_ids(self, skill_ids: list[str]) -> list[SkillDefinition]:
        if not skill_ids:
            return []
        file_skills = self._load_file_skills()
        found: list[SkillDefinition] = []
        seen: set[str] = set()
        for skill_ref in skill_ids:
            skill = self._resolve_file_skill(skill_ref, file_skills)
            if skill is None:
                continue
            if skill.id in seen:
                continue
            seen.add(skill.id)
            found.append(skill)
        return found

    def create_skill(self, payload: SkillDefinitionCreate) -> SkillDefinition:
        with self._connect() as connection:
            skill_id = self._upsert_skill_record(
                connection,
                name=payload.name,
                description=payload.description,
                instruction=payload.instruction,
                source_provider=None,
                source_skill_id=None,
                local_path=None,
            )
        skill = SkillDefinition(id=skill_id, **payload.model_dump())
        self._write_skill_package_file(
            skill_id=skill.id,
            name=skill.name,
            description=skill.description,
            instruction=skill.instruction,
            source_provider=None,
            source_skill_id=None,
            local_path=None,
            tool=None,
        )
        return skill

    def upsert_marketplace_skills(
        self,
        source_provider: str,
        skills: list[dict[str, Any]],
    ) -> tuple[int, int]:
        imported = 0
        updated = 0

        with self._connect() as connection:
            for item in skills:
                source_skill_id = str(item.get("source_skill_id") or "").strip()
                name = str(item.get("name") or "").strip()
                description = str(item.get("description") or "").strip()
                instruction = str(item.get("instruction") or "").strip()
                tool = item.get("tool")
                if not isinstance(tool, dict):
                    tool = None
                package_files = item.get("package_files")
                if not isinstance(package_files, dict):
                    package_files = None

                if not (source_skill_id and name and description and instruction):
                    continue

                existing = self._find_existing_skill_record(
                    connection,
                    source_provider=source_provider,
                    source_skill_id=source_skill_id,
                    local_path=None,
                )
                skill_id = self._upsert_skill_record(
                    connection,
                    name=name,
                    description=description,
                    instruction=instruction,
                    source_provider=source_provider,
                    source_skill_id=source_skill_id,
                    local_path=None,
                )
                if existing:
                    self._write_skill_package_file(
                        skill_id=skill_id,
                        name=name,
                        description=description,
                        instruction=instruction,
                        source_provider=source_provider,
                        source_skill_id=source_skill_id,
                        local_path=None,
                        tool=tool,
                        package_files=package_files,
                    )
                    updated += 1
                    continue
                self._write_skill_package_file(
                    skill_id=skill_id,
                    name=name,
                    description=description,
                    instruction=instruction,
                    source_provider=source_provider,
                    source_skill_id=source_skill_id,
                    local_path=None,
                    tool=tool,
                    package_files=package_files,
                )
                imported += 1

        return imported, updated

    def install_skill_package(
        self,
        skill_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        instruction: str | None = None,
        tool: dict[str, Any] | None = None,
        package_files: dict[str, str] | None = None,
    ) -> SkillDefinition | None:
        existing = self.get_skill(skill_id)
        if existing is None:
            return None

        next_name = str(name or existing.name).strip() or existing.name
        next_description = str(description or existing.description).strip() or existing.description
        next_instruction = str(instruction or existing.instruction).strip() or existing.instruction

        with self._connect() as connection:
            canonical_skill_id = self._upsert_skill_record(
                connection,
                name=next_name,
                description=next_description,
                instruction=next_instruction,
                source_provider=existing.source_provider,
                source_skill_id=existing.source_skill_id,
                local_path=existing.local_path,
            )

        self._write_skill_package_file(
            skill_id=canonical_skill_id,
            name=next_name,
            description=next_description,
            instruction=next_instruction,
            source_provider=existing.source_provider,
            source_skill_id=existing.source_skill_id,
            local_path=existing.local_path,
            tool=tool,
            package_files=package_files,
        )
        return self.get_skill(canonical_skill_id)

    def set_agent_skill_ids(self, agent_id: str, skill_ids: list[str]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agents
                SET skill_ids = ?
                WHERE id = ?
                """,
                (json.dumps(skill_ids), agent_id),
            )

    def _materialize_db_skills_to_files(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, description, instruction, source_provider, source_skill_id
                FROM skills
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        for row in rows:
            skill = self._row_to_skill(row)
            self._write_skill_package_file(
                skill_id=skill.id,
                name=skill.name,
                description=skill.description,
                instruction=skill.instruction,
                source_provider=skill.source_provider,
                source_skill_id=skill.source_skill_id,
                tool=skill.tool,
            )

    def list_workflows(self) -> list[WorkflowDefinition]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, type, specialist_agent_ids, router_prompt, finalizer_enabled
                FROM workflows
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [self._row_to_workflow(row) for row in rows]

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, type, specialist_agent_ids, router_prompt, finalizer_enabled
                FROM workflows
                WHERE id = ?
                """,
                (workflow_id,),
            ).fetchone()
        return self._row_to_workflow(row) if row else None

    def create_workflow(self, payload: WorkflowDefinitionCreate) -> WorkflowDefinition:
        workflow = WorkflowDefinition(id=_new_id("workflow"), **payload.model_dump())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflows (
                    id,
                    name,
                    type,
                    specialist_agent_ids,
                    router_prompt,
                    finalizer_enabled
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.name,
                    workflow.type,
                    json.dumps(workflow.specialist_agent_ids),
                    workflow.router_prompt,
                    1 if workflow.finalizer_enabled else 0,
                ),
            )
        return workflow

    def update_workflow(
        self,
        workflow_id: str,
        payload: WorkflowDefinitionUpdate,
    ) -> WorkflowDefinition | None:
        if self.get_workflow(workflow_id) is None:
            return None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows
                SET name = ?, type = ?, specialist_agent_ids = ?, router_prompt = ?, finalizer_enabled = ?
                WHERE id = ?
                """,
                (
                    payload.name,
                    payload.type,
                    json.dumps(payload.specialist_agent_ids),
                    payload.router_prompt,
                    1 if payload.finalizer_enabled else 0,
                    workflow_id,
                ),
            )
        return self.get_workflow(workflow_id)

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._connect() as connection:
            conversation_rows = connection.execute(
                """
                SELECT id
                FROM conversations
                WHERE workflow_id = ?
                """,
                (workflow_id,),
            ).fetchall()
            conversation_ids = [str(row["id"]) for row in conversation_rows if str(row["id"] or "").strip()]
            for conversation_id in conversation_ids:
                connection.execute(
                    """
                    DELETE FROM messages
                    WHERE conversation_id = ?
                    """,
                    (conversation_id,),
                )
            if conversation_ids:
                connection.execute(
                    f"""
                    DELETE FROM conversations
                    WHERE id IN ({",".join("?" for _ in conversation_ids)})
                    """,
                    conversation_ids,
                )
            cursor = connection.execute(
                """
                DELETE FROM workflows
                WHERE id = ?
                """,
                (workflow_id,),
            )
            return cursor.rowcount > 0

    def get_templates(self) -> list[WorkflowTemplate]:
        return [
            WorkflowTemplate(
                type="router_specialists",
                label="Router Specialists",
                description=(
                    "Router first selects the best specialist for the user intent, "
                    "then optionally passes through a finalizer."
                ),
                required_agent_count=2,
            ),
            WorkflowTemplate(
                type="planner_executor",
                label="Planner Executor",
                description=(
                    "Planner decomposes the request into sub-tasks, delegates each task "
                    "to workers, then synthesizes a final answer."
                ),
                required_agent_count=2,
            ),
            WorkflowTemplate(
                type="supervisor_dynamic",
                label="Supervisor Dynamic",
                description=(
                    "Supervisor decides delegation at runtime, loops through workers as needed, "
                    "and composes the final answer."
                ),
                required_agent_count=2,
            ),
            WorkflowTemplate(
                type="single_agent_chat",
                label="Single Agent Chat",
                description=(
                    "Direct chat with one selected agent. Graph is start -> agent -> end "
                    "(optional finalizer if enabled)."
                ),
                required_agent_count=1,
            ),
            WorkflowTemplate(
                type="peer_handoff",
                label="Peer Handoff",
                description=(
                    "Router chooses the first owner, then specialists can hand work to each other "
                    "through structured peer actions until the workflow converges."
                ),
                required_agent_count=2,
            ),
        ]

    # ============ Conversation CRUD ============

    def _row_to_conversation(self, row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            workflow_id=row["workflow_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_message(self, row: sqlite3.Row) -> Message:
        return Message(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            content=row["content"],
            agent_name=row["agent_name"],
            created_at=row["created_at"],
        )

    def list_conversations(self, workflow_id: str | None = None) -> list[Conversation]:
        with self._connect() as connection:
            if workflow_id:
                rows = connection.execute(
                    """
                    SELECT id, workflow_id, title, created_at, updated_at
                    FROM conversations
                    WHERE workflow_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (workflow_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, workflow_id, title, created_at, updated_at
                    FROM conversations
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [self._row_to_conversation(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, workflow_id, title, created_at, updated_at
                FROM conversations
                WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
        return self._row_to_conversation(row) if row else None

    def get_conversation_with_messages(self, conversation_id: str) -> ConversationDetail | None:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        messages = self.list_messages(conversation_id)
        return ConversationDetail(
            id=conversation.id,
            workflow_id=conversation.workflow_id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            messages=messages,
        )

    def create_conversation(self, payload: ConversationCreate) -> Conversation:
        conversation_id = _new_id("conv")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations (id, workflow_id, title, created_at, updated_at)
                VALUES (?, ?, NULL, ?, ?)
                """,
                (conversation_id, payload.workflow_id, now, now),
            )
        return Conversation(
            id=conversation_id,
            workflow_id=payload.workflow_id,
            title=None,
            created_at=now,
            updated_at=now,
        )

    def update_conversation_title(self, conversation_id: str, title: str) -> Conversation | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, now, conversation_id),
            )
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM messages WHERE conversation_id = ?
                """,
                (conversation_id,),
            )
            cursor = connection.execute(
                """
                DELETE FROM conversations WHERE id = ?
                """,
                (conversation_id,),
            )
            return cursor.rowcount > 0

    # ============ Message CRUD ============

    def list_messages(self, conversation_id: str, limit: int | None = None) -> list[Message]:
        with self._connect() as connection:
            if limit:
                rows = connection.execute(
                    """
                    SELECT id, conversation_id, role, content, agent_name, created_at
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (conversation_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, conversation_id, role, content, agent_name, created_at
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC
                    """,
                    (conversation_id,),
                ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def create_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        agent_name: str | None = None,
    ) -> Message:
        message_id = _new_id("msg")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (id, conversation_id, role, content, agent_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, conversation_id, role, content, agent_name, now),
            )
            connection.execute(
                """
                UPDATE conversations SET updated_at = ? WHERE id = ?
                """,
                (now, conversation_id),
            )
        return Message(
            id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            agent_name=agent_name,
            created_at=now,
        )

    def get_recent_messages(self, conversation_id: str, limit: int = 2) -> list[Message]:
        """获取最近N条消息（用于上下文）"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, role, content, agent_name, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        messages = [self._row_to_message(row) for row in rows]
        messages.reverse()  # 按时间正序返回
        return messages

    def seed_defaults(self) -> None:
        self._rename_default_workflow_names()

        skills = self.list_skills()
        skill_id_by_name = {skill.name: skill.id for skill in skills}

        default_agent_specs = [
            {
                "name": "产品经理",
                "description": "负责需求澄清、优先级判断、范围控制与验收标准定义。",
                "system_prompt": (
                    "你是软件产品团队中的“产品经理”。\n"
                    "职责：\n"
                    "1) 澄清用户目标、约束、业务背景与优先级；\n"
                    "2) 将模糊需求整理成明确的问题陈述、范围边界与验收标准；\n"
                    "3) 产出面向设计与研发的任务拆解与交付建议。\n"
                    "输出规则：\n"
                    "- 结构化表达，明确假设；\n"
                    "- 优先关注目标、用户价值、约束、优先级和完成标准；\n"
                    "- 你的目标不是解释应该怎么做，而是直接把当前职责范围内必须明确的需求、范围、约束、验收标准补齐到可继续执行的程度；\n"
                    "- 如果当前信息足以完成需求澄清与验收定义，就直接完成，不要只给方向、建议或下一步计划；\n"
                    "- 如果用户请求最终要交付可用产品、页面、工具或代码，请明确写出“完成到什么程度才算完成”，不要只给方向性建议；\n"
                    "- 对需求拆解时，必须覆盖用户请求中的所有关键部分，不要遗漏功能、交互、交付位置、文档或验收要求；\n"
                    "- 非必要不直接下沉到实现代码。"
                ),
                "skill_names": ["Structured Reasoning"],
            },
            {
                "name": "设计师",
                "description": "负责信息架构、交互流程、页面结构与视觉方向建议。",
                "system_prompt": (
                    "你是产品团队中的“设计师”。\n"
                    "职责：\n"
                    "1) 将需求转成清晰的信息架构、页面结构与用户流程；\n"
                    "2) 提供交互方案、界面层级、状态设计与视觉方向建议；\n"
                    "3) 输出便于工程实现的设计说明。\n"
                    "输出规则：\n"
                    "- 优先说明页面结构、用户路径、交互状态和异常状态；\n"
                    "- 兼顾可用性、一致性与实现可行性；\n"
                    "- 你的目标不是解释设计思路或建议工程后续再补，而是把当前职责范围内应明确的页面结构、交互行为、状态变化和视觉决策直接定清楚；\n"
                    "- 如果当前信息足以完成设计说明，就直接输出完整设计结论，不要只写概念方向、审美描述或待细化项；\n"
                    "- 不空谈风格，尽量给具体界面建议；\n"
                    "- 如果任务涉及可用产品、页面或工具，设计说明必须覆盖用户可见行为，而不仅是静态视觉样式；\n"
                    "- 输出给工程时，要让工程明确知道哪些行为必须存在才能算设计落地完成。"
                ),
                "skill_names": ["Structured Reasoning", "Teaching Mode"],
            },
            {
                "name": "工程师",
                "description": "负责技术方案、代码实现路径、风险提示与交付落地。",
                "system_prompt": (
                    "你是产品团队中的“工程师”。\n"
                    "职责：\n"
                    "1) 将需求与设计方案转化为实际可运行、可交付的实现；\n"
                    "2) 提供代码级实现、架构取舍、落地步骤与风险提示；\n"
                    "3) 说明实现边界、依赖、测试与交付方式。\n"
                    "输出规则：\n"
                    "- 你的首要目标不是解释怎么实现，而是把当前任务要求的可交付结果直接实现出来；\n"
                    "- 如果当前信息足以完成实现，就一次性做完，不要把可继续完成的工作留给“后续补齐”“下一步”或“待实现”；\n"
                    "- 优先交付务实、最小可行但可实际使用的实现结果，而不是停留在建议、骨架或占位代码；\n"
                    "- 如果任务要求交付产品、页面、应用、工具、脚本或功能，默认目标是一次性做完当前任务所要求的完整可用行为，不要主动留下“后续补齐”“待实现”“仅样式完成”这类遗留项；\n"
                    "- 只有在真实受阻、缺少必要信息或明确不属于当前任务时，才保留未完成项，并明确说明原因；\n"
                    "- 不要把静态界面、视觉壳子、未接线的结构、占位逻辑或半成品当作完成；\n"
                    "- 指出技术风险、复杂度与前置条件；\n"
                    "- 需要时补充代码、命令或文件级说明。"
                ),
                "skill_names": ["Structured Reasoning", "Risk Review"],
            },
        ]

        def resolve_skill_ids(skill_names: list[str]) -> list[str]:
            return [skill_id_by_name[name] for name in skill_names if skill_id_by_name.get(name)]

        agents = self.list_agents()
        legacy_to_new = {
            "Architecture Coach": 0,
            "Documentation Writer": 1,
            "Learning Coach": 2,
            "Solution Architect": 0,
            "Implementation Engineer": 1,
            "QA & Risk Reviewer": 2,
            "解决方案架构师": 0,
            "实施工程师": 1,
            "质量与风险审查员": 2,
        }
        legacy_prompt_markers = {
            "Architecture Coach": "You are an architecture specialist agent.",
            "Documentation Writer": "You are a documentation specialist agent.",
            "Learning Coach": "You are a learning coach specialist agent.",
            "Solution Architect": "You are the Solution Architect in a software delivery team.",
            "Implementation Engineer": "You are the Implementation Engineer.",
            "QA & Risk Reviewer": "You are the QA and Risk Reviewer.",
            "解决方案架构师": "你是软件交付团队中的“解决方案架构师”。",
            "实施工程师": "你是“实施工程师”。",
            "质量与风险审查员": "你是“质量与风险审查员”。",
        }
        for agent in agents:
            spec_index = legacy_to_new.get(agent.name)
            if spec_index is None:
                continue
            marker = legacy_prompt_markers.get(agent.name, "")
            if marker and marker not in (agent.system_prompt or ""):
                continue
            spec = default_agent_specs[spec_index]
            desired_skill_ids = agent.skill_ids or resolve_skill_ids(spec["skill_names"])
            self.update_agent(
                agent.id,
                AgentDefinitionUpdate(
                    name=spec["name"],
                    description=spec["description"],
                    system_prompt=spec["system_prompt"],
                    model=agent.model,
                    skill_ids=desired_skill_ids,
                    builtin_capabilities=agent.builtin_capabilities,
                ),
            )
        agents = self.list_agents()

        if not agents:
            for spec in default_agent_specs:
                self.create_agent(
                    AgentDefinitionCreate(
                        name=spec["name"],
                        description=spec["description"],
                        system_prompt=spec["system_prompt"],
                        skill_ids=resolve_skill_ids(spec["skill_names"]),
                        builtin_capabilities=["filesystem"],
                    )
                )
            agents = self.list_agents()

        spec_by_name = {spec["name"]: spec for spec in default_agent_specs}
        for agent in agents:
            spec = spec_by_name.get(agent.name)
            if not spec:
                continue
            desired = resolve_skill_ids(spec["skill_names"])
            if desired and not agent.skill_ids:
                self.set_agent_skill_ids(agent.id, desired)
        self._materialize_db_skills_to_files()
        self._migrate_and_clear_db_skills()


# Backward-compatible alias for existing imports.
InMemoryPlaygroundStore = SQLitePlaygroundStore


store = SQLitePlaygroundStore()
