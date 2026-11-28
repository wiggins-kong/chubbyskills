#!/usr/bin/env python3
"""Copy portable Agent Skills, including their repository-local dependencies.

This only installs source files. It never installs Python packages or replaces an
existing skill. The resulting skill directories can be moved independently.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON_SKILLS = {
    "bilibili-transcribe", "content-enrich", "douyin-transcribe",
    "knowledge-base-management", "learning-notes-automation", "podcast-transcribe",
    "tiktok-transcribe", "wechat-article-ingest", "weibo-transcribe",
    "x-ingest",
    "xiaohongshu-ingest", "youtube-transcribe", "zhihu-transcribe",
}
SUPPORTED_SKILLS = COMMON_SKILLS | {
    "industry-intelligence-radar",
}
DEFAULT_BUNDLE_NAME = "chubby"
WORKFLOW_REFERENCE_SKILLS = (
    "content-enrich",
    "knowledge-base-management",
    "learning-notes-automation",
    "industry-intelligence-radar",
)
BUNDLE_TOOL_FILES = (
    "__init__.py",
    "chubby_ingest.py",
    "platform_health.py",
    "source_identity.py",
    "import_document.py",
    "vault_index.py",
    "vault_curator.py",
    "evidence_brief.py",
)
IGNORED_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".DS_Store", ".chubby", "output", "outputs", "runs",
}


def ignored(name):
    return name in IGNORED_NAMES or name.startswith(".env") or name.endswith((".pyc", ".pyo"))


def payload_files(source):
    """Reject links instead of copying files from outside the source tree."""
    if source.is_symlink():
        raise ValueError(f"Symlink source is not supported: {source}")
    if source.is_file():
        return [(source, Path(source.name))]
    if not source.is_dir():
        raise ValueError(f"Missing source: {source}")
    result = []
    for child in sorted(source.iterdir()):
        if ignored(child.name):
            continue
        if child.is_symlink():
            raise ValueError(f"Symlink source is not supported: {child}")
        if child.is_dir():
            result.extend((path, Path(child.name) / rel) for path, rel in payload_files(child))
        elif child.is_file():
            result.append((child, Path(child.name)))
        else:
            raise ValueError(f"Unsupported source file: {child}")
    return result


def _strip_frontmatter(text):
    return re.sub(r"\A---\r?\n.*?\r?\n---\r?\n*", "", text, count=1, flags=re.DOTALL)


def bundle_skill_markdown(bundle_name, version):
    """Render concise root SKILL.md for the unified chubby bundle."""
    return (
        f"---\n"
        f"name: {bundle_name}\n"
        f'description: "统一内容采集与本地知识库工作流：通过 tools/chubby_ingest.py 自动识别平台并采集为 Obsidian Markdown，支持摘要加工、学习笔记、行业情报雷达与知识库管理。"\n'
        f"license: MIT\n"
        f"metadata:\n"
        f'  version: "{version}"\n'
        f"---\n\n"
        f"# {bundle_name} — 统一采集与知识库技能包\n\n"
        f"## 1. 统一内容采集入口\n\n"
        f"所有链接、BV 号或本地音视频/PDF/Markdown 均优先通过统一入口执行：\n\n"
        f"```bash\n"
        f'python3 tools/chubby_ingest.py "<source>" -o output/\n'
        f'python3 tools/chubby_ingest.py "<source>" --enrich --vault /path/to/vault/00_Inbox\n'
        f"```\n\n"
        f"- **自动路由**：`detect_skill()` 结合 `platforms/*.yaml` 自动匹配采集脚本或本地文档导入（`tools/import_document.py`）。\n"
        f"- **摘要加工**：传 `--enrich` 在采集后自动调用 `content-enrich/scripts/enrich.py` 写入 `summary`、`auto_tags` 与摘要区块。\n"
        f"- **桌面 Chromium CDP 兜底**：需要登录或触发拦截时，自动复用本机 Edge/Chrome + `websocket-client` 与站点独立本地 profile；无头失败时切有头窗口等待登录/验证（默认 180 秒）。新机器可用 `python3 douyin-transcribe/scripts/bootstrap.py` 检查浏览器与依赖。\n"
        f"- **知乎纯问题链接**：遇到仅含 `/question/<qid>`、缺少 `/answer/<aid>` 的知乎链接时，`zhihu-transcribe/scripts/transcribe.py` 会直接提示并退出（不抓不写文件），请向用户索取具体回答链接（`/question/<qid>/answer/<aid>`）或专栏链接（`zhuanlan.zhihu.com/p/<pid>`）。\n\n"
        f"## 2. 按需加载工作流文档（`references/*.md`）\n\n"
        f"- `references/knowledge-base-management.md`：Obsidian 三层架构、入库归档、健康检查（`knowledge-base-management/scripts/vault_health_check.py`）、索引/简报（`tools/vault_index.py`、`tools/vault_curator.py`、`tools/evidence_brief.py`、`tools/import_document.py`）与 MCP（`knowledge-base-management/scripts/mcp_server.py`）。\n"
        f"- `references/content-enrich.md`：对已有笔记或目录单篇/批量补充摘要、要点与标签（`content-enrich/scripts/enrich.py`）。\n"
        f"- `references/learning-notes-automation.md`：从转录稿提炼知识点并生成 Anki 闪卡（`learning-notes-automation/scripts/make_notes.py`）。\n"
        f"- `references/industry-intelligence-radar.md`：Hacker News / V2EX / RSS 多源行业情报扫描（`industry-intelligence-radar/scripts/scan.py`）。\n"
    )


def skill_payload(name, repo_root):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or name not in SUPPORTED_SKILLS:
        raise ValueError(f"Unknown skill: {name}")
    skill = repo_root / name
    if not (skill / "SKILL.md").is_file():
        raise ValueError(f"Missing SKILL.md: {skill}")
    files = payload_files(skill)
    if name in COMMON_SKILLS:
        files.extend((path, Path("chubby_common") / rel) for path, rel in payload_files(repo_root / "chubby_common"))
    if name == "knowledge-base-management":
        for module in ("vault_index.py", "vault_curator.py", "evidence_brief.py", "import_document.py"):
            files.extend((path, Path("tools") / rel) for path, rel in payload_files(repo_root / "tools" / module))
    if not (skill / "LICENSE").exists():
        files.extend(payload_files(repo_root / "LICENSE"))
    return files


def bundle_payload(repo_root):
    """Collect all files for the unified bundle, omitting nested SKILL.md files."""
    files = []
    for name in sorted(SUPPORTED_SKILLS):
        skill = repo_root / name
        if not (skill / "SKILL.md").is_file():
            raise ValueError(f"Missing SKILL.md: {skill}")
        files.extend(
            (path, Path(name) / rel)
            for path, rel in payload_files(skill)
            if rel != Path("SKILL.md")
        )
    files.extend((path, Path("chubby_common") / rel) for path, rel in payload_files(repo_root / "chubby_common"))
    files.extend((path, Path("platforms") / rel) for path, rel in payload_files(repo_root / "platforms"))
    for module in BUNDLE_TOOL_FILES:
        files.extend((path, Path("tools") / rel) for path, rel in payload_files(repo_root / "tools" / module))
    files.extend(payload_files(repo_root / "LICENSE"))

    references = {}
    for wf in WORKFLOW_REFERENCE_SKILLS:
        doc = repo_root / wf / "SKILL.md"
        if doc.is_symlink():
            raise ValueError(f"Symlink source is not supported: {doc}")
        if not doc.is_file():
            raise ValueError(f"Missing SKILL.md: {doc.parent}")
        references[Path("references") / f"{wf}.md"] = _strip_frontmatter(doc.read_text(encoding="utf-8"))
    return files, references


def _validate_destination(destination, source_roots):
    destination = Path(destination).expanduser()
    if destination.is_symlink():
        raise ValueError(f"Destination must not be a symlink: {destination}")
    destination = destination.resolve()
    for source_root in source_roots:
        if destination == source_root or source_root in destination.parents:
            raise ValueError(f"Destination is inside a source directory: {destination}")
    return destination


def _stage_files(stage_dir, files, extra_texts=()):
    stage_dir.mkdir()
    manifest_files = []
    for source, relative in files:
        target = stage_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest_files.append(str(relative))
    for relative, content in extra_texts:
        target = stage_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        manifest_files.append(str(relative))
    return sorted(manifest_files)


def _prune_legacy_dir(path, preserved):
    """Remove legacy skill contents recursively while preserving any .venv/venv."""
    if path.name in {".venv", "venv"}:
        preserved.append(path)
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        kept = False
        for child in list(path.iterdir()):
            if not _prune_legacy_dir(child, preserved):
                kept = True
        if not kept and not any(path.iterdir()):
            path.rmdir()
            return True
        return False
    return True


def clean_legacy_skills(destination, skip=()):
    """Remove known legacy skill directories under destination, preserving nested venvs."""
    removed = []
    preserved = []
    skip_set = set(skip)
    for name in sorted(SUPPORTED_SKILLS):
        if name in skip_set:
            continue
        legacy = destination / name
        if not legacy.exists() and not legacy.is_symlink():
            continue
        _prune_legacy_dir(legacy, preserved)
        removed.append(name)
    for venv_path in preserved:
        print(
            f"Warning: preserved virtual environment in legacy skill directory: {venv_path}",
            file=sys.stderr,
        )
    return removed, preserved


def install_skills(names, destination, repo_root=ROOT):
    """Validate all inputs before copying; never merge into an existing skill."""
    repo_root = Path(repo_root).resolve()
    names = list(dict.fromkeys(names))
    if not names:
        raise ValueError("Select at least one skill")
    source_roots = [repo_root / "chubby_common", repo_root / "tools", *(repo_root / n for n in names)]
    destination = _validate_destination(destination, source_roots)
    plans = {}
    for name in names:
        files = skill_payload(name, repo_root)
        target = destination / name
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Skill already exists; nothing overwritten: {target}")
        plans[name] = files

    version = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    with tempfile.TemporaryDirectory(prefix=".chubby-install-", dir=destination) as temporary:
        staging = Path(temporary)
        for name, files in plans.items():
            skill_stage = staging / name
            manifest_files = _stage_files(skill_stage, files)
            manifest = {"skill": name, "version": version, "files": manifest_files}
            (skill_stage / "installation.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        try:
            for name in names:
                target = destination / name
                # Exclusive creation also catches a target created since preflight.
                target.mkdir()
                installed.append(target)
                shutil.copytree(staging / name, target, dirs_exist_ok=True)
        except Exception:
            for target in installed:
                shutil.rmtree(target)
            raise
    return installed


def install_bundle(destination, bundle_name=DEFAULT_BUNDLE_NAME, repo_root=ROOT, clean_legacy=False):
    """Install all supported skills as one self-contained unified skill bundle."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", bundle_name) or len(bundle_name) > 64:
        raise ValueError(f"Invalid bundle name: {bundle_name}")
    repo_root = Path(repo_root).resolve()
    source_roots = [
        repo_root / "chubby_common",
        repo_root / "platforms",
        repo_root / "tools",
        *(repo_root / n for n in SUPPORTED_SKILLS),
    ]
    destination = _validate_destination(destination, source_roots)
    target = destination / bundle_name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Skill already exists; nothing overwritten: {target}")

    files, references = bundle_payload(repo_root)
    version = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    extra_texts = [(Path("SKILL.md"), bundle_skill_markdown(bundle_name, version))]
    extra_texts.extend(sorted(references.items(), key=lambda item: str(item[0])))

    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".chubby-bundle-", dir=destination) as temporary:
        bundle_stage = Path(temporary) / bundle_name
        manifest_files = _stage_files(bundle_stage, files, extra_texts=extra_texts)
        manifest = {
            "skill": bundle_name,
            "bundle": True,
            "skills": sorted(SUPPORTED_SKILLS),
            "version": version,
            "files": manifest_files,
        }
        (bundle_stage / "installation.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        created = False
        try:
            target.mkdir()
            created = True
            shutil.copytree(bundle_stage, target, dirs_exist_ok=True)
        except Exception:
            if created:
                shutil.rmtree(target)
            raise
    if clean_legacy:
        clean_legacy_skills(destination, skip={bundle_name})
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install portable skills with bundled local dependencies")
    parser.add_argument("skills", nargs="*", help="Full skill directory names (use --list to see them)")
    parser.add_argument("--all", action="store_true", help="Install all supported skills")
    parser.add_argument("--bundle", action="store_true", help="Install all skills as a single unified bundle")
    parser.add_argument(
        "--bundle-name",
        default=DEFAULT_BUNDLE_NAME,
        help=f"Directory and skill name for --bundle (default: {DEFAULT_BUNDLE_NAME})",
    )
    parser.add_argument(
        "--clean-legacy",
        action="store_true",
        help="Remove legacy individual skill directories under --dest (preserves nested .venv/venv)",
    )
    parser.add_argument(
        "--dest",
        "--target",
        dest="dest",
        help="Agent skills directory; no existing skill is overwritten",
    )
    parser.add_argument("--list", action="store_true", help="List supported skill names")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(sorted(SUPPORTED_SKILLS)))
        return 0
    if args.clean_legacy and not args.bundle:
        parser.error("--clean-legacy requires --bundle")
    if args.bundle and (args.all or args.skills):
        parser.error("Use --bundle alone, not with --all or skill names")
    if args.all and args.skills:
        parser.error("Use skill names or --all, not both")
    if not args.dest or (not args.bundle and not args.all and not args.skills):
        parser.error("Select skill names, --all, or --bundle and provide --dest")
    try:
        if args.bundle:
            installed = [install_bundle(args.dest, bundle_name=args.bundle_name, clean_legacy=args.clean_legacy)]
        else:
            installed = install_skills(sorted(SUPPORTED_SKILLS) if args.all else args.skills, args.dest)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Installation failed: {exc}\n")
    for path in installed:
        print(f"Installed {path.name}: {path}")
    print("Source files installed. Install optional runtime dependencies separately with setup.sh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
