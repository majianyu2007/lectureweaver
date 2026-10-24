"""西农云课堂 / 智教云 (ylb.nwafu.edu.cn) student API.

Everything here was read out of the official student web app
(``/TeachingCenterStudentWeb``, bundle ``app.4effa527.js`` and the
``aiClassroom`` chunk) and must be re-checked against live responses:

* Login: CAS service ``ylb.nwafu.edu.cn/ssoserver/login?platformNumber=002
  &platformType=4&parentPlatformNum=010``; the SSO server returns to the SPA
  with ``?ticket=…``. ``GET {acp}/api/v1/tickets/{ticket}`` answers
  ``{userId, sessionId, clientId}``.
* Every request carries ``x-Token`` = DES-CBC-decrypt(sessionId, key = iv =
  ``njlz@123``), ``x-Nonce`` = md5(userId + t + uuid), ``x-Sign`` =
  md5(nonce + t + clientKey) and ``x-Time`` = t (ms). A 401 with "Token is
  null or expired or invalid" means: POST ``{gateway}/api/v1/refreshtoken``.
* Most GETs need ``validCode`` = md5("<k>=<v>&signKey=123123") or the static
  code from the app config.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import unquote

import httpx
from Crypto.Cipher import DES
from Crypto.Util.Padding import unpad

from .cas import AuthError, LoginRequired

YLB_ORIGIN = "https://ylb.nwafu.edu.cn"
SSO_SERVICE = f"{YLB_ORIGIN}/ssoserver/login?platformNumber=002&platformType=4&parentPlatformNum=010"
SPA = f"{YLB_ORIGIN}/TeachingCenterStudentWeb/index.html"

# Runtime config of the student SPA ($staticConfig()), captured 2026-10-22.
CONFIG = {
    "lzaiApi": "https://jxzx.nwafu.edu.cn/lzaiapi",
    "lzgateway": "https://jxzx.nwafu.edu.cn",
    "acp": "https://jxzx.nwafu.edu.cn/authoritycontrolplatformapi",
    "courseApi": "https://jxzx.nwafu.edu.cn/courseApi",
    "teachingApi": "https://jxzx.nwafu.edu.cn/teachingApi",
    "lzsearchApi": "https://jxzx.nwafu.edu.cn/lzsearchapi",
    "validCode": "155cc6439a2bd898c289a0a86ee1058c",
    "desKey": "njlz@123",
    "clientId": "user-attribute-login-client",
    "clientKey": "njlz@123",
}
SIGN_KEY = "123123"


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def valid_code(query: str = "") -> str:
    """``$md5("<query>&signKey=123123")``; an empty query gives md5("&signKey=…")."""
    return md5(f"{query}&signKey={SIGN_KEY}")


def des_decrypt_hex(hex_text: str, key: str = CONFIG["desKey"]) -> str:
    k = key.encode()[:8]
    cipher = DES.new(k, DES.MODE_CBC, k)
    return unpad(cipher.decrypt(bytes.fromhex(hex_text)), 8).decode("utf-8")


def extract_ticket(url: str) -> str | None:
    """The SPA reads ``ticket`` from anything after the first '?', hash included."""
    m = re.search(r"[?&#]ticket=([^&#]+)", url)
    return unquote(m.group(1)) if m else None


class PlatformError(Exception):
    pass


@dataclass
class YlbToken:
    user_id: str = ""
    session_id: str = ""  # as returned (hex, DES-encrypted)
    client_id: str = ""
    obtained_at: float = 0.0
    user: dict = field(default_factory=dict)

    @property
    def x_token(self) -> str:
        return des_decrypt_hex(self.session_id) if self.session_id else ""


class YlbClient:
    """Signed access to the jxzx APIs; renews the token through CAS when needed."""

    def __init__(self, http: httpx.Client, token: YlbToken | None, relogin=None):
        self.http = http
        self.token = token or YlbToken()
        # Called when the token cannot be refreshed; must return a fresh YlbToken.
        self._relogin = relogin

    # ------------------------------------------------------------ session
    @classmethod
    def token_from_service_response(cls, http: httpx.Client, last: httpx.Response) -> YlbToken:
        url = str(last.url)
        ticket = extract_ticket(url)
        if not ticket:
            # The SSO server may hand over through a JS/meta redirect page.
            m = re.search(r"(https?://[^\"'\s]+ticket=[^\"'\s&]+[^\"'\s]*)", last.text or "")
            ticket = extract_ticket(m.group(1)) if m else None
        if not ticket:
            raise AuthError("云课堂单点登录没有返回 ticket", url[:120], "schema")
        r = http.get(
            f"{CONFIG['acp']}/api/v1/tickets/{ticket}",
            params={"ticket": ticket, "validCode": CONFIG["validCode"]},
            headers=_base_headers(""),
        )
        data = _decode(r)
        if not isinstance(data, dict) or not data.get("userId"):
            raise AuthError("云课堂 ticket 兑换失败", str(data)[:120], "schema")
        return YlbToken(
            user_id=str(data["userId"]),
            session_id=str(data.get("sessionId") or ""),
            client_id=str(data.get("clientId") or CONFIG["clientId"]),
            obtained_at=time.time(),
        )

    def _headers(self) -> dict[str, str]:
        return _base_headers(self.token.x_token, self.token.user_id)

    def _refresh(self) -> bool:
        cid = self.token.client_id or CONFIG["clientId"]
        try:
            r = self.http.post(f"{CONFIG['lzgateway']}/api/v1/refreshtoken", params={"clientId": cid}, headers=self._headers())
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    def request(self, method: str, url: str, *, params=None, json_body=None, _retried: bool = False) -> Any:
        try:
            r = self.http.request(method, url, params=params, json=json_body, headers=self._headers())
        except httpx.HTTPError as e:
            raise PlatformError(f"云课堂网络错误：{e.__class__.__name__}") from e
        if r.status_code == 401 and not _retried:
            if not self._refresh():
                if not self._relogin:
                    raise LoginRequired("云课堂登录已过期", "运行 lw auth login", "expired")
                self.token = self._relogin()
            return self.request(method, url, params=params, json_body=json_body, _retried=True)
        if r.status_code in (401, 403):
            raise LoginRequired("云课堂拒绝访问（登录过期或无权限）", f"HTTP {r.status_code}", "expired")
        if r.status_code >= 400:
            raise PlatformError(f"云课堂接口错误 HTTP {r.status_code}: {r.text[:160]}")
        return _decode(r)

    def get(self, url: str, **params) -> Any:
        return self.request("GET", url, params=params)

    # ------------------------------------------------------------ user / terms / courses
    def user_resource(self) -> dict:
        uid = self.token.user_id
        return self.get(f"{CONFIG['acp']}/api/v1/users/{uid}/userresource", validCode=valid_code(f"id={uid}"))

    def recent_terms(self, previous: int = 6) -> list[dict]:
        return self.get(
            f"{CONFIG['courseApi']}/v1/term/recent",
            futureTermNum=0,
            previousTermNum=previous,
            validCode=CONFIG["validCode"],
        )

    def now_term(self) -> dict:
        return self.get(f"{CONFIG['courseApi']}/v1/terms/nowterm", validCode=CONFIG["validCode"])

    def student_courses(self, school_year: str, term: str | int) -> list[dict]:
        return self.get(
            f"{CONFIG['courseApi']}/v1/courseTables/studentCourseList",
            schoolYear=school_year,
            term=term,
            userId=self.token.user_id,
            validCode=valid_code(""),
        )

    # ------------------------------------------------------------ 课堂实录
    def recording_courses(self, school_year: str, term: str | int) -> list[dict]:
        """Courses that have 课堂实录 this term (``teacherVideoResource`` page)."""
        return self.get(
            f"{CONFIG['teachingApi']}/v1/videoinfo/student/courses",
            studentId=self.token.user_id,
            schoolYear=school_year,
            term=term,
            validCode=CONFIG["validCode"],
        )

    def recordings_page(self, class_ids, school_year: str, term, page: int = 1, page_size: int = 50, week=None) -> dict:
        body = {
            "userId": self.token.user_id,
            "groupIds": class_ids,
            "openStatus": "1",
            "week": week,
            "schoolYear": school_year,
            "term": term,
            "validCode": CONFIG["validCode"],
            "page": page,
            "pageSize": page_size,
        }
        return self.request(
            "POST",
            f"{CONFIG['teachingApi']}/v1/videoinfos/page",
            params={"validCode": CONFIG["validCode"]},
            json_body=body,
        )

    def all_recordings(self, class_ids, school_year: str, term) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            r = self.recordings_page(class_ids, school_year, term, page=page)
            items = (r or {}).get("videoInfoResourceList") or []
            out.extend(items)
            total = int((r or {}).get("total") or 0)
            if not items or len(out) >= total:
                return out
            page += 1

    def course_recordings(self, course_table_id: str) -> list[dict]:
        """Months → ``recordVideos`` of one course table (教学班)."""
        return self.get(
            f"{CONFIG['teachingApi']}/v1/dibblingvideo/videodetail/{course_table_id}",
            validCode=valid_code(f"courseTableId={course_table_id}"),
            openStatus=1,
            isFilterTeacherDelete="true",
        )

    def record_video(self, video_id: str) -> dict:
        return self.get(f"{CONFIG['teachingApi']}/v1/recordvideo/{video_id}", validCode=valid_code(f"id={video_id}"))

    def speech(self, task_id: str) -> list[dict]:
        """Existing ASR: ``[{speechRecognitionResults: [{startTimeStamp, endTimeStamp, recognitionResult}]}]``."""
        return self.get(
            f"{CONFIG['lzaiApi']}/v1/spotbroadcastspeechrecognitions",
            identifySensitiveWord="false",
            taskId=task_id,
            validCode=valid_code(f"taskId={task_id}"),
        )

    def ppt_pages(self, record_video_file_id: str) -> list[dict]:
        return self.get(
            f"{CONFIG['teachingApi']}/v1/classpptinfo/pptpagerecognitioninfos",
            recordVideoFileId=record_video_file_id,
            validCode=valid_code(f"recordVideoFileId={record_video_file_id}"),
        )

    def platform_ai(self, video_id: str, kind: str) -> Any:
        """``summarize`` | ``abstracts`` | ``mindmap``: the platform's own AI output."""
        return self.get(f"{CONFIG['teachingApi']}/v1/recordvideo/{video_id}/{kind}", validCode=valid_code(f"id={video_id}"))

    def room_speech_enabled(self, room_id: str) -> Any:
        return self.get(f"{CONFIG['teachingApi']}/v1/aispeechrecognitionroom/{room_id}", validCode=valid_code(f"roomId={room_id}"))


