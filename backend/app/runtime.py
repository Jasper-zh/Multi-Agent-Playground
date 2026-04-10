from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import shlex
import hashlib
import platform
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from dotenv import dotenv_values
from openai import OpenAI

from .schemas import AgentDefinition
from .settings_bridge import settings
from .store import store

ToolTraceHook = Callable[[dict[str, Any]], None]


class LLMGateway:
    _TOOL_BLOCKED_MARKER = "TOOL_EXECUTION_BLOCKED"
    _INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
        "search": (
            "search",
            "find",
            "lookup",
            "news",
            "web",
            "google",
            "bing",
            "tavily",
            "搜索",
            "查找",
            "检索",
            "新闻",
            "网页",
            "最新",
        ),
        "rednote": (
            "rednote",
            "xiaohongshu",
            "xhs",
            "card",
            "cards",
            "carousel",
            "html",
            "小红书",
            "卡片",
            "图文",
            "封面",
            "排版",
            "生成图片",
        ),
        "filesystem": (
            "file",
            "files",
            "folder",
            "directory",
            "path",
            "desktop",
            "download",
            "downloads",
            "read file",
            "list files",
            "文件",
            "文件夹",
            "目录",
            "路径",
            "桌面",
            "下载",
            "读取文件",
            "列出文件",
        ),
    }
    _ENV_IGNORE: set[str] = {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "PWD",
        "SHELL",
        "TERM",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NODE_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "NPM_CONFIG_PREFIX",
        "CI",
    }
    _EN_STOPWORDS: set[str] = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "about",
        "what",
        "when",
        "where",
        "which",
        "please",
        "need",
        "want",
        "help",
        "make",
        "create",
        "generate",
    }
    _ZH_STOPWORDS: set[str] = {
        "请问",
        "帮我",
        "一下",
        "这个",
        "那个",
        "怎么",
        "可以",
        "我要",
        "给我",
        "然后",
        "还有",
        "进行",
        "生成",
        "内容",
    }
    _INTENT_KEYWORDS_V2: dict[str, tuple[str, ...]] = {
        "search": (
            "search",
            "find",
            "lookup",
            "news",
            "web",
            "google",
            "bing",
            "tavily",
            "搜索",
            "查找",
            "检索",
            "新闻",
            "网页",
            "最新",
        ),
        "rednote": (
            "rednote",
            "xiaohongshu",
            "xhs",
            "card",
            "cards",
            "carousel",
            "html",
            "小红书",
            "卡片",
            "图文",
            "封面",
            "排版",
            "生成图片",
        ),
        "filesystem": (
            "file",
            "files",
            "folder",
            "directory",
            "path",
            "desktop",
            "download",
            "downloads",
            "read file",
            "list files",
            "文件",
            "文件夹",
            "目录",
            "路径",
            "桌面",
            "下载",
            "读取文件",
            "列出文件",
        ),
    }

    def __init__(self) -> None:
        self._prepared_node_dirs: set[str] = set()
        self._prepared_python_dirs: set[str] = set()
        self._tool_env_cache: dict[str, list[str]] = {}
        self._shell_deps_cache: dict[str, list[str]] = {}
        self._runtime_root = Path(settings.APP_HOME).resolve() / ".runtime"
        self._runtime_root.mkdir(parents=True, exist_ok=True)
        self.client = None
        self.api_configured = False
        self.refresh_client()

    def refresh_client(self) -> None:
        self.api_configured = bool(settings.OPENAI_API_KEY)
        self.client = (
            OpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,
            )
            if self.api_configured
            else None
        )

    def route(self, user_input: str, agents: Iterable[AgentDefinition]) -> tuple[str, str]:
        agent_list = list(agents)
        if not agent_list:
            raise ValueError("router_specialists workflow 至少需要一个 specialist agent。")

        if not self.api_configured or self.client is None:
            return self._fallback_route(user_input, agent_list)

        catalog = "\n".join(
            f"- id={agent.id}; name={agent.name}; description={agent.description}"
            for agent in agent_list
        )
        prompt = (
            "你是 workflow router。请从下面的 specialist agent 中选出最适合处理用户请求的一个。\n"
            "只返回一行，格式必须是：agent_id|reason\n"
            f"可选 agent:\n{catalog}\n"
            f"用户请求：{user_input}"
        )
        response = self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        content = (response.choices[0].message.content or "").strip()
        parts = content.split("|", 1)
        agent_id = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else "模型未返回解释，使用默认解释。"

        selected = next((agent for agent in agent_list if agent.id == agent_id), None)
        if selected is None:
            return self._fallback_route(user_input, agent_list)
        return selected.id, reason

    def plan_tasks(
        self,
        user_input: str,
        max_tasks: int = 4,
        force_multi: bool = False,
    ) -> tuple[list[str], str]:
        if not self.api_configured or self.client is None:
            return self._fallback_plan_tasks(user_input, max_tasks=max_tasks), "rule"

        multi_hint = (
            "Prefer at least 2 tasks when the request includes multiple intents."
            if force_multi
            else "Use the minimum number of tasks needed."
        )
        prompt = (
            "You are a planning module.\n"
            f"Decompose the user request into at most {max_tasks} executable tasks.\n"
            f"{multi_hint}\n"
            "Return ONLY a JSON array of strings.\n"
            f"User request: {user_input}"
        )
        try:
            response = self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            content = (response.choices[0].message.content or "").strip()
            tasks = self._parse_task_list(content, max_tasks=max_tasks)
            if tasks:
                return tasks, "llm"
        except Exception:  # noqa: BLE001
            pass
        return self._fallback_plan_tasks(user_input, max_tasks=max_tasks), "rule"

    def supervisor_review_decision(
        self,
        *,
        user_input: str,
        reports: list[str],
        cycle: int,
        max_cycles: int,
    ) -> tuple[bool, str, str]:
        if cycle >= max_cycles:
            return False, "", "Reached max cycle limit."

        if not self.api_configured or self.client is None:
            return self._fallback_supervisor_review_decision(
                user_input=user_input,
                reports=reports,
                cycle=cycle,
                max_cycles=max_cycles,
            )

        recent_reports = reports[-3:] if reports else []
        report_block = "\n\n".join(recent_reports) if recent_reports else "(no reports yet)"
        prompt = (
            "You are a supervisor loop controller.\n"
            "Given the original user request and worker reports, decide whether to continue delegation.\n"
            "Respond with JSON only in this shape:\n"
            '{"continue": true/false, "next_focus_task": "<string>", "reason": "<string>"}\n'
            "Rules:\n"
            "- If current output is sufficient and coherent, set continue=false.\n"
            "- If key requirements are missing, set continue=true and provide the next focus task.\n"
            f"- Current cycle: {cycle}, max cycles: {max_cycles}.\n\n"
            f"User request:\n{user_input}\n\n"
            f"Recent reports:\n{report_block}\n"
        )
        try:
            response = self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            content = (response.choices[0].message.content or "").strip()
            parsed = self._parse_supervisor_decision(content)
            if parsed is not None:
                should_continue, next_focus_task, reason = parsed
                if should_continue and not next_focus_task.strip():
                    next_focus_task = "Refine missing constraints, risks, and acceptance criteria."
                return should_continue, next_focus_task, reason or "Supervisor decision from model."
        except Exception as error:  # noqa: BLE001
            fallback_continue, fallback_focus, fallback_reason = self._fallback_supervisor_review_decision(
                user_input=user_input,
                reports=reports,
                cycle=cycle,
                max_cycles=max_cycles,
            )
            return fallback_continue, fallback_focus, f"{fallback_reason} (fallback due to: {error})"

        return self._fallback_supervisor_review_decision(
            user_input=user_input,
            reports=reports,
            cycle=cycle,
            max_cycles=max_cycles,
        )

    def run_agent(
        self,
        agent: AgentDefinition,
        user_input: str,
        history: list[dict[str, str]] | None = None,
        trace_hook: ToolTraceHook | None = None,
    ) -> str:
        system_prompt = self._compose_system_prompt(agent)
        enabled_skills = store.get_skills_by_ids(agent.skill_ids)
        executable_tools = [
            *self._get_builtin_tools(agent),
            *self._get_executable_skills(enabled_skills),
        ]
        if not self.api_configured or self.client is None:
            return self._fallback_agent_response(agent, user_input, system_prompt)
        return self._run_agent_with_tools(
            agent=agent,
            user_input=user_input,
            system_prompt=system_prompt,
            executable_tools=executable_tools,
            history=history,
            trace_hook=trace_hook,
        )

    def finalize(self, user_input: str, agent: AgentDefinition, specialist_answer: str) -> str:
        if self._is_tool_blocked_response(specialist_answer):
            return specialist_answer
        if not self.api_configured or self.client is None:
            return (
                f"系统已将请求路由给 {agent.name}。\n"
                f"{agent.name} 的回答如下：\n{specialist_answer}"
            )

        prompt = (
            "你是 workflow finalizer。请根据用户原始请求和 specialist 的回答，"
            "输出最终对用户可见的答案，控制在 6 句话以内。\n"
            f"用户请求：{user_input}\n"
            f"specialist: {agent.name}\n"
            f"specialist 回复：{specialist_answer}"
        )
        response = self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
        return (response.choices[0].message.content or "").strip()

    def _is_tool_blocked_response(self, text: str) -> bool:
        normalized = str(text or "")
        return (
            self._TOOL_BLOCKED_MARKER in normalized
            or "response generation is blocked to avoid fabricated output" in normalized
        )

    def _fallback_route(
        self,
        user_input: str,
        agents: list[AgentDefinition],
    ) -> tuple[str, str]:
        text = user_input.lower()
        ranked = []
        for agent in agents:
            score = 0
            haystack = f"{agent.name} {agent.description} {agent.system_prompt}".lower()
            for keyword in ("架构", "architecture", "design", "边界", "模块"):
                if keyword in text and keyword in haystack:
                    score += 3
            for keyword in ("写", "总结", "文档", "改写", "说明"):
                if keyword in text and keyword in haystack:
                    score += 3
            for keyword in ("学习", "路径", "怎么学", "建议", "步骤"):
                if keyword in text and keyword in haystack:
                    score += 3
            ranked.append((score, agent))

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = ranked[0][1]
        return selected.id, "当前处于无 API Key 的演示模式，使用关键词路由。"

    def _compose_system_prompt(self, agent: AgentDefinition) -> str:
        skills = store.get_skills_by_ids(agent.skill_ids)
        builtins = [
            str(item).strip()
            for item in (getattr(agent, "builtin_capabilities", None) or [])
            if str(item).strip()
        ]

        sections = [agent.system_prompt]

        if builtins:
            sections.append(
                "Enabled built-in capabilities:\n"
                + "\n".join(f"- {capability}" for capability in builtins)
                + "\nUse built-in capabilities directly whenever they help inspect, read, or modify local files. "
                  "Do not merely describe the steps if a suitable built-in capability is available."
            )

        if skills:
            skill_lines = "\n".join(
                f"- {skill.name}: {skill.instruction}"
                for skill in skills
            )
            sections.append(
                "Enabled skills:\n"
                f"{skill_lines}"
            )

        return "\n\n".join(section for section in sections if section)

    def _get_executable_skills(self, skills: list[Any]) -> list[dict[str, Any]]:
        executable: list[dict[str, Any]] = []
        for skill in skills:
            tool = getattr(skill, "tool", None)
            local_path = getattr(skill, "local_path", None)
            if not isinstance(tool, dict):
                continue
            command = tool.get("command")
            if not isinstance(command, list) or not command:
                continue
            if not isinstance(local_path, str) or not local_path:
                continue
            local_dir = Path(local_path)
            if not local_dir.exists() or not local_dir.is_dir():
                continue

            input_schema = tool.get("input_schema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}

            normalized_command = [str(part).strip() for part in command if str(part).strip()]
            if not normalized_command:
                continue
            if not self._is_command_runnable(local_dir, normalized_command):
                continue

            tool_name = str(tool.get("name") or skill.id).strip()
            if not tool_name:
                continue
            base_name = re.sub(r"[^a-zA-Z0-9_]", "_", tool_name)[:40] or "tool"
            safe_name = f"{skill.id[:16]}_{base_name}"

            executable.append(
                {
                    "tool_kind": "skill",
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "local_path": str(local_dir),
                    "name": safe_name,
                    "description": str(tool.get("description") or skill.description or "").strip(),
                    "input_schema": input_schema,
                    "command": normalized_command,
                    "timeout_seconds": int(tool.get("timeout_seconds") or 20),
                    "input_mode": str(tool.get("input_mode") or "stdin_json").strip() or "stdin_json",
                    "default_output_dir": str(tool.get("default_output_dir") or "").strip(),
                }
            )
        return executable

    def _workspace_root(self) -> Path:
        return Path(settings.PROJECT_ROOT).resolve()

    def _desktop_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        home = Path.home()
        candidates.append(home / "Desktop")
        candidates.append(home / "OneDrive" / "Desktop")

        userprofile = str(os.getenv("USERPROFILE") or "").strip()
        if userprofile:
            candidates.append(Path(userprofile) / "Desktop")

        one_drive = (
            str(os.getenv("OneDrive") or "").strip()
            or str(os.getenv("OneDriveConsumer") or "").strip()
            or str(os.getenv("OneDriveCommercial") or "").strip()
        )
        if one_drive:
            candidates.append(Path(one_drive) / "Desktop")

        deduped: list[Path] = []
        seen: set[str] = set()
        for item in candidates:
            try:
                key = str(item.resolve())
            except OSError:
                key = str(item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _known_folder_candidates(self, canonical: str) -> list[Path]:
        home = Path.home()
        userprofile = Path(str(os.getenv("USERPROFILE") or "").strip()) if str(os.getenv("USERPROFILE") or "").strip() else None
        one_drive = (
            str(os.getenv("OneDrive") or "").strip()
            or str(os.getenv("OneDriveConsumer") or "").strip()
            or str(os.getenv("OneDriveCommercial") or "").strip()
        )
        one_drive_path = Path(one_drive) if one_drive else None

        if canonical == "desktop":
            return self._desktop_candidates()

        folder_name_map = {
            "downloads": "Downloads",
            "documents": "Documents",
            "pictures": "Pictures",
            "videos": "Videos",
            "music": "Music",
        }
        folder_name = folder_name_map.get(canonical)
        if not folder_name:
            return []

        candidates: list[Path] = [home / folder_name]
        if userprofile is not None:
            candidates.append(userprofile / folder_name)
        if one_drive_path is not None:
            candidates.append(one_drive_path / folder_name)

        deduped: list[Path] = []
        seen: set[str] = set()
        for item in candidates:
            try:
                key = str(item.resolve())
            except OSError:
                key = str(item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _allowed_filesystem_roots(self) -> list[Path]:
        roots: list[Path] = [self._workspace_root()]

        allow_desktop = self._coerce_bool(
            os.getenv("AGENT_FS_ALLOW_DESKTOP", "1"),
            default=True,
        )
        allow_user_folders = self._coerce_bool(
            os.getenv("AGENT_FS_ALLOW_USER_FOLDERS", "1"),
            default=True,
        )
        if allow_desktop:
            for desktop in self._desktop_candidates():
                if desktop.exists() and desktop.is_dir():
                    roots.append(desktop.resolve())
        if allow_user_folders:
            for canonical in ("downloads", "documents", "pictures", "videos", "music"):
                for candidate in self._known_folder_candidates(canonical):
                    if candidate.exists() and candidate.is_dir():
                        roots.append(candidate.resolve())

        extra = str(os.getenv("AGENT_FS_ALLOWED_ROOTS", "") or "").strip()
        if extra:
            for token in re.split(r"[;\r\n]+", extra):
                part = token.strip()
                if not part:
                    continue
                candidate = Path(part).expanduser()
                if not candidate.is_absolute():
                    candidate = self._workspace_root() / candidate
                if candidate.exists() and candidate.is_dir():
                    roots.append(candidate.resolve())

        deduped: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(root)
        return deduped

    def _is_in_allowed_roots(self, target: Path) -> bool:
        resolved = target.resolve()
        for root in self._allowed_filesystem_roots():
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _resolve_special_path_alias(self, raw_path: str) -> Path | None:
        normalized = str(raw_path or "").strip().replace("\\", "/")
        if not normalized:
            return None

        desktop_aliases = {"desktop", "桌面", "我的桌面", "~/desktop", "~\\desktop"}
        lowered = normalized.lower()
        if lowered in desktop_aliases:
            candidates = self._desktop_candidates()
            for candidate in candidates:
                if candidate.exists():
                    return candidate
            return candidates[0] if candidates else None

        desktop_prefixes = ("desktop/", "桌面/", "我的桌面/")
        for prefix in desktop_prefixes:
            if lowered.startswith(prefix):
                tail = normalized[len(prefix) :]
                candidates = self._desktop_candidates()
                if not candidates:
                    return None
                base = next((item for item in candidates if item.exists()), candidates[0])
                return base / Path(tail)
        return None

    def _workspace_relative(self, target: Path) -> str:
        resolved = target.resolve()
        workspace_root = self._workspace_root()
        for root in self._allowed_filesystem_roots():
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            if root == workspace_root:
                return relative or "."
            root_label = root.name or str(root)
            return f"{root_label}/{relative}" if relative else root_label
        return str(resolved)

    def _resolve_workspace_target(self, raw_path: Any) -> Path:
        path_text = str(raw_path or "").strip()
        if not path_text:
            raise ValueError("Path is required.")

        alias_target = self._resolve_special_path_alias(path_text)
        candidate = alias_target if alias_target is not None else Path(path_text).expanduser()
        if not candidate.is_absolute():
            candidate = self._workspace_root() / candidate
        resolved = candidate.resolve()
        if not self._is_in_allowed_roots(resolved):
            allowed = ", ".join(str(item) for item in self._allowed_filesystem_roots())
            raise ValueError(f"Path is outside allowed roots. Allowed roots: {allowed}")
        return resolved

    def _known_folder_query_aliases(self, text: str) -> list[str]:
        lowered = str(text or "").lower()
        aliases: list[str] = []
        mapping: dict[str, tuple[str, ...]] = {
            "desktop": ("desktop", "桌面"),
            "downloads": ("downloads", "download", "下载"),
            "documents": ("documents", "document", "docs", "文档", "文件"),
            "pictures": ("pictures", "picture", "images", "photos", "图片", "照片"),
            "videos": ("videos", "video", "影片", "视频"),
            "music": ("music", "audio", "歌曲", "音乐"),
        }
        for canonical, keys in mapping.items():
            if any(key in lowered for key in keys):
                aliases.append(canonical)
        return aliases

    def _extract_path_query_terms(self, text: str) -> list[str]:
        raw = str(text or "").strip()
        terms: list[str] = []
        if raw:
            terms.append(raw)
        for item in self._extract_query_tokens(raw):
            terms.append(item)
        terms.extend(self._known_folder_query_aliases(raw))
        deduped: list[str] = []
        seen: set[str] = set()
        for item in terms:
            value = str(item or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped

    def _search_paths(
        self,
        *,
        query: str,
        base_path: Any = ".",
        recursive: bool = True,
        include_hidden: bool = False,
        max_results: int = 40,
        path_type: str = "any",
        max_depth: int = 6,
    ) -> list[Path]:
        normalized_type = str(path_type or "any").strip().lower()
        if normalized_type not in {"any", "file", "dir"}:
            normalized_type = "any"

        max_results = self._coerce_int(max_results, 40, minimum=1, maximum=200)
        max_depth = self._coerce_int(max_depth, 6, minimum=0, maximum=20)

        roots: list[Path]
        base_text = str(base_path or "").strip()
        if not base_text or base_text == ".":
            roots = self._allowed_filesystem_roots()
        else:
            roots = [self._resolve_workspace_target(base_text)]

        terms = [term.lower() for term in self._extract_path_query_terms(query)]
        if not terms:
            return []

        hits: list[Path] = []
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            if normalized_type in {"any", "dir"} and any(term in root.name.lower() for term in terms):
                hits.append(root)
                if len(hits) >= max_results:
                    return hits
            for current_root, dir_names, file_names in os.walk(root):
                current = Path(current_root)
                try:
                    depth = len(current.relative_to(root).parts)
                except ValueError:
                    depth = 0
                if depth > max_depth:
                    dir_names[:] = []
                    continue

                if not include_hidden:
                    dir_names[:] = [name for name in dir_names if not name.startswith(".")]
                    file_names = [name for name in file_names if not name.startswith(".")]

                names_to_check: list[tuple[str, bool]] = []
                if normalized_type in {"any", "dir"}:
                    names_to_check.extend((name, True) for name in dir_names)
                if normalized_type in {"any", "file"}:
                    names_to_check.extend((name, False) for name in file_names)

                for name, is_dir in names_to_check:
                    lowered_name = name.lower()
                    if not any(term in lowered_name for term in terms):
                        continue
                    target = current / name
                    hits.append(target)
                    if len(hits) >= max_results:
                        return hits

                if not recursive:
                    dir_names[:] = []
        return hits

    def _guess_existing_target(self, raw_path: Any, *, expect_dir: bool | None = None) -> Path | None:
        text = str(raw_path or "").strip()
        if not text:
            return None

        for alias in self._known_folder_query_aliases(text):
            for candidate in self._known_folder_candidates(alias):
                if not candidate.exists():
                    continue
                if expect_dir is True and not candidate.is_dir():
                    continue
                if expect_dir is False and not candidate.is_file():
                    continue
                if self._is_in_allowed_roots(candidate):
                    return candidate

        try:
            direct = self._resolve_workspace_target(text)
            if direct.exists():
                if expect_dir is True and not direct.is_dir():
                    return None
                if expect_dir is False and not direct.is_file():
                    return None
                return direct
        except Exception:  # noqa: BLE001
            pass

        preferred_type = "any"
        if expect_dir is True:
            preferred_type = "dir"
        elif expect_dir is False:
            preferred_type = "file"

        hits = self._search_paths(
            query=text,
            base_path=".",
            recursive=True,
            include_hidden=False,
            max_results=8,
            path_type=preferred_type,
            max_depth=6,
        )
        if not hits:
            return None
        hits = sorted(hits, key=lambda item: len(item.parts))
        return hits[0]

    def _coerce_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off"}:
                return False
        return default

    def _coerce_int(
        self,
        value: Any,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            if isinstance(value, str):
                parsed = int(value.strip())
            else:
                parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        if minimum is not None:
            parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    def _builtin_filesystem_tools(self) -> list[dict[str, Any]]:
        root = self._workspace_root()
        base = {
            "skill_id": "builtin_filesystem",
            "skill_name": "Builtin Filesystem",
            "local_path": str(root),
            "timeout_seconds": 15,
            "input_mode": "builtin",
            "default_output_dir": "",
            "execution_mode": "builtin_fs",
            "command": [],
        }
        return [
            {
                **base,
                "name": "fs_list_roots",
                "description": "List all filesystem roots currently allowed for agent access.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                **base,
                "name": "fs_search_paths",
                "description": "Search files or directories by keyword under allowed roots.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keyword to search."},
                        "path": {"type": "string", "description": "Optional base path under allowed roots."},
                        "path_type": {"type": "string", "enum": ["any", "file", "dir"]},
                        "recursive": {"type": "boolean"},
                        "include_hidden": {"type": "boolean"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                        "max_depth": {"type": "integer", "minimum": 0, "maximum": 20},
                    },
                    "required": ["query"],
                },
            },
            {
                **base,
                "name": "fs_list_directory",
                "description": "List files and directories in the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path, relative to workspace."},
                        "recursive": {"type": "boolean"},
                        "include_hidden": {"type": "boolean"},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
            },
            {
                **base,
                "name": "fs_read_file",
                "description": "Read a text file from the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path, relative to workspace."},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                        "max_chars": {"type": "integer", "minimum": 500, "maximum": 40000},
                    },
                    "required": ["path"],
                },
            },
            {
                **base,
                "name": "fs_write_file",
                "description": "Write text content to a file in the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path, relative to workspace."},
                        "content": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                **base,
                "name": "fs_append_file",
                "description": "Append text content to a file in the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path, relative to workspace."},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                **base,
                "name": "fs_make_directory",
                "description": "Create a directory in the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path, relative to workspace."},
                    },
                    "required": ["path"],
                },
            },
            {
                **base,
                "name": "fs_delete_path",
                "description": "Delete a file or directory from the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Target path, relative to workspace."},
                        "recursive": {"type": "boolean"},
                    },
                    "required": ["path"],
                },
            },
            {
                **base,
                "name": "fs_move_path",
                "description": "Move or rename a file or directory in the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string"},
                        "destination_path": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                    },
                    "required": ["source_path", "destination_path"],
                },
            },
        ]

    def _execute_builtin_filesystem_tool(
        self,
        function_name: str,
        args: dict[str, Any],
        tool: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        tool_meta: dict[str, Any] = {
            "ok": False,
            "error": None,
            "generated_files": [],
            "output_dir": None,
            "skill_id": tool.get("skill_id"),
            "skill_name": tool.get("skill_name"),
            "required_env_vars": [],
            "missing_env_vars": [],
            "required_shell_dependencies": [],
            "missing_shell_dependencies": [],
            "auto_provisioned_shell_dependencies": [],
            "auto_provision_errors": [],
            "missing_launchers": [],
            "attempt_count": 1,
            "max_attempts": 1,
            "retry_events": [],
        }
        try:
            if function_name == "fs_list_roots":
                roots = self._allowed_filesystem_roots()
                lines = [f"- {str(root)}" for root in roots]
                tool_meta["ok"] = True
                return "Allowed filesystem roots:\n" + ("\n".join(lines) if lines else "(none)"), tool_meta

            if function_name == "fs_search_paths":
                query = str(args.get("query") or "").strip()
                if not query:
                    raise ValueError("query is required.")
                hits = self._search_paths(
                    query=query,
                    base_path=args.get("path") or ".",
                    recursive=self._coerce_bool(args.get("recursive"), default=True),
                    include_hidden=self._coerce_bool(args.get("include_hidden"), default=False),
                    max_results=self._coerce_int(args.get("max_results"), 40, minimum=1, maximum=200),
                    path_type=str(args.get("path_type") or "any"),
                    max_depth=self._coerce_int(args.get("max_depth"), 6, minimum=0, maximum=20),
                )
                lines = []
                for item in hits:
                    marker = "D" if item.is_dir() else "F"
                    lines.append(f"[{marker}] {self._workspace_relative(item)}")
                body = "\n".join(lines) if lines else "(no matches)"
                tool_meta["ok"] = True
                return f"Search query: {query}\nMatches: {len(hits)}\n{body}", tool_meta

            if function_name == "fs_list_directory":
                raw_path = str(args.get("path") or ".").strip()
                try:
                    target = self._resolve_workspace_target(raw_path)
                except ValueError:
                    guessed = self._guess_existing_target(raw_path, expect_dir=True)
                    if guessed is None:
                        raise
                    target = guessed
                if not target.exists() or not target.is_dir():
                    guessed = self._guess_existing_target(raw_path, expect_dir=True)
                    if guessed is not None:
                        target = guessed
                if not target.exists():
                    raise ValueError(f"Path not found: {self._workspace_relative(target)}")
                if not target.is_dir():
                    raise ValueError(f"Not a directory: {self._workspace_relative(target)}")

                recursive = self._coerce_bool(args.get("recursive"), default=False)
                include_hidden = self._coerce_bool(args.get("include_hidden"), default=False)
                max_entries = self._coerce_int(args.get("max_entries"), 200, minimum=1, maximum=1000)

                entries: list[Path]
                if recursive:
                    entries = sorted(target.rglob("*"), key=lambda item: item.as_posix().lower())
                else:
                    entries = sorted(target.iterdir(), key=lambda item: item.as_posix().lower())

                lines: list[str] = []
                total_seen = 0
                for entry in entries:
                    if not include_hidden and any(part.startswith(".") for part in entry.parts):
                        continue
                    total_seen += 1
                    if len(lines) >= max_entries:
                        continue
                    label = "D" if entry.is_dir() else "F"
                    rel = self._workspace_relative(entry)
                    if entry.is_file():
                        try:
                            size = entry.stat().st_size
                        except OSError:
                            size = 0
                        lines.append(f"[{label}] {rel} ({size} bytes)")
                    else:
                        lines.append(f"[{label}] {rel}")

                truncated = total_seen > len(lines)
                root_display = self._workspace_relative(target)
                body = "\n".join(lines) if lines else "(empty)"
                message = (
                    f"Directory: {root_display}\n"
                    f"Entries shown: {len(lines)}"
                    + (f" (truncated from {total_seen})" if truncated else "")
                    + f"\n{body}"
                )
                tool_meta["ok"] = True
                return message[:20000], tool_meta

            if function_name == "fs_read_file":
                target = self._resolve_workspace_target(args.get("path"))
                if not target.exists():
                    raise ValueError(f"File not found: {self._workspace_relative(target)}")
                if not target.is_file():
                    raise ValueError(f"Not a file: {self._workspace_relative(target)}")

                raw = target.read_bytes()
                if b"\x00" in raw[:4096]:
                    raise ValueError("Binary file is not supported by fs_read_file.")
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines()
                total_lines = len(lines)

                start_line = self._coerce_int(
                    args.get("start_line"),
                    1,
                    minimum=1,
                    maximum=max(total_lines, 1),
                )
                if args.get("end_line") is None:
                    end_line = min(total_lines, start_line + 199) if total_lines > 0 else 0
                else:
                    end_line = self._coerce_int(
                        args.get("end_line"),
                        start_line,
                        minimum=start_line,
                        maximum=max(total_lines, start_line),
                    )

                if total_lines == 0:
                    payload = "(empty file)"
                    line_scope = "0-0/0"
                else:
                    selected = lines[start_line - 1 : end_line]
                    numbered = [
                        f"{index} | {value}"
                        for index, value in enumerate(selected, start=start_line)
                    ]
                    payload = "\n".join(numbered)
                    line_scope = f"{start_line}-{end_line}/{total_lines}"

                max_chars = self._coerce_int(
                    args.get("max_chars"),
                    12000,
                    minimum=500,
                    maximum=40000,
                )
                truncated = False
                if len(payload) > max_chars:
                    payload = payload[:max_chars]
                    truncated = True

                message = (
                    f"File: {self._workspace_relative(target)}\n"
                    f"Lines: {line_scope}\n"
                    f"{payload}"
                    + ("\n... (truncated)" if truncated else "")
                )
                tool_meta["ok"] = True
                return message, tool_meta

            if function_name == "fs_write_file":
                target = self._resolve_workspace_target(args.get("path"))
                if target.exists() and target.is_dir():
                    raise ValueError(f"Cannot write file because target is a directory: {self._workspace_relative(target)}")

                overwrite = self._coerce_bool(args.get("overwrite"), default=True)
                if target.exists() and not overwrite:
                    raise ValueError(
                        f"File already exists and overwrite is false: {self._workspace_relative(target)}"
                    )

                content = args.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                rel = self._workspace_relative(target)
                tool_meta["generated_files"] = [rel]
                tool_meta["ok"] = True
                return f"Wrote file: {rel} ({len(content.encode('utf-8'))} bytes).", tool_meta

            if function_name == "fs_append_file":
                target = self._resolve_workspace_target(args.get("path"))
                if target.exists() and target.is_dir():
                    raise ValueError(f"Cannot append file because target is a directory: {self._workspace_relative(target)}")

                content = args.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(content)
                rel = self._workspace_relative(target)
                tool_meta["generated_files"] = [rel]
                tool_meta["ok"] = True
                return f"Appended file: {rel} ({len(content.encode('utf-8'))} bytes).", tool_meta

            if function_name == "fs_make_directory":
                target = self._resolve_workspace_target(args.get("path"))
                target.mkdir(parents=True, exist_ok=True)
                tool_meta["ok"] = True
                return f"Directory ready: {self._workspace_relative(target)}", tool_meta

            if function_name == "fs_delete_path":
                target = self._resolve_workspace_target(args.get("path"))
                if not target.exists():
                    raise ValueError(f"Path not found: {self._workspace_relative(target)}")
                recursive = self._coerce_bool(args.get("recursive"), default=False)
                rel = self._workspace_relative(target)
                if target.is_dir():
                    if recursive:
                        shutil.rmtree(target)
                    else:
                        try:
                            target.rmdir()
                        except OSError as error:
                            raise ValueError(
                                "Directory is not empty. Set recursive=true to delete recursively."
                            ) from error
                else:
                    target.unlink()
                tool_meta["ok"] = True
                return f"Deleted: {rel}", tool_meta

            if function_name == "fs_move_path":
                source = self._resolve_workspace_target(args.get("source_path"))
                destination = self._resolve_workspace_target(args.get("destination_path"))
                if not source.exists():
                    raise ValueError(f"Source path not found: {self._workspace_relative(source)}")
                if source == destination:
                    raise ValueError("Source and destination are the same path.")

                overwrite = self._coerce_bool(args.get("overwrite"), default=False)
                if destination.exists():
                    if not overwrite:
                        raise ValueError(
                            f"Destination already exists and overwrite is false: {self._workspace_relative(destination)}"
                        )
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                rel = self._workspace_relative(destination)
                tool_meta["generated_files"] = [rel]
                tool_meta["ok"] = True
                return f"Moved: {self._workspace_relative(source)} -> {rel}", tool_meta

            raise ValueError(f"Unsupported builtin tool: {function_name}")
        except Exception as error:  # noqa: BLE001
            message = str(error).strip() or f"Builtin tool '{function_name}' failed."
            tool_meta["error"] = message
            return message[:1200], tool_meta

    def _get_builtin_tools(self, agent: AgentDefinition) -> list[dict[str, Any]]:
        capabilities = {
            str(item).strip()
            for item in (getattr(agent, "builtin_capabilities", None) or [])
            if str(item).strip()
        }
        tools: list[dict[str, Any]] = []

        if "fs_list" in capabilities:
            tools.append(
                {
                    "tool_kind": "builtin",
                    "builtin_capability": "fs_list",
                    "name": "builtin_list_directory",
                    "skill_id": None,
                    "skill_name": "Built-in Filesystem",
                    "description": (
                        "List files and folders in a directory path. "
                        "用于查看目录、桌面、下载目录中的文件和文件夹。"
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Directory path to inspect.",
                            },
                            "include_hidden": {
                                "type": "boolean",
                                "description": "Whether to include hidden files.",
                            },
                            "recursive": {
                                "type": "boolean",
                                "description": "Whether to traverse subdirectories recursively.",
                            },
                            "max_entries": {
                                "type": "integer",
                                "description": "Maximum number of entries to return.",
                            },
                        },
                    },
                }
            )

        if "fs_read" in capabilities:
            tools.append(
                {
                    "tool_kind": "builtin",
                    "builtin_capability": "fs_read",
                    "name": "builtin_read_file",
                    "skill_id": None,
                    "skill_name": "Built-in Filesystem",
                    "description": (
                        "Read a text file from a path. "
                        "用于读取本地文本文件内容。"
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "File path to read.",
                            },
                            "max_chars": {
                                "type": "integer",
                                "description": "Maximum number of characters to return.",
                            },
                        },
                        "required": ["path"],
                    },
                }
            )

        if "fs_write" in capabilities:
            tools.append(
                {
                    "tool_kind": "builtin",
                    "builtin_capability": "fs_write",
                    "name": "builtin_write_file",
                    "skill_id": None,
                    "skill_name": "Built-in Filesystem",
                    "description": (
                        "Write text content to a file path. "
                        "用于创建或覆盖本地文本文件。"
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Target file path.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Text content to write.",
                            },
                        },
                        "required": ["path", "content"],
                    },
                }
            )

        return tools

    def build_skill_preflight(self, skill: Any) -> dict[str, Any]:
        skill_id = str(getattr(skill, "id", "") or "").strip()
        skill_name = str(getattr(skill, "name", "") or "").strip()
        local_path = str(getattr(skill, "local_path", "") or "").strip()
        tool = getattr(skill, "tool", None)

        base: dict[str, Any] = {
            "skill_id": skill_id,
            "skill_name": skill_name,
            "tool_enabled": False,
            "ready": True,
            "status": "prompt_only",
            "command": [],
            "input_mode": None,
            "timeout_seconds": None,
            "required_env_vars": [],
            "missing_env_vars": [],
            "required_shell_dependencies": [],
            "missing_shell_dependencies": [],
            "auto_provisioned_shell_dependencies": [],
            "auto_provision_errors": [],
            "missing_launchers": [],
            "node_prepare_required": False,
            "python_prepare_required": False,
            "message": "Prompt-only skill (no executable tool).",
        }
        if not isinstance(tool, dict):
            return base

        base["tool_enabled"] = True
        command = [str(part).strip() for part in (tool.get("command") or []) if str(part).strip()]
        command = self._resolve_runtime_command(command)
        base["command"] = command
        base["input_mode"] = str(tool.get("input_mode") or "stdin_json").strip() or "stdin_json"
        base["timeout_seconds"] = int(tool.get("timeout_seconds") or 20)

        if not command:
            base.update(
                {
                    "ready": False,
                    "status": "invalid_tool",
                    "message": "Tool command is missing.",
                }
            )
            return base

        if not local_path:
            base.update(
                {
                    "ready": False,
                    "status": "invalid_local_path",
                    "message": "Skill local path is missing.",
                }
            )
            return base

        tool_dir = Path(local_path)
        if not tool_dir.exists() or not tool_dir.is_dir():
            base.update(
                {
                    "ready": False,
                    "status": "invalid_local_path",
                    "message": f"Skill local path does not exist: {local_path}",
                }
            )
            return base

        missing_launchers = self._missing_command_launchers(command)
        if missing_launchers:
            joined = ", ".join(missing_launchers)
            base.update(
                {
                    "ready": False,
                    "status": "missing_launcher",
                    "missing_launchers": missing_launchers,
                    "message": (
                        f"Missing command launcher(s): {joined}. "
                        "Install missing runtime dependencies manually (e.g., jq), "
                        "or run backend/scripts/bootstrap-runtime.sh on Unix."
                    ),
                }
            )
            return base

        if not self._is_command_runnable(tool_dir, command):
            base.update(
                {
                    "ready": False,
                    "status": "missing_command_target",
                    "message": "Tool script or command target is missing.",
                }
            )
            return base

        tool_ctx = {"local_path": str(tool_dir)}
        runtime_env = self._build_runtime_env(tool_dir=tool_dir)
        required_env_vars = self._detect_required_env_vars(tool_ctx, command)
        missing_env_vars = [key for key in required_env_vars if not str(runtime_env.get(key, "")).strip()]
        required_shell_deps = self._detect_shell_dependencies(tool_ctx, command)
        missing_shell_deps = self._missing_shell_dependencies(tool_ctx, command, env_map=runtime_env)
        if missing_shell_deps:
            missing_shell_deps, auto_provisioned, auto_provision_errors = self._auto_provision_shell_dependencies(
                missing_shell_deps,
                runtime_env=runtime_env,
            )
        else:
            auto_provisioned = []
            auto_provision_errors = []

        base["required_env_vars"] = required_env_vars
        base["missing_env_vars"] = missing_env_vars
        base["required_shell_dependencies"] = required_shell_deps
        base["missing_shell_dependencies"] = missing_shell_deps
        base["auto_provisioned_shell_dependencies"] = auto_provisioned
        base["auto_provision_errors"] = auto_provision_errors

        first = command[0]
        if self._is_node_launcher(first):
            package_json = tool_dir / "package.json"
            node_modules = tool_dir / "node_modules"
            node_prepare_required = package_json.exists() and not node_modules.exists()
            base["node_prepare_required"] = node_prepare_required
        if first.lower().endswith(".py") or self._is_python_launcher(first):
            requirements = tool_dir / "requirements.txt"
            base["python_prepare_required"] = requirements.exists() and requirements.is_file()

        if missing_shell_deps:
            joined = ", ".join(missing_shell_deps)
            base.update(
                {
                    "ready": False,
                    "status": "missing_shell_dependencies",
                    "message": (
                        f"Missing shell dependencies: {joined}. "
                        "Install them manually (e.g., jq), or run backend/scripts/bootstrap-runtime.sh on Unix."
                    ),
                }
            )
            return base

        if missing_env_vars:
            joined = ", ".join(missing_env_vars)
            base.update(
                {
                    "ready": False,
                    "status": "missing_environment",
                    "message": f"Missing environment variables: {joined}",
                }
            )
            return base

        base.update(
            {
                "ready": True,
                "status": "ready",
                "message": (
                    "Tool is ready."
                    if not (base["node_prepare_required"] or base["python_prepare_required"])
                    else (
                        "Tool is ready (runtime dependencies may install on first execution)."
                    )
                ),
            }
        )
        if auto_provisioned:
            base["message"] = (
                f"Tool is ready. Auto-provisioned shell dependencies: {', '.join(auto_provisioned)}."
            )
        return base

    def _skill_runtime_slug(self, tool_dir: Path) -> str:
        try:
            raw = str(tool_dir.resolve())
        except OSError:
            raw = str(tool_dir)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return digest

    def _first_non_empty_env_value(self, env_map: dict[str, str], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = str(env_map.get(key, "")).strip()
            if value:
                return value
        return ""

    def _set_env_if_missing(self, env_map: dict[str, str], key: str, value: str) -> None:
        if not value:
            return
        if str(env_map.get(key, "")).strip():
            return
        env_map[key] = value

    def _apply_llm_env_aliases(self, env_map: dict[str, str]) -> dict[str, str]:
        key_value = self._first_non_empty_env_value(
            env_map,
            (
                "LLM_API_KEY",
                "OPENAI_API_KEY",
                "OPENROUTER_API_KEY",
                "MOONSHOT_API_KEY",
                "QWEN_API_KEY",
                "DASHSCOPE_API_KEY",
            ),
        )
        base_value = self._first_non_empty_env_value(
            env_map,
            (
                "LLM_BASE_URL",
                "OPENAI_BASE_URL",
                "OPENROUTER_BASE_URL",
                "MOONSHOT_BASE_URL",
                "QWEN_BASE_URL",
                "DASHSCOPE_BASE_URL",
            ),
        )
        model_value = self._first_non_empty_env_value(
            env_map,
            (
                "LLM_MODEL",
                "OPENAI_MODEL",
                "OPENROUTER_MODEL",
                "MOONSHOT_MODEL",
                "QWEN_MODEL",
                "DASHSCOPE_MODEL",
            ),
        )

        self._set_env_if_missing(env_map, "LLM_API_KEY", key_value)
        self._set_env_if_missing(env_map, "LLM_BASE_URL", base_value)
        self._set_env_if_missing(env_map, "LLM_MODEL", model_value)

        llm_key = str(env_map.get("LLM_API_KEY", "")).strip()
        llm_base = str(env_map.get("LLM_BASE_URL", "")).strip()
        llm_model = str(env_map.get("LLM_MODEL", "")).strip()

        self._set_env_if_missing(env_map, "OPENAI_API_KEY", llm_key)
        self._set_env_if_missing(env_map, "OPENAI_BASE_URL", llm_base)
        self._set_env_if_missing(env_map, "OPENAI_MODEL", llm_model)

        base_lower = llm_base.lower()
        if "openrouter.ai" in base_lower:
            self._set_env_if_missing(env_map, "OPENROUTER_API_KEY", llm_key)
            self._set_env_if_missing(env_map, "OPENROUTER_BASE_URL", llm_base)
            self._set_env_if_missing(env_map, "OPENROUTER_MODEL", llm_model)
        if "moonshot.cn" in base_lower:
            self._set_env_if_missing(env_map, "MOONSHOT_API_KEY", llm_key)
            self._set_env_if_missing(env_map, "MOONSHOT_BASE_URL", llm_base)
            self._set_env_if_missing(env_map, "MOONSHOT_MODEL", llm_model)
        if "dashscope.aliyuncs.com" in base_lower:
            self._set_env_if_missing(env_map, "QWEN_API_KEY", llm_key)
            self._set_env_if_missing(env_map, "QWEN_BASE_URL", llm_base)
            self._set_env_if_missing(env_map, "QWEN_MODEL", llm_model)
            self._set_env_if_missing(env_map, "DASHSCOPE_API_KEY", llm_key)
            self._set_env_if_missing(env_map, "DASHSCOPE_BASE_URL", llm_base)
            self._set_env_if_missing(env_map, "DASHSCOPE_MODEL", llm_model)
        return env_map

    def _default_runtime_env(self) -> dict[str, str]:
        env_map = dict(os.environ)
        env_candidates = [
            Path(settings.APP_ENV_PATH),
            Path(settings.APP_HOME) / ".env",
            Path(settings.PROJECT_ROOT) / ".env",
        ]
        seen_env_paths: set[str] = set()
        for env_path in env_candidates:
            try:
                normalized = str(env_path.resolve())
            except OSError:
                normalized = str(env_path)
            if normalized in seen_env_paths:
                continue
            seen_env_paths.add(normalized)
            if not env_path.exists() or not env_path.is_file():
                continue
            try:
                loaded = dotenv_values(env_path)
            except Exception:  # noqa: BLE001
                loaded = {}
            for key, value in loaded.items():
                key_text = str(key or "").strip()
                if not key_text:
                    continue
                value_text = str(value) if value is not None else ""
                if value_text and not str(env_map.get(key_text, "")).strip():
                    env_map[key_text] = value_text
        return self._apply_llm_env_aliases(env_map)

    def _python_exec_for_venv(self, venv_dir: Path) -> str:
        if os.name == "nt":
            return str(venv_dir / "Scripts" / "python.exe")
        return str(venv_dir / "bin" / "python")

    def _prepare_python_runtime(
        self,
        *,
        tool_dir: Path,
        runtime_env: dict[str, str],
    ) -> tuple[str | None, str | None]:
        requirements = tool_dir / "requirements.txt"
        if not requirements.exists() or not requirements.is_file():
            return None, None

        slug = self._skill_runtime_slug(tool_dir)
        venv_dir = self._runtime_root / "venvs" / slug
        python_exec = self._python_exec_for_venv(venv_dir)
        cache_key = str(venv_dir.resolve()) if venv_dir.exists() else str(venv_dir)
        if cache_key in self._prepared_python_dirs and Path(python_exec).exists():
            return python_exec, None

        try:
            if not Path(python_exec).exists():
                venv_dir.parent.mkdir(parents=True, exist_ok=True)
                created = subprocess.run(
                    [sys.executable, "-m", "venv", str(venv_dir)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=runtime_env,
                )
                if created.returncode != 0:
                    detail = (created.stderr or created.stdout or "venv creation failed").strip()
                    return None, f"python venv creation failed: {detail[:1200]}"

            installed = subprocess.run(
                [
                    python_exec,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    str(requirements),
                ],
                capture_output=True,
                text=True,
                cwd=str(tool_dir),
                timeout=300,
                env=runtime_env,
            )
            if installed.returncode != 0:
                detail = (installed.stderr or installed.stdout or "pip install failed").strip()
                return None, f"python dependency install failed: {detail[:1200]}"
        except subprocess.TimeoutExpired:
            return None, "python dependency install timed out."
        except Exception as error:  # noqa: BLE001
            return None, f"python dependency setup failed: {error}"

        self._prepared_python_dirs.add(cache_key)
        return python_exec, None

    def _build_runtime_env(
        self,
        *,
        tool_dir: Path,
    ) -> dict[str, str]:
        runtime_env = self._default_runtime_env()
        bin_dir = self._runtime_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        path_parts: list[str] = []
        current_path = str(runtime_env.get("PATH") or "")
        if str(bin_dir) not in current_path:
            path_parts.append(str(bin_dir))
        if str(tool_dir) not in current_path:
            path_parts.append(str(tool_dir))
        if current_path:
            path_parts.append(current_path)
        runtime_env["PATH"] = os.pathsep.join(path_parts) if path_parts else current_path
        if not str(runtime_env.get("LANG") or "").strip():
            runtime_env["LANG"] = "C.UTF-8"
        if not str(runtime_env.get("LC_ALL") or "").strip():
            runtime_env["LC_ALL"] = str(runtime_env.get("LANG") or "C.UTF-8")
        return runtime_env

    def _can_auto_provision_shell_dependency(self, dep: str) -> bool:
        return dep in {"jq", "base64"}

    def _download_file(self, url: str, target: Path) -> str | None:
        try:
            with urlopen(url, timeout=30) as response:  # nosec B310
                data = response.read()
        except Exception as error:  # noqa: BLE001
            return str(error)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if os.name != "nt":
                target.chmod(0o755)
        except OSError as error:
            return str(error)
        return None

    def _ensure_jq_binary(self, runtime_env: dict[str, str]) -> tuple[bool, str | None]:
        existing = shutil.which("jq", path=runtime_env.get("PATH", ""))
        if existing:
            return True, None

        bin_dir = self._runtime_root / "bin"
        if os.name == "nt":
            target = bin_dir / "jq.exe"
            if target.exists():
                return True, None
            url = "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-windows-amd64.exe"
            error = self._download_file(url, target)
            return (error is None), error

        machine = platform.machine().lower()
        system = platform.system().lower()
        if system == "linux":
            suffix = "jq-linux-arm64" if "aarch64" in machine or "arm64" in machine else "jq-linux-amd64"
        elif system == "darwin":
            suffix = "jq-macos-arm64" if "arm64" in machine else "jq-macos-amd64"
        else:
            return False, f"unsupported platform for auto jq provisioning: {system}"

        target = bin_dir / "jq"
        if target.exists():
            return True, None
        url = f"https://github.com/jqlang/jq/releases/download/jq-1.7.1/{suffix}"
        error = self._download_file(url, target)
        return (error is None), error

    def _ensure_base64_utility(self, runtime_env: dict[str, str]) -> tuple[bool, str | None]:
        existing = shutil.which("base64", path=runtime_env.get("PATH", ""))
        if existing:
            return True, None

        if os.name != "nt":
            return False, "base64 not found and auto-provision is only implemented for Windows."

        bin_dir = self._runtime_root / "bin"
        py_path = bin_dir / "base64_shim.py"
        cmd_path = bin_dir / "base64.cmd"
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
            if not py_path.exists():
                py_path.write_text(
                    (
                        "import base64\n"
                        "import sys\n\n"
                        "args = [a.strip() for a in sys.argv[1:]]\n"
                        "decode = ('-d' in args) or ('--decode' in args)\n"
                        "data = sys.stdin.buffer.read()\n"
                        "if decode:\n"
                        "    try:\n"
                        "        out = base64.b64decode(data, validate=False)\n"
                        "    except Exception:\n"
                        "        sys.stderr.write('base64: decode failed\\n')\n"
                        "        sys.exit(1)\n"
                        "    sys.stdout.buffer.write(out)\n"
                        "else:\n"
                        "    out = base64.b64encode(data)\n"
                        "    sys.stdout.buffer.write(out + b'\\n')\n"
                    ),
                    encoding="utf-8",
                )
            if not cmd_path.exists():
                python_exec = str(Path(sys.executable).resolve())
                cmd_path.write_text(
                    "@echo off\r\n"
                    f"\"{python_exec}\" \"%~dp0base64_shim.py\" %*\r\n",
                    encoding="utf-8",
                )
        except OSError as error:
            return False, str(error)

        if shutil.which("base64", path=runtime_env.get("PATH", "")):
            return True, None
        return True, None

    def _auto_provision_shell_dependencies(
        self,
        missing_deps: list[str],
        *,
        runtime_env: dict[str, str],
    ) -> tuple[list[str], list[str], list[str]]:
        if not missing_deps:
            return [], [], []

        provisioned: list[str] = []
        errors: list[str] = []
        for dep in sorted(set(missing_deps)):
            if dep == "jq":
                ok, error = self._ensure_jq_binary(runtime_env)
                if ok:
                    provisioned.append(dep)
                elif error:
                    errors.append(f"{dep}: {error}")
            elif dep == "base64":
                ok, error = self._ensure_base64_utility(runtime_env)
                if ok:
                    provisioned.append(dep)
                elif error:
                    errors.append(f"{dep}: {error}")

        remaining = [dep for dep in missing_deps if dep not in provisioned]
        return remaining, provisioned, errors

    def _is_python_launcher(self, token: str) -> bool:
        lowered = token.lower()
        basename = Path(lowered).name
        return basename in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}

    def _is_node_launcher(self, token: str) -> bool:
        lowered = token.lower()
        basename = Path(lowered).name
        return basename in {"node", "node.exe"}

    def _resolve_shell_launcher(self) -> str | None:
        if os.name == "nt":
            preferred = [
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
            ]
            for candidate in preferred:
                if Path(candidate).exists():
                    return candidate

        discovered = [
            shutil.which("bash"),
            shutil.which("bash.exe"),
            shutil.which("sh"),
            shutil.which("sh.exe"),
        ]
        candidates = [item for item in discovered if item]
        if not candidates:
            return None
        if os.name == "nt":
            for item in candidates:
                normalized = str(item).replace("/", "\\").lower()
                if "\\windows\\system32\\bash.exe" in normalized:
                    continue
                return str(item)
        return str(candidates[0])

    def _resolve_runtime_command(self, command: list[str]) -> list[str]:
        if not command:
            return command
        resolved = list(command)
        first = str(resolved[0]).strip()
        first_base = Path(first.lower()).name
        if first_base in {"bash", "bash.exe", "sh", "sh.exe"}:
            shell_bin = self._resolve_shell_launcher()
            if shell_bin:
                resolved[0] = shell_bin
        return resolved

    def _missing_command_launchers(self, command: list[str]) -> list[str]:
        if not command:
            return []

        first = str(command[0]).strip()
        if not first:
            return []

        if self._is_python_launcher(first):
            return []

        first_path = Path(first)
        if first_path.is_absolute():
            return [] if first_path.exists() else [first]

        first_base = Path(first.lower()).name
        if first_base in {"bash", "bash.exe", "sh", "sh.exe"}:
            shell_bin = self._resolve_shell_launcher()
            return [] if shell_bin else [first]

        if self._is_node_launcher(first):
            node_bin = shutil.which("node") or shutil.which("node.exe")
            return [] if node_bin else [first]

        if "/" in first or "\\" in first:
            return []

        return [] if shutil.which(first) else [first]

    def _is_command_runnable(self, local_dir: Path, command: list[str]) -> bool:
        if not command:
            return False

        first = command[0]
        first_path = Path(first)

        if self._is_python_launcher(first):
            if len(command) < 2:
                return False
            script_token = command[1]
            if script_token.startswith("-"):
                return False
            script_path = Path(script_token)
            if script_path.is_absolute():
                return script_path.exists() and script_path.is_file()
            return (local_dir / script_path).exists() and (local_dir / script_path).is_file()

        if self._is_node_launcher(first):
            if len(command) < 2:
                return False
            script_token = command[1]
            if script_token.startswith("-"):
                return True
            script_path = Path(script_token)
            if script_path.is_absolute():
                return script_path.exists() and script_path.is_file()
            return (local_dir / script_path).exists() and (local_dir / script_path).is_file()

        if first.lower().endswith(".py"):
            if first_path.is_absolute():
                return first_path.exists() and first_path.is_file()
            return (local_dir / first_path).exists() and (local_dir / first_path).is_file()

        if first_path.is_absolute():
            return first_path.exists()
        if "/" in first or "\\" in first:
            target = local_dir / first_path
            return target.exists() and target.is_file()
        return True

    def _inline_shell_script(
        self,
        local_dir: Path,
        command: list[str],
    ) -> tuple[list[str], str | None]:
        if not command:
            return command, None
        first_base = Path(str(command[0]).lower()).name
        if first_base not in {"bash", "bash.exe", "sh", "sh.exe"} or len(command) < 2:
            return command, None

        script_token = str(command[1]).strip()
        if not script_token.lower().endswith(".sh"):
            return command, None

        script_path = Path(script_token)
        if not script_path.is_absolute():
            script_path = local_dir / script_path
        if not script_path.exists() or not script_path.is_file():
            return command, None

        try:
            raw = script_path.read_bytes()
        except OSError:
            return command, None

        script_text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        # For `bash -s`, the first argument after `--` becomes $0.
        # Add a placeholder script name so original script args still map to $1/$2...
        inlined_command = [command[0], "-s", "--", "__skill_inline__", *command[2:]]
        return inlined_command, script_text

    def _resolve_tool_script_path(self, tool_dir: Path, command: list[str]) -> Path | None:
        if not command:
            return None
        candidates: list[str] = []
        first = str(command[0]).strip()
        first_base = Path(first.lower()).name
        if first_base in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe", "node", "node.exe", "bash", "bash.exe", "sh", "sh.exe"}:
            if len(command) >= 2:
                second = str(command[1]).strip()
                if second and not second.startswith("-"):
                    candidates.append(second)
        elif first and not first.startswith("-"):
            candidates.append(first)

        for token in candidates:
            path = Path(token)
            if not path.is_absolute():
                path = tool_dir / path
            if path.exists() and path.is_file():
                return path
        return None

    def _extract_env_vars_from_text(self, text: str) -> set[str]:
        patterns = [
            r"process\.env\.([A-Z][A-Z0-9_]{2,})",
            r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]",
            r"getenv\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]",
            r"\$\{([A-Z][A-Z0-9_]{2,})\}",
            r"\$([A-Z][A-Z0-9_]{2,})\b",
            r"\bexport\s+([A-Z][A-Z0-9_]{2,})\b",
        ]
        found: set[str] = set()
        for pattern in patterns:
            for match in re.findall(pattern, text):
                key = str(match).strip().upper()
                if key and key not in self._ENV_IGNORE:
                    found.add(key)
        return found

    def _detect_required_env_vars(self, tool: dict[str, Any], command: list[str]) -> list[str]:
        tool_dir = Path(str(tool.get("local_path") or ""))
        cache_key = f"{tool_dir.resolve()}::{json.dumps(command, ensure_ascii=False)}"
        cached = self._tool_env_cache.get(cache_key)
        if cached is not None:
            return cached

        texts: list[str] = []
        script_path = self._resolve_tool_script_path(tool_dir, command)
        if script_path is not None:
            try:
                texts.append(script_path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        skill_md = tool_dir / "SKILL.md"
        if skill_md.exists() and skill_md.is_file():
            try:
                texts.append(skill_md.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass

        required: set[str] = set()
        for text in texts:
            required.update(self._extract_env_vars_from_text(text))

        filtered = sorted(
            key
            for key in required
            if any(token in key for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        )
        self._tool_env_cache[cache_key] = filtered
        return filtered

    def _missing_required_env_vars(
        self,
        tool: dict[str, Any],
        command: list[str],
        *,
        env_map: dict[str, str] | None = None,
    ) -> list[str]:
        required = self._detect_required_env_vars(tool, command)
        runtime_env = env_map or self._default_runtime_env()
        missing: list[str] = []
        for key in required:
            value = runtime_env.get(key, "")
            if not str(value).strip():
                missing.append(key)
        return missing

    def _extract_shell_dependencies(self, script_text: str) -> list[str]:
        candidates = (
            "jq",
            "curl",
            "npx",
            "node",
            "python",
            "python3",
            "git",
            "wget",
            "sed",
            "awk",
            "base64",
        )
        found: list[str] = []
        for token in candidates:
            if re.search(rf"\b{re.escape(token)}\b", script_text):
                found.append(token)
        return found

    def _detect_shell_dependencies(self, tool: dict[str, Any], command: list[str]) -> list[str]:
        if not command:
            return []
        first_base = Path(str(command[0]).lower()).name
        if first_base not in {"bash", "bash.exe", "sh", "sh.exe"}:
            return []

        tool_dir = Path(str(tool.get("local_path") or ""))
        script_path = self._resolve_tool_script_path(tool_dir, command)
        if script_path is None:
            return []

        cache_key = str(script_path.resolve())
        deps = self._shell_deps_cache.get(cache_key)
        if deps is None:
            try:
                script_text = script_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                script_text = ""
            deps = self._extract_shell_dependencies(script_text)
            self._shell_deps_cache[cache_key] = deps
        return sorted(set(deps))

    def _missing_shell_dependencies(
        self,
        tool: dict[str, Any],
        command: list[str],
        *,
        env_map: dict[str, str] | None = None,
    ) -> list[str]:
        deps = self._detect_shell_dependencies(tool, command)
        runtime_env = env_map or self._default_runtime_env()
        missing: list[str] = []
        for dep in deps:
            try:
                probe = subprocess.run(
                    [command[0], "-lc", f"command -v {shlex.quote(dep)} >/dev/null 2>&1"],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    env=runtime_env,
                )
            except Exception:  # noqa: BLE001
                missing.append(dep)
                continue
            if probe.returncode != 0:
                missing.append(dep)
        return sorted(set(missing))

    def _extract_query_tokens(self, text: str) -> set[str]:
        lowered = text.lower()
        tokens: set[str] = set()
        for token in re.findall(r"[a-z][a-z0-9_-]{2,}", lowered):
            if token in self._EN_STOPWORDS:
                continue
            tokens.add(token)
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,6}", text):
            if chunk in self._ZH_STOPWORDS:
                continue
            tokens.add(chunk)
        return tokens

    def _looks_like_filesystem_intent(self, text: str) -> bool:
        lowered = text.lower()
        keyword_hits = (
            "file",
            "files",
            "folder",
            "directory",
            "dir",
            "path",
            "read",
            "write",
            "edit",
            "modify",
            "create",
            "delete",
            "remove",
            "rename",
            "move",
            "list",
            "code",
            "repo",
            "project",
            "目录",
            "文件",
            "读取",
            "查看",
            "写入",
            "修改",
            "创建",
            "删除",
            "重命名",
            "移动",
            "代码",
            "工程",
        )
        if any(keyword in lowered for keyword in keyword_hits):
            return True
        return bool(
            re.search(
                r"([a-zA-Z]:\\|[\\/][^\\/\s]+|\.{1,2}[\\/]|[a-zA-Z0-9_-]+\.(py|js|ts|tsx|json|md|yaml|yml|txt))",
                text,
            )
        )

    def _infer_intent_labels(self, text: str) -> set[str]:
        lowered = text.lower()
        labels: set[str] = set()
        for label, keywords in self._INTENT_KEYWORDS_V2.items():
            if any(keyword in lowered for keyword in keywords):
                labels.add(label)
        if self._looks_like_filesystem_intent(text):
            labels.add("filesystem")
        return labels

    def _score_tool_relevance(
        self,
        *,
        user_input: str,
        tool: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        tool_text = " ".join(
            str(part or "")
            for part in (
                tool.get("name"),
                tool.get("skill_name"),
                tool.get("description"),
            )
        )
        user_labels = self._infer_intent_labels(user_input)
        tool_labels = self._infer_intent_labels(tool_text)
        label_overlap = sorted(user_labels.intersection(tool_labels))

        user_tokens = self._extract_query_tokens(user_input)
        tool_tokens = self._extract_query_tokens(tool_text)
        lexical_overlap = sorted(user_tokens.intersection(tool_tokens))

        score = 0
        score += len(label_overlap) * 10
        score += min(8, len(lexical_overlap))
        if user_labels and tool_labels and not label_overlap:
            score -= 12
        if not label_overlap and len(lexical_overlap) < 2:
            score -= 4
        if str(tool.get("name") or "").lower() in user_input.lower():
            score += 2

        debug = {
            "tool_name": tool.get("name"),
            "skill_name": tool.get("skill_name"),
            "score": score,
            "label_overlap": label_overlap,
            "tool_labels": sorted(tool_labels),
            "lexical_overlap": lexical_overlap[:10],
        }
        return score, debug

    def _select_relevant_tools(
        self,
        user_input: str,
        executable_skills: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        scored: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for tool in executable_skills:
            score, debug = self._score_tool_relevance(user_input=user_input, tool=tool)
            scored.append((score, tool, debug))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected_builtins = [
            item
            for _, item, _ in scored
            if str(item.get("tool_kind") or "") == "builtin"
        ]
        selected_scored = [
            item
            for score, item, _ in scored
            if str(item.get("tool_kind") or "") != "builtin" and score > 0
        ]
        selected = [*selected_builtins, *selected_scored[: max(0, 8 - len(selected_builtins))]]
        if self._looks_like_filesystem_intent(user_input):
            builtin_fs = [
                item
                for _, item, _ in scored
                if str(item.get("execution_mode") or "") == "builtin_fs"
            ]
            for item in builtin_fs:
                if len(selected) >= 8:
                    break
                if item not in selected:
                    selected.append(item)
        debug_rows = [debug for _, _, debug in scored]
        return selected, debug_rows

    def _build_argv_command(
        self,
        *,
        command: list[str],
        args: dict[str, Any],
        tool: dict[str, Any],
    ) -> list[str]:
        built = list(command)

        content = str(
            args.get("content")
            or args.get("text")
            or args.get("input")
            or args.get("query")
            or ""
        ).strip()
        if content:
            built.append(content)

        title = str(args.get("title") or "").strip()
        if title:
            built.extend(["--title", title])

        output_dir = str(args.get("output_dir") or "").strip() or str(tool.get("default_output_dir") or "").strip()
        if output_dir:
            built.extend(["--output", output_dir])

        cards = args.get("cards")
        if isinstance(cards, int) and cards > 0:
            built.extend(["--cards", str(cards)])

        single = args.get("single")
        if isinstance(single, bool) and single:
            built.append("--single")

        with_images = args.get("with_images")
        if isinstance(with_images, bool) and with_images:
            built.append("--with-images")

        return built

    def _build_argv_json_command(
        self,
        *,
        command: list[str],
        args: dict[str, Any],
    ) -> list[str]:
        payload = dict(args)
        if "query" not in payload:
            content = str(payload.get("content") or payload.get("text") or "").strip()
            if content:
                payload["query"] = content
        payload.pop("content", None)
        payload.pop("text", None)
        if "query" not in payload:
            payload["query"] = ""
        # Keep argv JSON ASCII-safe so shell runtimes on Windows do not corrupt
        # non-ASCII bytes when forwarding arguments to external tools.
        json_arg = json.dumps(payload, ensure_ascii=True)
        return [*command, json_arg]

    def _extract_structured_tool_error(self, text: str) -> str | None:
        raw = str(text or "").strip()
        if not raw:
            return None

        lowered_raw = raw.lower()
        plain_error_markers = (
            "internal server error",
            "bad request",
            "unauthorized",
            "forbidden",
            "service unavailable",
            "missing mcp-session-id",
        )
        if any(marker in lowered_raw for marker in plain_error_markers):
            return raw[:300]
        if lowered_raw.startswith("error:"):
            return raw[6:].strip() or raw[:300]

        candidates = [raw]
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if lines:
            candidates.extend(lines[:3])

        for candidate in candidates:
            if not candidate.startswith("{"):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            error_obj = parsed.get("error")
            if isinstance(error_obj, dict):
                msg = str(error_obj.get("message") or "").strip()
                if msg:
                    return msg
                return str(error_obj)[:300]
            if isinstance(error_obj, str) and error_obj.strip():
                return error_obj.strip()
            if str(parsed.get("status") or "").lower() == "error":
                message = str(parsed.get("message") or "").strip()
                if message:
                    return message
        return None

    def _is_transient_tool_error(self, message: str) -> bool:
        lowered = str(message or "").lower()
        transient_markers = (
            "internal server error",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "temporarily unavailable",
            "connection reset",
            "connection closed",
            "timed out",
            "timeout",
            "too many requests",
            "rate limit",
        )
        return any(marker in lowered for marker in transient_markers)

    def _tool_retry_limit(self, tool: dict[str, Any]) -> int:
        raw = tool.get("transient_retry_count")
        if raw is None:
            raw = os.getenv("SKILL_TOOL_TRANSIENT_RETRIES", "2")
        try:
            retries = int(raw)
        except (TypeError, ValueError):
            retries = 2
        return max(0, min(retries, 5))

    def _tool_retry_backoff_seconds(self) -> float:
        raw = os.getenv("SKILL_TOOL_RETRY_BACKOFF_SECONDS", "0.6")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.6
        return max(0.1, min(value, 3.0))

    def _resolve_npm_command(self) -> list[str] | None:
        npm_cmd = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm_cmd:
            return None
        lowered = npm_cmd.lower()
        if os.name == "nt" and (lowered.endswith(".cmd") or lowered.endswith(".bat")):
            return ["cmd", "/c", npm_cmd]
        return [npm_cmd]

    def _prepare_node_runtime(self, tool_dir: Path, *, runtime_env: dict[str, str] | None = None) -> str | None:
        cache_key = str(tool_dir.resolve())
        if cache_key in self._prepared_node_dirs:
            return None

        package_json = tool_dir / "package.json"
        if not package_json.exists():
            self._prepared_node_dirs.add(cache_key)
            return None

        node_modules = tool_dir / "node_modules"
        if node_modules.exists() and node_modules.is_dir():
            self._prepared_node_dirs.add(cache_key)
            return None

        npm_exec = self._resolve_npm_command()
        if not npm_exec:
            return "npm command not found. Please install Node.js/npm first."

        try:
            completed = subprocess.run(
                [*npm_exec, "install", "--no-audit", "--no-fund"],
                capture_output=True,
                text=True,
                cwd=str(tool_dir),
                timeout=240,
                env=runtime_env or self._default_runtime_env(),
            )
        except subprocess.TimeoutExpired:
            return "npm install timed out while preparing node runtime."
        except Exception as error:  # noqa: BLE001
            return f"npm install failed to start: {error}"

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "npm install failed").strip()
            return detail[:1200]

        self._prepared_node_dirs.add(cache_key)
        return None

    def _run_agent_with_tools(
        self,
        agent: AgentDefinition,
        user_input: str,
        system_prompt: str,
        executable_tools: list[dict[str, Any]],
        history: list[dict[str, str]] | None = None,
        trace_hook: ToolTraceHook | None = None,
    ) -> str:
        if self.client is None:
            return self._fallback_agent_response(agent, user_input, system_prompt)

        builtin_tools = self._builtin_filesystem_tools()
        available_tools = [*executable_tools, *builtin_tools]
        filtered_skills, matching_debug = self._select_relevant_tools(user_input, available_tools)
        if trace_hook is not None:
            trace_hook(
                {
                    "stage": "tool_candidates",
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "available_count": len(available_tools),
                    "skill_tool_count": len(executable_tools),
                    "builtin_tool_count": len(builtin_tools),
                    "selected_count": len(filtered_skills),
                    "matching": matching_debug,
                }
            )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if history:
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_input})

        if not filtered_skills:
            response = self.client.chat.completions.create(
                model=agent.model or settings.OPENAI_MODEL,
                temperature=0.2,
                messages=messages,
            )
            return (response.choices[0].message.content or "").strip()

        tool_registry = {item["name"]: item for item in filtered_skills}
        tools_payload = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"] or f"Execute skill {item['skill_name']}",
                    "parameters": item["input_schema"],
                },
            }
            for item in filtered_skills
        ]

        has_non_builtin_tools = any(
            str(item.get("tool_kind") or "") != "builtin"
            for item in filtered_skills
        )
        must_call_tool = has_non_builtin_tools or self._looks_like_filesystem_intent(user_input)

        for _ in range(4):
            response = self.client.chat.completions.create(
                model=agent.model or settings.OPENAI_MODEL,
                temperature=0.2,
                messages=messages,
                tools=tools_payload,
                tool_choice="required" if must_call_tool else "auto",
            )
            message = response.choices[0].message
            tool_calls = list(message.tool_calls or [])

            if not tool_calls:
                if must_call_tool:
                    return (
                        "Tool invocation was required but no tool was called. "
                        "Response is blocked to avoid non-tool fallback."
                    )
                content = (message.content or "").strip()
                if content:
                    return content
                break
            must_call_tool = False

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                function_name = call.function.name
                tool_info = tool_registry.get(function_name, {})
                args_text = call.function.arguments or "{}"
                try:
                    args = json.loads(args_text) if args_text else {}
                    if not isinstance(args, dict):
                        args = {"input": args}
                except json.JSONDecodeError:
                    args = {"raw": args_text}

                if trace_hook is not None:
                    trace_hook(
                        {
                            "stage": "tool_started",
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "tool_call_id": call.id,
                            "tool_name": function_name,
                            "input_keys": sorted(str(key) for key in args.keys()),
                            "skill_id": tool_info.get("skill_id"),
                            "skill_name": tool_info.get("skill_name"),
                        }
                    )

                started_at = time.perf_counter()
                tool_result, tool_meta = self._execute_tool(function_name, args, tool_registry)
                duration_ms = int((time.perf_counter() - started_at) * 1000)

                retry_events = tool_meta.get("retry_events", [])
                if trace_hook is not None and isinstance(retry_events, list):
                    for retry_event in retry_events:
                        if not isinstance(retry_event, dict):
                            continue
                        trace_hook(
                            {
                                "stage": "tool_retry",
                                "agent_id": agent.id,
                                "agent_name": agent.name,
                                "tool_call_id": call.id,
                                "tool_name": function_name,
                                "attempt": retry_event.get("attempt"),
                                "max_attempts": retry_event.get("max_attempts"),
                                "delay_ms": retry_event.get("delay_ms"),
                                "reason": retry_event.get("reason"),
                                "skill_id": tool_meta.get("skill_id"),
                                "skill_name": tool_meta.get("skill_name"),
                            }
                        )

                if trace_hook is not None:
                    trace_hook(
                        {
                            "stage": "tool_finished",
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "tool_call_id": call.id,
                            "tool_name": function_name,
                            "ok": bool(tool_meta.get("ok")),
                            "error": tool_meta.get("error"),
                            "output_dir": tool_meta.get("output_dir"),
                            "generated_files": tool_meta.get("generated_files", []),
                            "duration_ms": duration_ms,
                            "attempt_count": tool_meta.get("attempt_count"),
                            "max_attempts": tool_meta.get("max_attempts"),
                            "result_preview": tool_result[:300],
                            "skill_id": tool_meta.get("skill_id"),
                            "skill_name": tool_meta.get("skill_name"),
                            "auto_provisioned_shell_dependencies": tool_meta.get(
                                "auto_provisioned_shell_dependencies",
                                [],
                            ),
                            "auto_provision_errors": tool_meta.get("auto_provision_errors", []),
                        }
                    )
                if not bool(tool_meta.get("ok")):
                    blocked_message = self._build_tool_blocked_message(
                        function_name=function_name,
                        tool_result=tool_result,
                        tool_meta=tool_meta,
                    )
                    if trace_hook is not None:
                        trace_hook(
                            {
                                "stage": "tool_blocked",
                                "agent_id": agent.id,
                                "agent_name": agent.name,
                                "tool_call_id": call.id,
                                "tool_name": function_name,
                                "reason": tool_meta.get("error") or tool_result,
                                "skill_id": tool_meta.get("skill_id"),
                                "skill_name": tool_meta.get("skill_name"),
                                "attempt_count": tool_meta.get("attempt_count"),
                                "max_attempts": tool_meta.get("max_attempts"),
                                "missing_env_vars": tool_meta.get("missing_env_vars", []),
                                "missing_shell_dependencies": tool_meta.get("missing_shell_dependencies", []),
                                "missing_launchers": tool_meta.get("missing_launchers", []),
                                "auto_provisioned_shell_dependencies": tool_meta.get(
                                    "auto_provisioned_shell_dependencies",
                                    [],
                                ),
                                "auto_provision_errors": tool_meta.get("auto_provision_errors", []),
                            }
                        )
                    return blocked_message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_result,
                    }
                )

        return self._fallback_agent_response(agent, user_input, system_prompt)

    def _execute_tool(
        self,
        function_name: str,
        args: dict[str, Any],
        tool_registry: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        tool = tool_registry.get(function_name)
        if not tool:
            message = f"Tool '{function_name}' is not registered."
            return message, {
                "ok": False,
                "error": message,
                "generated_files": [],
                "output_dir": None,
                "skill_id": None,
                "skill_name": None,
            }

        if str(tool.get("tool_kind") or "skill") == "builtin":
            return self._execute_builtin_tool(function_name, args, tool)
        return self._execute_local_skill_tool(function_name, args, tool_registry)

    def _expand_builtin_path(self, raw_path: str) -> Path:
        value = str(raw_path or "").strip()
        if not value:
            return Path.home()

        lowered = value.lower()
        aliases = {
            "desktop": Path.home() / "Desktop",
            "桌面": Path.home() / "Desktop",
            "downloads": Path.home() / "Downloads",
            "download": Path.home() / "Downloads",
            "下载": Path.home() / "Downloads",
            "~": Path.home(),
        }
        if lowered in aliases:
            return aliases[lowered]

        expanded = Path(value).expanduser()
        if expanded.is_absolute():
            return expanded
        return (Path.cwd() / expanded).resolve()

    def _execute_builtin_tool(
        self,
        function_name: str,
        args: dict[str, Any],
        tool: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        capability = str(tool.get("builtin_capability") or "").strip()
        tool_meta: dict[str, Any] = {
            "ok": False,
            "error": None,
            "generated_files": [],
            "output_dir": None,
            "skill_id": None,
            "skill_name": tool.get("skill_name"),
            "required_env_vars": [],
            "missing_env_vars": [],
            "required_shell_dependencies": [],
            "missing_shell_dependencies": [],
            "auto_provisioned_shell_dependencies": [],
            "auto_provision_errors": [],
            "missing_launchers": [],
            "attempt_count": 1,
            "max_attempts": 1,
            "retry_events": [],
            "builtin_capability": capability,
        }

        try:
            if capability == "fs_list":
                target_path = self._expand_builtin_path(
                    str(args.get("path") or args.get("directory") or "").strip()
                )
                include_hidden = bool(args.get("include_hidden"))
                recursive = bool(args.get("recursive"))
                try:
                    max_entries = int(args.get("max_entries") or 200)
                except (TypeError, ValueError):
                    max_entries = 200
                max_entries = max(1, min(max_entries, 500))

                if not target_path.exists():
                    raise FileNotFoundError(f"Path does not exist: {target_path}")
                if not target_path.is_dir():
                    raise NotADirectoryError(f"Path is not a directory: {target_path}")

                if recursive:
                    iterator = sorted(target_path.rglob("*"))
                else:
                    iterator = sorted(target_path.iterdir())

                entries: list[dict[str, Any]] = []
                for item in iterator:
                    name = item.name
                    if not include_hidden and name.startswith("."):
                        continue
                    try:
                        relative = item.relative_to(target_path).as_posix()
                    except ValueError:
                        relative = name
                    entries.append(
                        {
                            "name": name,
                            "relative_path": relative,
                            "type": "directory" if item.is_dir() else "file",
                            "size": None if item.is_dir() else item.stat().st_size,
                        }
                    )
                    if len(entries) >= max_entries:
                        break

                tool_meta["ok"] = True
                return json.dumps(
                    {
                        "path": str(target_path),
                        "entry_count": len(entries),
                        "recursive": recursive,
                        "entries": entries,
                    },
                    ensure_ascii=False,
                    indent=2,
                ), tool_meta

            if capability == "fs_read":
                target_path = self._expand_builtin_path(str(args.get("path") or "").strip())
                try:
                    max_chars = int(args.get("max_chars") or 12000)
                except (TypeError, ValueError):
                    max_chars = 12000
                max_chars = max(200, min(max_chars, 40000))

                if not target_path.exists():
                    raise FileNotFoundError(f"File does not exist: {target_path}")
                if not target_path.is_file():
                    raise IsADirectoryError(f"Path is not a file: {target_path}")

                content = target_path.read_text(encoding="utf-8", errors="replace")
                truncated = len(content) > max_chars
                tool_meta["ok"] = True
                return json.dumps(
                    {
                        "path": str(target_path),
                        "truncated": truncated,
                        "content": content[:max_chars],
                    },
                    ensure_ascii=False,
                    indent=2,
                ), tool_meta

            if capability == "fs_write":
                target_path = self._expand_builtin_path(str(args.get("path") or "").strip())
                content = str(args.get("content") or "")
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(content, encoding="utf-8")
                tool_meta["ok"] = True
                return json.dumps(
                    {
                        "path": str(target_path),
                        "written_chars": len(content),
                    },
                    ensure_ascii=False,
                    indent=2,
                ), tool_meta

            raise ValueError(f"Unsupported builtin capability: {capability}")
        except Exception as error:  # noqa: BLE001
            message = str(error).strip() or f"Builtin tool '{function_name}' failed."
            tool_meta["error"] = message[:1200]
            return f"Tool '{function_name}' failed: {message[:1200]}", tool_meta

    def _build_tool_blocked_message(
        self,
        *,
        function_name: str,
        tool_result: str,
        tool_meta: dict[str, Any],
    ) -> str:
        reason = str(tool_meta.get("error") or tool_result or "Unknown tool error").strip()
        skill_name = str(tool_meta.get("skill_name") or "").strip()
        missing_env = tool_meta.get("missing_env_vars") or []
        missing_deps = tool_meta.get("missing_shell_dependencies") or []
        missing_launchers = tool_meta.get("missing_launchers") or []
        auto_provisioned = tool_meta.get("auto_provisioned_shell_dependencies") or []
        auto_provision_errors = tool_meta.get("auto_provision_errors") or []
        attempt_count = int(tool_meta.get("attempt_count") or 1)
        max_attempts = int(tool_meta.get("max_attempts") or 1)

        lines = [
            "Tool execution failed; response generation is blocked to avoid fabricated output.",
            f"Tool: {function_name}",
        ]
        if skill_name:
            lines.append(f"Skill: {skill_name}")
        if max_attempts > 1:
            lines.append(f"Attempts: {attempt_count}/{max_attempts}")
        lines.append(f"Reason: {reason}")
        if missing_launchers:
            lines.append(f"Missing launchers: {', '.join(str(item) for item in missing_launchers)}")
        if missing_deps:
            lines.append(f"Missing shell dependencies: {', '.join(str(item) for item in missing_deps)}")
        if missing_env:
            lines.append(f"Missing environment variables: {', '.join(str(item) for item in missing_env)}")
        if auto_provisioned:
            lines.append(f"Auto-provisioned shell dependencies: {', '.join(str(item) for item in auto_provisioned)}")
        if auto_provision_errors:
            lines.append(f"Auto-provision errors: {'; '.join(str(item) for item in auto_provision_errors)}")
        return "\n".join(lines)

    def _execute_local_skill_tool(
        self,
        function_name: str,
        args: dict[str, Any],
        tool_registry: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        tool = tool_registry.get(function_name)
        if not tool:
            message = f"Tool '{function_name}' is not registered."
            return message, {
                "ok": False,
                "error": message,
                "generated_files": [],
                "output_dir": None,
                "skill_id": None,
                "skill_name": None,
            }

        if str(tool.get("tool_kind") or "").strip() == "builtin":
            return self._execute_builtin_tool(function_name, args, tool)

        if str(tool.get("execution_mode") or "").strip() == "builtin_fs":
            return self._execute_builtin_filesystem_tool(function_name, args, tool)

        cwd = tool["local_path"]
        command = list(tool["command"])
        command = self._resolve_runtime_command(command)
        timeout_seconds = max(1, int(tool["timeout_seconds"]))
        input_mode = str(tool.get("input_mode") or "stdin_json").strip() or "stdin_json"
        output_dir = str(args.get("output_dir") or "").strip() or str(tool.get("default_output_dir") or "").strip()
        tool_meta: dict[str, Any] = {
            "ok": False,
            "error": None,
            "generated_files": [],
            "output_dir": output_dir or None,
            "skill_id": tool.get("skill_id"),
            "skill_name": tool.get("skill_name"),
            "required_env_vars": [],
            "missing_env_vars": [],
            "required_shell_dependencies": [],
            "missing_shell_dependencies": [],
            "auto_provisioned_shell_dependencies": [],
            "auto_provision_errors": [],
            "missing_launchers": [],
            "attempt_count": 0,
            "max_attempts": 1,
            "retry_events": [],
        }

        if not command:
            message = f"Tool '{function_name}' has no command."
            tool_meta["error"] = message
            return message, tool_meta
        tool_dir = Path(cwd)
        if not tool_dir.exists() or not tool_dir.is_dir():
            message = f"Tool '{function_name}' path is invalid: {cwd}"
            tool_meta["error"] = message
            return message, tool_meta
        if not self._is_command_runnable(tool_dir, command):
            message = f"Tool '{function_name}' command target is missing."
            tool_meta["error"] = message
            return message, tool_meta

        runtime_env = self._build_runtime_env(tool_dir=tool_dir)
        missing_launchers = self._missing_command_launchers(command)
        tool_meta["missing_launchers"] = missing_launchers
        if missing_launchers:
            joined = ", ".join(missing_launchers)
            message = (
                f"Tool '{function_name}' missing command launcher(s): {joined}. "
                "Please install them and expose in PATH. "
                "On Unix you can run backend/scripts/bootstrap-runtime.sh."
            )
            tool_meta["error"] = message
            return message, tool_meta

        required_deps = self._detect_shell_dependencies(tool, command)
        tool_meta["required_shell_dependencies"] = required_deps
        missing_deps = self._missing_shell_dependencies(tool, command, env_map=runtime_env)
        if missing_deps:
            missing_deps, auto_provisioned, auto_provision_errors = self._auto_provision_shell_dependencies(
                missing_deps,
                runtime_env=runtime_env,
            )
            tool_meta["auto_provisioned_shell_dependencies"] = auto_provisioned
            tool_meta["auto_provision_errors"] = auto_provision_errors
        tool_meta["missing_shell_dependencies"] = missing_deps
        if missing_deps:
            joined = ", ".join(missing_deps)
            message = (
                f"Tool '{function_name}' missing required shell dependencies: {joined}. "
                "Please install them in the runtime environment. "
                "On Unix you can run backend/scripts/bootstrap-runtime.sh."
            )
            tool_meta["error"] = message
            return message, tool_meta

        required_env = self._detect_required_env_vars(tool, command)
        tool_meta["required_env_vars"] = required_env
        missing_env = self._missing_required_env_vars(tool, command, env_map=runtime_env)
        tool_meta["missing_env_vars"] = missing_env
        if missing_env:
            joined = ", ".join(missing_env)
            message = (
                f"Tool '{function_name}' requires environment variables: {joined}. "
                "Please configure them in backend runtime environment."
            )
            tool_meta["error"] = message
            return message, tool_meta

        first = command[0]
        first_lower = first.lower()
        first_base = Path(first_lower).name
        if self._is_node_launcher(first):
            prepare_error = self._prepare_node_runtime(tool_dir, runtime_env=runtime_env)
            if prepare_error:
                message = f"Tool '{function_name}' runtime preparation failed: {prepare_error}"
                tool_meta["error"] = prepare_error
                return message, tool_meta
        python_exec = sys.executable
        if first_lower.endswith(".py") or self._is_python_launcher(first):
            prepared_python, prepare_error = self._prepare_python_runtime(
                tool_dir=tool_dir,
                runtime_env=runtime_env,
            )
            if prepare_error:
                message = f"Tool '{function_name}' runtime preparation failed: {prepare_error}"
                tool_meta["error"] = prepare_error
                return message, tool_meta
            if prepared_python:
                python_exec = prepared_python
                venv_bin = str(Path(python_exec).parent)
                current_path = str(runtime_env.get("PATH") or "")
                if venv_bin and venv_bin not in current_path:
                    runtime_env["PATH"] = (
                        f"{venv_bin}{os.pathsep}{current_path}" if current_path else venv_bin
                    )
        if first_lower.endswith(".py"):
            command = [python_exec, command[0], *command[1:]]
        elif self._is_python_launcher(first):
            command[0] = python_exec

        stdin_data: str | None = None
        argv_json_payload: str | None = None
        if input_mode == "argv_content":
            command = self._build_argv_command(command=command, args=args, tool=tool)
        elif input_mode == "argv_json":
            command = self._build_argv_json_command(command=command, args=args)
            if command:
                argv_json_payload = str(command[-1])
        else:
            stdin_data = json.dumps(args, ensure_ascii=False)

        inlined_script: str | None = None
        if first_base in {"bash", "bash.exe", "sh", "sh.exe"}:
            if input_mode == "argv_json" and argv_json_payload is not None:
                runtime_env["SKILL_JSON_INPUT_B64"] = base64.b64encode(
                    argv_json_payload.encode("utf-8")
                ).decode("ascii")
                # For shell JSON mode, pass payload through base64 env and inject as $1
                # to avoid platform-specific argv quoting/encoding issues.
                command = command[:-1]
            command, inlined_script = self._inline_shell_script(tool_dir, command)
            if inlined_script is not None and stdin_data is None:
                if input_mode == "argv_json" and argv_json_payload is not None:
                    inlined_script = (
                        "if [ -n \"${SKILL_JSON_INPUT_B64:-}\" ]; then\n"
                        "  _skill_json_input=\"$(printf '%s' \"$SKILL_JSON_INPUT_B64\" | base64 -d 2>/dev/null || printf '%s' \"$SKILL_JSON_INPUT_B64\" | base64 --decode 2>/dev/null)\"\n"
                        "  set -- \"$_skill_json_input\" \"$@\"\n"
                        "fi\n\n"
                        + inlined_script
                    )
                export_lines: list[str] = []
                for key in required_env:
                    value = runtime_env.get(key, "")
                    if not str(value).strip():
                        continue
                    export_lines.append(f"export {key}={shlex.quote(str(value))}")
                if export_lines:
                    inlined_script = "\n".join([*export_lines, "", inlined_script])
                stdin_data = inlined_script

        run_text_mode = True
        run_input: str | bytes | None = stdin_data
        if inlined_script is not None and stdin_data is not None:
            run_text_mode = False
            run_input = stdin_data.encode("utf-8")

        retry_limit = self._tool_retry_limit(tool)
        max_attempts = max(1, 1 + retry_limit)
        backoff_base_seconds = self._tool_retry_backoff_seconds()
        tool_meta["max_attempts"] = max_attempts
        retry_events: list[dict[str, Any]] = []
        stdout = ""
        stderr = ""
        last_error_text: str | None = None

        for attempt in range(1, max_attempts + 1):
            tool_meta["attempt_count"] = attempt
            try:
                completed = subprocess.run(
                    command,
                    input=run_input,
                    capture_output=True,
                    text=run_text_mode,
                    cwd=str(tool_dir),
                    timeout=timeout_seconds,
                    env=runtime_env,
                )
            except subprocess.TimeoutExpired:
                error_text = f"Tool '{function_name}' timed out after {timeout_seconds}s."
                last_error_text = error_text
                if attempt < max_attempts and self._is_transient_tool_error(error_text):
                    delay_ms = int(backoff_base_seconds * (2 ** (attempt - 1)) * 1000)
                    retry_events.append(
                        {
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "delay_ms": delay_ms,
                            "reason": error_text[:300],
                        }
                    )
                    time.sleep(delay_ms / 1000)
                    continue
                tool_meta["error"] = error_text
                tool_meta["retry_events"] = retry_events
                return error_text, tool_meta
            except Exception as error:  # noqa: BLE001
                error_text = str(error).strip() or f"Tool '{function_name}' execution failed."
                last_error_text = error_text
                if attempt < max_attempts and self._is_transient_tool_error(error_text):
                    delay_ms = int(backoff_base_seconds * (2 ** (attempt - 1)) * 1000)
                    retry_events.append(
                        {
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "delay_ms": delay_ms,
                            "reason": error_text[:300],
                        }
                    )
                    time.sleep(delay_ms / 1000)
                    continue
                tool_meta["error"] = error_text[:1200]
                tool_meta["retry_events"] = retry_events
                return f"Tool '{function_name}' execution failed: {error_text[:1200]}", tool_meta

            if run_text_mode:
                stdout = (completed.stdout or "").strip()
                stderr = (completed.stderr or "").strip()
            else:
                stdout = bytes(completed.stdout or b"").decode("utf-8", errors="replace").strip()
                stderr = bytes(completed.stderr or b"").decode("utf-8", errors="replace").strip()

            if completed.returncode != 0:
                error_text = (stderr or stdout or f"exit code {completed.returncode}").strip()
                last_error_text = error_text
                if attempt < max_attempts and self._is_transient_tool_error(error_text):
                    delay_ms = int(backoff_base_seconds * (2 ** (attempt - 1)) * 1000)
                    retry_events.append(
                        {
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "delay_ms": delay_ms,
                            "reason": error_text[:300],
                        }
                    )
                    time.sleep(delay_ms / 1000)
                    continue
                tool_meta["error"] = error_text[:1200]
                tool_meta["retry_events"] = retry_events
                return f"Tool '{function_name}' failed: {error_text[:1200]}", tool_meta

            structured_error = self._extract_structured_tool_error(stdout) or self._extract_structured_tool_error(stderr)
            if structured_error:
                last_error_text = structured_error
                if attempt < max_attempts and self._is_transient_tool_error(structured_error):
                    delay_ms = int(backoff_base_seconds * (2 ** (attempt - 1)) * 1000)
                    retry_events.append(
                        {
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "delay_ms": delay_ms,
                            "reason": structured_error[:300],
                        }
                    )
                    time.sleep(delay_ms / 1000)
                    continue
                tool_meta["error"] = structured_error[:1200]
                tool_meta["retry_events"] = retry_events
                return f"Tool '{function_name}' failed: {structured_error[:1200]}", tool_meta
            break
        else:
            fallback_error = (last_error_text or f"Tool '{function_name}' failed.").strip()
            tool_meta["error"] = fallback_error[:1200]
            tool_meta["retry_events"] = retry_events
            return f"Tool '{function_name}' failed: {fallback_error[:1200]}", tool_meta

        generated_files: list[str] = []
        if output_dir:
            output_path = Path(output_dir)
            if not output_path.is_absolute():
                output_path = tool_dir / output_path
            if output_path.exists() and output_path.is_dir():
                for file_path in sorted(output_path.rglob("*")):
                    if not file_path.is_file():
                        continue
                    try:
                        relative = file_path.relative_to(output_path).as_posix()
                    except ValueError:
                        relative = file_path.name
                    generated_files.append(relative)
                    if len(generated_files) >= 12:
                        break
        tool_meta["retry_events"] = retry_events
        tool_meta["generated_files"] = generated_files
        tool_meta["ok"] = True

        if stdout:
            result_text = stdout[:1200]
            if generated_files:
                listing = "\n".join(f"- {name}" for name in generated_files)
                return f"{result_text}\n\nGenerated files in {output_dir}:\n{listing}", tool_meta
            return result_text, tool_meta
        if stderr:
            return stderr[:1200], tool_meta
        if generated_files:
            listing = "\n".join(f"- {name}" for name in generated_files)
            return f"Tool '{function_name}' completed.\nGenerated files in {output_dir}:\n{listing}", tool_meta
        return f"Tool '{function_name}' completed.", tool_meta

    def _fallback_agent_response(
        self,
        agent: AgentDefinition,
        user_input: str,
        system_prompt: str,
    ) -> str:
        return (
            f"[演示模式] {agent.name} 正在处理用户请求。\n"
            f"角色说明：{agent.description}\n"
            f"有效系统提示词（含 skills 注入）：\n{system_prompt}\n"
            f"用户请求：{user_input}\n"
            "这里是一个占位回复；配置 OPENAI_API_KEY 后会切换成真实模型输出。"
        )

    def _parse_task_list(self, content: str, max_tasks: int) -> list[str]:
        text = content.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()

        parsed: object
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            bracket = re.search(r"\[.*\]", text, flags=re.DOTALL)
            if not bracket:
                return []
            try:
                parsed = json.loads(bracket.group(0))
            except json.JSONDecodeError:
                return []

        items: list[str] = []
        if isinstance(parsed, list):
            items = [str(item).strip() for item in parsed if str(item).strip()]
        elif isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
            items = [str(item).strip() for item in parsed["tasks"] if str(item).strip()]

        return items[:max_tasks]

    def _fallback_plan_tasks(self, user_input: str, max_tasks: int = 4) -> list[str]:
        normalized = user_input
        normalized = normalized.replace("\uff0c", ",").replace("\u3001", ",")
        normalized = normalized.replace("\uff1b", ";").replace("\u3002", ";")
        normalized = re.sub(
            r"(\u7136\u540e|\u63a5\u7740|\u6700\u540e|\u5e76\u4e14|\u540c\u65f6|\u53e6\u5916)",
            ";",
            normalized,
        )
        chunks = re.split(r"[\n;,]+", normalized)
        tasks: list[str] = []
        for chunk in chunks:
            segment = chunk.strip()
            if not segment:
                continue
            numbered_parts = re.split(r"(?:^|\s)(?:\d+[.)]\s+|[-*]\s+)", segment)
            extracted = [part.strip() for part in numbered_parts if part.strip()]
            if extracted:
                tasks.extend(extracted)
            else:
                tasks.append(segment)
        if not tasks:
            return [user_input.strip()]
        return tasks[:max_tasks]

    def _parse_supervisor_decision(self, content: str) -> tuple[bool, str, str] | None:
        text = str(content or "").strip()
        if not text:
            return None

        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()

        parsed: object
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            brace = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not brace:
                return None
            try:
                parsed = json.loads(brace.group(0))
            except json.JSONDecodeError:
                return None

        if not isinstance(parsed, dict):
            return None

        raw_continue = parsed.get("continue")
        if isinstance(raw_continue, bool):
            should_continue = raw_continue
        elif isinstance(raw_continue, str):
            normalized = raw_continue.strip().lower()
            should_continue = normalized in {"true", "yes", "y", "1", "continue"}
        else:
            should_continue = bool(raw_continue)

        next_focus_task = str(parsed.get("next_focus_task") or parsed.get("next_focus") or "").strip()
        reason = str(parsed.get("reason") or parsed.get("decision_reason") or "").strip()
        return should_continue, next_focus_task, reason

    def _fallback_supervisor_review_decision(
        self,
        *,
        user_input: str,
        reports: list[str],
        cycle: int,
        max_cycles: int,
    ) -> tuple[bool, str, str]:
        if cycle >= max_cycles:
            return False, "", "Reached max cycle limit."

        request = str(user_input or "").strip()
        latest = str(reports[-1] if reports else "").lower()
        complete_markers = ("final", "complete", "done", "conclusion", "最终", "结论", "已完成")
        unresolved_markers = ("todo", "unknown", "risk", "assumption", "待补充", "未知", "风险", "假设")

        if cycle < 2 and len(request) >= 24:
            return True, "补充约束条件、边界场景与验收标准。", "Fallback: run at least two cycles for non-trivial requests."
        if any(token in latest for token in unresolved_markers):
            return True, "针对未解决项继续补充可执行细节。", "Fallback: latest report indicates unresolved items."
        if any(token in latest for token in complete_markers):
            return False, "", "Fallback: latest report appears complete."
        return False, "", "Fallback: no strong signal to continue."


llm_gateway = LLMGateway()
