"""Explicit update checker and self-upgrader for LectureWeaver and protocol libraries.

Never runs in the background during ordinary commands: invoked only by
``lw update`` and ``lw doctor``.
"""

from __future__ import annotations

import concurrent.futures
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__
from .config import outbound_proxy
from .notebooklm import with_outbound_proxy

GITHUB_REPO = "majianyu2007/lectureweaver"
CRITICAL_DEPS = ("nwafu-cas", "notebooklm-py")


class UpdateError(Exception):
    """User-facing error during ``lw update``."""


def _ver_key(v: str) -> tuple:
    """Comparable key for standard PEP 440 versions without requiring ``packaging``."""
    v = v.strip().lstrip("vV").split("+", 1)[0]
    m = re.match(r"^(\d+(?:\.\d+)*)(?:[-._]?(a|alpha|b|beta|rc|c|pre|post)(\d*))?", v, re.I)
    if not m:
        return ((0,), 0, 0, v)
    nums = tuple(int(x) for x in m.group(1).split("."))
    # Normalize trailing zeros: 1.0 == 1.0.0
    while len(nums) > 1 and nums[-1] == 0:
        nums = nums[:-1]
    tag = (m.group(2) or "").lower()
    tag_num = int(m.group(3) or 0)
    stage_rank = {
        "a": -3, "alpha": -3,
        "b": -2, "beta": -2,
        "rc": -1, "c": -1, "pre": -1,
        "": 0,
        "post": 1,
    }.get(tag, 0)
    return (nums, stage_rank, tag_num, "")


def is_newer(remote_version: str, local_version: str) -> bool:
    return _ver_key(remote_version) > _ver_key(local_version)