def _base_headers(x_token: str, user_id: str = "") -> dict[str, str]:
    t = str(int(time.time() * 1000))
    nonce = md5(f"{user_id}{t}{uuid.uuid4()}").lower()
    sign = md5(nonce + t + CONFIG["clientKey"]).lower()
    return {
        "X-Requested-With": "xmlhttprequest",
        "Accept": "application/json, text/plain, */*",
        "Origin": YLB_ORIGIN,
        "Referer": f"{YLB_ORIGIN}/TeachingCenterStudentWeb/index.html",
        "x-Token": x_token,
        "x-Nonce": nonce,
        "x-Sign": sign,
        "x-Time": t,
    }


def _decode(r: httpx.Response) -> Any:
    text = r.text.strip()
    if not text:
        return None
    # The SPA strips a JSONP wrapper if present: "cb({...});"
    try:
        return r.json()
    except ValueError:
        inner = re.sub(r"^.*?\(\s*", "", text, count=1, flags=re.S)
        inner = re.sub(r"\);?$", "", inner)
        import json

        try:
            return json.loads(inner)
        except ValueError:
            return text


def video_url(file: dict) -> str:
    return f"{file.get('outerIp') or ''}{file.get('videoStorePath') or ''}"


def player_url(course_table_id: str, video_id: str, seconds: int | None = None) -> str:
    """Deep link into the official player; ``latestWatchTime`` seeks on load."""
    q = f"courseTableId={course_table_id}&videoId={video_id}"
    if seconds is not None:
        q += f"&latestWatchTime={int(seconds)}"
    return f"{SPA}#/aiClassroom/aiClassroom.htm?{q}"


def token_to_json(t: YlbToken) -> dict:
    return asdict(t)


def token_from_json(d: dict | None) -> YlbToken | None:
    if not d:
        return None
    return YlbToken(**{k: v for k, v in d.items() if k in YlbToken.__dataclass_fields__})
