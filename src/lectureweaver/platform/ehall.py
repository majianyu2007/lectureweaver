"""The 金智 EMAP timetable app on ehall.nwafu.edu.cn (我的本研课表, ``wdkbby``).

Port of ``nwafu-bksxk/lib/data/ehall_service.dart`` (verified live there on
2026-10-11): the app index is a CAS service; after the ticket exchange the data
endpoints answer 403 until ``funauthapp/api/getAppConfig/<app>-<id>.do`` has
been called once. Responses look like
``{"code":"0","datas":{"<name>":{"rows":[...]}}}``.
"""

from __future__ import annotations

import json
from typing import Any


from .cas import CasClient, LoginRequired

EHALL = "https://ehall.nwafu.edu.cn"
APP_PATH = "wdkbby"
APP_ID = "5959167891382285"
INDEX = f"{EHALL}/jwapp/sys/{APP_PATH}/*default/index.do"


class EhallError(Exception):
    pass


class EhallTimetable:
    def __init__(self, cas: CasClient):
        self.cas = cas
        self.http = cas.http
        self._ready = False

    def activate(self) -> None:
        last = self.cas.open_service(INDEX)
        if "ehall.nwafu.edu.cn" not in str(last.url) or last.status_code >= 400:
            raise LoginRequired("无法进入教务课表应用", str(last.url)[:80], "expired")
        r = self.http.get(f"{EHALL}/jwapp/sys/funauthapp/api/getAppConfig/{APP_PATH}-{APP_ID}.do")
        try:
            body = r.json()
        except ValueError as e:
            raise EhallError("教务课表应用配置无效") from e
        if str(body.get("APP_ID")) != APP_ID:
            raise EhallError("教务课表应用配置无效")
        self._ready = True

    def rows(self, path: str, name: str, form: dict | None = None) -> list[dict[str, Any]]:
        if not self._ready:
            self.activate()
        r = self.http.post(
            f"{EHALL}/jwapp/sys/{APP_PATH}/{path}",
            data=form or {},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if r.status_code in (302, 401, 403) or "authserver" in r.headers.get("location", ""):
            self._ready = False
            raise LoginRequired("教务系统会话已失效", "", "expired")
        try:
            body = r.json()
        except ValueError as e:
            raise EhallError("教务系统返回了无法识别的内容") from e
        if str(body.get("code")) != "0":
            raise EhallError(str(body.get("msg") or "教务系统返回错误"))
        block = (body.get("datas") or {}).get(name) or {}
        rows = block.get("rows")
        if not isinstance(rows, list):
            raise EhallError("教务系统数据结构已变化")
        return rows

    def current_term(self) -> dict:
        r = self.rows("modules/jshkcb/dqxnxq.do", "dqxnxq")
        if not r:
            raise EhallError("未取得当前学期")
        return r[0]

    def terms(self) -> list[dict]:
        return self.rows("modules/jshkcb/xnxqcx.do", "xnxqcx")

    def timetable_raw(self, term_code: str) -> dict[str, Any]:
        """Everything needed to rebuild a term's schedule offline."""
        year, half = term_code[:9], term_code[10:] or "1"
        out: dict[str, Any] = {"term": term_code}
        out["calendar"] = self.rows("modules/xskcb/cxxljc.do", "cxxljc", {"XN": year, "XQ": half})
        out["periods"] = self.rows("modules/jshkcb/jc.do", "jc")
        out["sessions"] = self.rows("modules/xskcb/cxxszhxqkb.do", "cxxszhxqkb", {"XNXQDM": term_code})
        try:
            out["unscheduled"] = self.rows("modules/xskcb/xswpkc.do", "xswpkc", {"XNXQDM": term_code})
        except EhallError:
            out["unscheduled"] = []
        try:
            out["adjustments"] = self.rows(
                "modules/xskcb/xsdkkc.do",
                "xsdkkc",
                {
                    "XNXQDM": term_code,
                    "*order": "-SQSJ",
                    "querySetting": json.dumps(
                        [{"name": "BYBZ", "builder": "notEqual", "linkOpt": "AND", "value": "1"}]
                    ),
                },
            )
        except EhallError:
            out["adjustments"] = []
        return out

    def week_of(self, term_code: str, date_iso: str) -> int | None:
        """The school's own "date → teaching week" (dqzc.do)."""
        year, half = term_code[:9], term_code[10:] or "1"
        try:
            r = self.rows("modules/jshkcb/dqzc.do", "dqzc", {"XN": year, "XQ": half, "RQ": date_iso})
        except EhallError:
            return None
        try:
            return int(r[0]["ZC"]) if r else None
        except (KeyError, ValueError, TypeError):
            return None
