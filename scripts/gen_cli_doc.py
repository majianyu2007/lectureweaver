"""Regenerate skill/lecture-weaver/references/cli.md from the argparse definitions."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lectureweaver.cli import build_parser  # noqa: E402


def walk(p: argparse.ArgumentParser, prefix: str, out: list[str]):
    subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        opts = []
        for a in p._actions:
            if a.dest in ("help", "json"):
                continue
            name = "/".join(a.option_strings) if a.option_strings else f"<{a.dest}>"
            opts.append(f"  - `{name}` {a.help or ''}")
        out.append(f"- **`{prefix}`** — {p.description or ''}")
        out += opts
        return
    for s in subs:
        for name, sp in s.choices.items():
            walk(sp, f"{prefix} {name}", out)


lines = ["# 命令参考（自动生成：python scripts/gen_cli_doc.py）", "", "所有命令支持 `--json`。", ""]
walk(build_parser(), "lw", lines)
Path(__file__).resolve().parents[1].joinpath("skill/lecture-weaver/references/cli.md").write_text("\n".join(lines) + "\n", "utf-8")
print(len(lines), "lines")
