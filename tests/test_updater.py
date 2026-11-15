import subprocess
from pathlib import Path

from lectureweaver import updater as U


def test_pep440_version_comparison():
    assert U.is_newer("0.2.0", "0.1.0")
    assert U.is_newer("1.0.2", "1.0.1")
    assert U.is_newer("v0.9.0", "0.8.6")
    assert not U.is_newer("0.8.6", "0.8.6")
    assert not U.is_newer("1.0.0", "1.0")
    assert U.is_newer("1.0.0", "1.0.0rc1")
    assert not U.is_newer("1.0.0b2", "1.0.0")


def test_check_updates_parses_github_and_pypi(monkeypatch):
    monkeypatch.setattr(
        U,
        "detect_lw_install",
        lambda: {
            "mode": "editable_git",
            "version": "0.1.0",
            "repo_dir": "/tmp/fake-repo",
            "branch": "main",
            "head": "1111111111111111111111111111111111111111",
            "short_head": "1111111",
            "dirty": False,
        },
    )
    monkeypatch.setattr(U.shutil, "which", lambda name: None if name == "git" else "/usr/bin/" + name)

    class FakeResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._payload

    class FakeHttpClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            if "api.github.com" in url:
                return FakeResp(200, {
                    "sha": "2222222222222222222222222222222222222222",
                    "commit": {"message": "feat: support NotebookLM sync\ndetails"},
                })
            if "nwafu-cas" in url:
                return FakeResp(200, {"info": {"version": "99.0.0"}})
            if "notebooklm-py" in url:
                return FakeResp(200, {"info": {"version": "0.0.1"}})
            raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(U.httpx, "Client", FakeHttpClient)
    rep = U.check_updates()
    assert rep["any_update"] is True
    assert rep["lectureweaver"]["update_available"] is True
    assert "2222222" in rep["lectureweaver"]["summary"]
    deps = {d["package"]: d for d in rep["dependencies"]}
    assert deps["nwafu-cas"]["update_available"] is True
    assert deps["notebooklm-py"]["update_available"] is False


def test_check_updates_degrades_gracefully_when_offline(monkeypatch):
    class OfflineClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            raise OSError("network unreachable")

    monkeypatch.setattr(U.httpx, "Client", OfflineClient)
    rep = U.check_updates(timeout=0.5)
    assert rep["any_update"] is False
    assert rep["lectureweaver"]["error"]
    assert all(d["error"] for d in rep["dependencies"])


def test_run_update_skips_git_pull_on_dirty_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], stdin=subprocess.DEVNULL, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], stdin=subprocess.DEVNULL, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], stdin=subprocess.DEVNULL, check=True)
    f = repo / "README.md"
    f.write_text("v1\n", "utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], stdin=subprocess.DEVNULL, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], stdin=subprocess.DEVNULL, check=True, capture_output=True)
    # Modify tracked file without committing
    f.write_text("local uncommitted work\n", "utf-8")

    monkeypatch.setattr(
        U,
        "check_updates",
        lambda timeout=8.0: {
            "lectureweaver": {
                "mode": "editable_git",
                "version": "0.1.0",
                "repo_dir": str(repo),
                "branch": "main",
                "head": "abc",
                "short_head": "abc",
                "dirty": True,
                "checked": True,
                "update_available": True,
                "summary": "远端有新提交",
            },
            "dependencies": [],
            "any_update": True,
        },
    )
    res = U.run_update(only="lw", log=lambda _: None)
    assert any("未提交" in w for w in res["warnings"])
    assert f.read_text("utf-8") == "local uncommitted work\n"