def _git_out(repo: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        r = subprocess.run(
            [git, "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def detect_lw_install() -> dict[str, Any]:
    """Inspect how ``lectureweaver`` is installed in the current Python runtime."""
    repo_candidate = Path(__file__).resolve().parents[2]
    editable_dir: Path | None = None

    try:
        dist = importlib.metadata.distribution("lectureweaver")
        raw_direct = dist.read_text("direct_url.json")
        if raw_direct:
            info = json.loads(raw_direct)
            url = info.get("url", "")
            if url.startswith("file://"):
                from urllib.parse import unquote, urlparse

                parsed = urlparse(url)
                p_str = unquote(parsed.path)
                if sys.platform == "win32" and p_str.startswith("/") and len(p_str) > 2 and p_str[2] == ":":
                    p_str = p_str[1:]
                editable_dir = Path(p_str)
    except Exception:  # noqa: BLE001
        pass

    if editable_dir is None and (repo_candidate / ".git").exists() and (repo_candidate / "pyproject.toml").is_file():
        editable_dir = repo_candidate

    if editable_dir and (editable_dir / ".git").exists():
        head = _git_out(editable_dir, "rev-parse", "HEAD")
        branch = _git_out(editable_dir, "rev-parse", "--abbrev-ref", "HEAD") or "main"
        dirty_out = _git_out(editable_dir, "status", "--porcelain", "--untracked-files=no")
        return {
            "mode": "editable_git",
            "version": __version__,
            "repo_dir": str(editable_dir),
            "branch": branch,
            "head": head,
            "short_head": head[:7] if head else None,
            "dirty": bool(dirty_out),
        }

    return {
        "mode": "package",
        "version": __version__,
        "repo_dir": None,
        "branch": None,
        "head": None,
        "short_head": None,
        "dirty": False,
    }


def _check_lw_remote(install_info: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"LectureWeaver/{__version__}"}
    out = {**install_info, "checked": False, "update_available": False, "remote_ref": None, "summary": "", "error": None}
    try:
        with httpx.Client(proxy=outbound_proxy(), timeout=timeout, headers=headers, follow_redirects=True) as hc:
            if install_info["mode"] == "editable_git" and install_info.get("head"):
                branch = install_info["branch"] if install_info.get("branch") and install_info["branch"] != "HEAD" else "main"
                r = hc.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{branch}")
                if r.status_code == 404 and branch != "main":
                    branch = "main"
                    r = hc.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/main")
                r.raise_for_status()
                commit_data = r.json()
                remote_sha = commit_data["sha"]
                msg_first_line = ((commit_data.get("commit") or {}).get("message") or "").splitlines()[0][:72]
                out["checked"] = True
                out["remote_ref"] = remote_sha[:7]
                if remote_sha == install_info["head"]:
                    out["summary"] = f"已是最新提交（{remote_sha[:7]}）"
                    return out
                repo_path = Path(install_info["repo_dir"])
                # Check if remote_sha is already in local history (meaning local is ahead).
                git = shutil.which("git")
                if git:
                    anc = subprocess.run(
                        [git, "-C", str(repo_path), "merge-base", "--is-ancestor", remote_sha, install_info["head"]],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                    )
                    if anc.returncode == 0:
                        out["summary"] = f"本地领先于远端 {branch}（本地 {install_info['short_head']}，远端 {remote_sha[:7]}）"
                        return out
                out["update_available"] = True
                out["summary"] = f"远端 {branch} 有新提交 {remote_sha[:7]}：{msg_first_line}"
                return out

            # Package install: check latest release or tag
            r = hc.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
            if r.status_code == 404:
                rt = hc.get(f"https://api.github.com/repos/{GITHUB_REPO}/tags", params={"per_page": 1})
                rt.raise_for_status()
                tags = rt.json()
                out["checked"] = True
                if not tags:
                    out["summary"] = f"当前 v{__version__}（远端暂无 Release 标签）"
                    return out
                latest_tag = tags[0]["name"]
            else:
                r.raise_for_status()
                latest_tag = r.json().get("tag_name", "")
                out["checked"] = True
            out["remote_ref"] = latest_tag
            if latest_tag and is_newer(latest_tag, __version__):
                out["update_available"] = True
                out["summary"] = f"发现新版本 {latest_tag}（当前 v{__version__}）"
            else:
                out["summary"] = f"已是最新（v{__version__}）"
            return out
    except httpx.HTTPStatusError as e:
        out["error"] = str(e)
        suffix = f" @{install_info['short_head']}" if install_info.get("short_head") else ""
        if e.response.status_code == 403:
            out["summary"] = f"GitHub 匿名查询配额暂时受限（当前 v{__version__}{suffix}）"
        else:
            out["summary"] = f"GitHub 查询返回 HTTP {e.response.status_code}（当前 v{__version__}{suffix}）"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        out["summary"] = f"未联网或无法访问 GitHub（当前 v{__version__}" + (f" @{install_info['short_head']}" if install_info.get("short_head") else "") + "）"
        return out


def _check_pypi_package(pkg: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        installed = importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        installed = None

    res: dict[str, Any] = {
        "package": pkg,
        "installed": installed,
        "latest": None,
        "checked": False,
        "update_available": False,
        "error": None,
    }
    try:
        with httpx.Client(proxy=outbound_proxy(), timeout=timeout, follow_redirects=True) as hc:
            r = hc.get(f"https://pypi.org/pypi/{pkg}/json")
            r.raise_for_status()
            latest = r.json()["info"]["version"]
            res["latest"] = latest
            res["checked"] = True
            if installed is None or is_newer(latest, installed):
                res["update_available"] = True
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)
    return res


def check_updates(timeout: float = 5.0) -> dict[str, Any]:
    """Check LectureWeaver and critical PyPI dependencies concurrently."""
    install_info = detect_lw_install()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1 + len(CRITICAL_DEPS)) as pool:
        fut_lw = pool.submit(_check_lw_remote, install_info, timeout)
        fut_deps = {pkg: pool.submit(_check_pypi_package, pkg, timeout) for pkg in CRITICAL_DEPS}
        lw_res = fut_lw.result()
        dep_res = [fut_deps[pkg].result() for pkg in CRITICAL_DEPS]

    any_update = bool(lw_res["update_available"] or any(d["update_available"] for d in dep_res))
    return {
        "lectureweaver": lw_res,
        "dependencies": dep_res,
        "any_update": any_update,
    }


def _ensure_isolated_env() -> None:
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        raise UpdateError("当前 Python 不是独立的虚拟环境或 uv tool 环境，拒绝直接修改系统 Python；请用 `uv tool upgrade lectureweaver` 管理。")


def _upgrade_packages_in_current_env(specs: list[str]) -> str:
    _ensure_isolated_env()
    uv = shutil.which("uv")
    with with_outbound_proxy():
        if uv:
            cmd = [uv, "pip", "install", "--python", sys.executable, "--upgrade", *specs]
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            if r.returncode == 0:
                return "uv pip"
            # Fall back to python -m pip if uv pip cannot manage this venv
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", *specs],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if r.returncode != 0:
            raise UpdateError(f"升级依赖失败：\n{(r.stderr or r.stdout).strip()[:600]}")
        return "pip"


def refresh_copied_skills(repo_dir: Path) -> list[str]:
    """Refresh any ``.lw-copy`` skill directories (used on Windows without symlink privileges)."""
    from .commands import SKILL_DIRS

    src = repo_dir / "skill" / "lecture-weaver"
    if not src.is_dir():
        return []
    refreshed: list[str] = []
    for rel in SKILL_DIRS.values():
        dst = Path.home() / rel / "lecture-weaver"
        if dst.is_dir() and not dst.is_symlink() and (dst / ".lw-copy").is_file():
            shutil.rmtree(dst)
            shutil.copytree(src, dst)
            (dst / ".lw-copy").write_text("copied by lw skill install; re-run after updating the project\n", encoding="utf-8")
            refreshed.append(str(dst))
    return refreshed


def run_update(
    only: str | None = None,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> dict[str, Any]:
    """Execute update for LectureWeaver and/or protocol-critical dependencies."""
    if only not in (None, "lw", "deps"):
        raise UpdateError("`--only` 仅支持 lw 或 deps")

    report = check_updates(timeout=8.0)
    actions: list[str] = []
    warnings: list[str] = []
    lw_info = report["lectureweaver"]

    # 1. Update LectureWeaver repo / package
    if only in (None, "lw"):
        if lw_info["mode"] == "editable_git":
            repo = Path(lw_info["repo_dir"])
            if lw_info["dirty"]:
                warnings.append(f"本地仓库 {repo} 有未提交的改动，已安全跳过 `git pull`（避免覆盖你的修改）")
            else:
                git = shutil.which("git")
                if not git:
                    warnings.append("未找到 git 命令，跳过仓库拉取")
                else:
                    log(f"正在更新 LectureWeaver 仓库（{repo}）…")
                    with with_outbound_proxy():
                        pr = subprocess.run(
                            [git, "-C", str(repo), "pull", "--ff-only"],
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=60,
                        )
                    if pr.returncode != 0:
                        warnings.append(f"`git pull --ff-only` 未成功：{(pr.stderr or pr.stdout).strip()[:300]}")
                    else:
                        new_head = _git_out(repo, "rev-parse", "--short", "HEAD")
                        actions.append(f"LectureWeaver 仓库已同步至 {new_head}（{pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else 'up to date'}）")
                        _upgrade_packages_in_current_env(["-e", str(repo)])
                        refreshed = refresh_copied_skills(repo)
                        if refreshed:
                            actions.append(f"已刷新复制模式的 Skill 目录：{', '.join(refreshed)}")
        elif lw_info["update_available"]:
            log("正在升级 lectureweaver 包…")
            _upgrade_packages_in_current_env(["lectureweaver"])
            actions.append("已升级 lectureweaver 包")

    # 2. Update protocol-critical dependencies
    if only in (None, "deps"):
        to_up = [
            ("notebooklm-py[browser]" if d["package"] == "notebooklm-py" else d["package"])
            for d in report["dependencies"]
            if d["update_available"] or d["installed"] is None
        ]
        if to_up:
            log(f"正在升级协议相关依赖：{', '.join(to_up)}…")
            runner = _upgrade_packages_in_current_env(to_up)
            after_vers = {}
            for d in CRITICAL_DEPS:
                try:
                    after_vers[d] = importlib.metadata.version(d)
                except importlib.metadata.PackageNotFoundError:
                    after_vers[d] = None
            actions.append(f"已通过 {runner} 升级依赖：" + "，".join(f"{k} -> {v}" for k, v in after_vers.items()))
        else:
            actions.append("核心依赖（nwafu-cas、notebooklm-py）均已是最新 PyPI 版本")

    return {
        "before": report,
        "actions": actions,
        "warnings": warnings,
    }
