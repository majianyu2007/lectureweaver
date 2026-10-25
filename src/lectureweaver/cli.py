"""Command-line entry point: ``lw <group> <command>``.

Every command prints readable Chinese text by default and JSON with
``--json`` so an AI harness can parse results reliably.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from . import __version__

COMMANDS: dict[tuple[str, ...], Callable[[argparse.Namespace], Any]] = {}


def command(*path: str):
    def deco(fn):
        COMMANDS[path] = fn
        return fn

    return deco


def emit(args: argparse.Namespace, data: Any, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif text is not None:
        print(text)
    elif isinstance(data, (dict, list)):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif data is not None:
        print(data)


def build_parser() -> argparse.ArgumentParser:
    from . import commands  # noqa: F401  (registers subcommands)

    p = argparse.ArgumentParser(prog="lw", description="LectureWeaver 个人课堂知识与学习助手")
    p.add_argument("--version", action="version", version=f"lectureweaver {__version__}")
    sub = p.add_subparsers(dest="_group", metavar="<命令组>")
    for spec in commands.SPECS:
        spec(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    from . import netfix

    # Windows consoles/pipes default to the local code page (e.g. GBK);
    # always emit UTF-8 so Chinese text and symbols never crash output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    netfix.install()
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = getattr(args, "_fn", None)
    if fn is None:
        parser.print_help()
        return 0
    from nwafu_cas import CasError

    from .platform.cas import AuthError
    from .prompt import PromptUnavailable

    try:
        rv = fn(args)
        return int(rv) if isinstance(rv, int) else 0
    except (AuthError, CasError) as e:
        print(f"登录问题：{e}", file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({"error": "auth", "kind": e.kind, "message": str(e)}, ensure_ascii=False))
        return 3
    except PromptUnavailable as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    except UserError as e:
        print(f"错误：{e}", file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({"error": "user", "message": str(e)}, ensure_ascii=False))
        return 2


class UserError(Exception):
    """A problem the user can fix; printed without a traceback."""


if __name__ == "__main__":
    sys.exit(main())
