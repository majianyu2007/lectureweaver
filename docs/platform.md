# 平台事实记录（维护用）

本项目依赖的学校系统行为。全部来自对官方前端代码的阅读和只读接口测试（2026 年秋季学期）；平台改版时先对照这里排查。

## 统一身份认证（authserver.nwafu.edu.cn，金智 AuthServer）

- 登录由 [nwafu-cas](https://github.com/majianyu2007/nwafu-cas) 实现（原 wisedu_cas，协议细节与二次认证各方式的接口见该项目；`nwafu-cas doctor` 可检查页面是否改版）。基本流程与 `nwafu-bksxk/lib/data/cas_client.dart` 一致：`GET /authserver/login?service=<S>`（302 = CASTGC 仍有效）→ 表单 `pwdEncryptSalt` + `execution` → AES-CBC 加密密码 → 跟随重定向。
- `checkNeedCaptcha.htl` 对不存在的学号也返回 `isNeed:true`；页面 `captchaSwitch="2"`（滑块）。真实账号正常登录时不需要。
- **二次验证页面 2026 年秋改版**：方式列在 `reauth-tab-item`（Tab，`data-type`）和 `reauth-tab-more-item`（“更多”下拉，`id`）中。每个账号可用的方式不同；服务器支持：2 认证密码、3 短信、4 企业微信、5 今日校园、6/18/20 实人认证、7 安全问题（现改走密码）、8 微信扫码、9 QQ 扫码、10 安全令牌 OTP（以 `otpCode` 提交）、11 邮箱、12 钉钉、13 WeLink、14 钉钉/企微/WeLink 扫码、15 公众号、CA UKey。除 QQ 扫码和 UKey 外 nwafu-cas 都能完成。提交 `reAuthSubmit.do`，`skipTmpReAuth=true` = 信任此设备。“记住我”现为 7 天。
- “信任此设备”绑定浏览器指纹：页面 JS 计算 md5(浏览器特征) 后 `GET /authserver/bfp/info?bfp=<FP>`，服务器设置 `MULTIFACTOR_BROWSER_FINGERPRINT`（30 天）。本工具为每个安装生成固定指纹并在登录前注册。
- `CASTGC` 有效期约 14 天（rememberMe=true）。

## 云课堂 / 智教云（ylb.nwafu.edu.cn → 接口在 jxzx.nwafu.edu.cn）

- 前端 `/TeachingCenterStudentWeb/`，运行时配置被混淆（含反调试死循环），通过无头浏览器读取 `$staticConfig()` 获得：
  `teachingApi=https://jxzx.nwafu.edu.cn/teachingApi`、`courseApi=…/courseApi`、`lzaiApi=…/lzaiapi`、`authoritycontrolplatformApi=…/authoritycontrolplatformapi`、`lzgateway=https://jxzx.nwafu.edu.cn`、`validCode=155cc6439a2bd898c289a0a86ee1058c`、`desKey=njlz@123`、`clientKey=njlz@123`、`clientId=user-attribute-login-client`。
- CAS service：`https://ylb.nwafu.edu.cn/ssoserver/login?platformNumber=002&platformType=4&parentPlatformNum=010`。ssoserver 校验 CAS ticket 后 302 到自身，返回一个 HTML 页面，其中含带 `ticket=` 的 SPA 地址。
- `GET {acp}/api/v1/tickets/{ticket}?ticket=…&validCode=<static>` → `{userId, sessionId(hex, DES-CBC 加密), clientId}`。
- 每个请求头：`x-Token` = DES-CBC 解密 sessionId（key=iv=`njlz@123`）；`x-Nonce` = md5(userId+t+uuid)；`x-Sign` = md5(nonce+t+clientKey)；`x-Time` = t(ms)。401 "Token is null or expired or invalid" → `POST {lzgateway}/api/v1/refreshtoken?clientId=`。
- 大多数 GET 需要 `validCode` = md5("<k>=<v>&signKey=123123")（无参数时 md5("&signKey=123123")），少数用静态 validCode。
- 课堂实录：
  - `GET teachingApi/v1/videoinfo/student/courses?studentId&schoolYear&term&validCode(静态)` → 课程（`courseId`、`classIds`）。
  - `POST teachingApi/v1/videoinfos/page?validCode=` body `{userId, groupIds: classIds, openStatus:"1", week, schoolYear, term, page, pageSize}` → `videoInfoResourceList`、`total`。**每条录像是一节（45 分钟）**；同一次课的各节共享 `courseTableDetailId`。
  - `GET teachingApi/v1/recordvideo/{id}` → `teacherViewFiles`（教师机位）、`studentViewFiles`（学生机位）、`vgaViewFiles`（电脑录屏），每个文件 `outerIp + videoStorePath` 为 MP4 直链；还有 `taskId`、`startTime` 等。
  - `GET lzaiApi/v1/spotbroadcastspeechrecognitions?taskId&identifySensitiveWord&validCode` → `[{speechRecognitionResults:[{startTimeStamp,endTimeStamp,recognitionResult}]}]`（相对该节录像开始的秒数）。抽查的一个学期里约八成录像有转写，最近几天的录像常常还没有。
  - `GET teachingApi/v1/classpptinfo/pptpagerecognitioninfos?recordVideoFileId` → 平台课件页识别（实测多为空）。
  - `GET teachingApi/v1/recordvideo/{id}/summarize|abstracts|mindmap` → 平台 AI 生成内容（只作定位参考）。
  - 播放器深链：`/TeachingCenterStudentWeb/index.html#/aiClassroom/aiClassroom.htm?courseTableId=…&videoId=…&latestWatchTime=<秒>`，已登录的浏览器打开后三路视频定位到该秒（已验证播放器 `currentTime` 与参数一致）。
- 少数录像是老师手动录制的短片（`recordResource=1`，无 `segment`/`courseTableDetailId`）。

## 媒体服务器（lzmNN.nwafu.edu.cn，openresty + 对象存储）

- 只校验 `Referer: https://ylb.nwafu.edu.cn/`（无则 403），支持 HTTP Range（206）。
- MP4 布局：`ftyp, free, mdat(~386MB), moov(~1.6–2.2MB)`，moov 在文件末尾。
- 一节课三路文件约：教师机位 730MB（1080p 50fps，2Mbps，AAC 64k 立体声），学生机位 383MB，录屏 388MB（1080p 20fps，1Mbps，AAC 119k）。
- 录屏关键帧（IDR）每 1 秒一个、约 50KB；教师机位每 2 秒一个、约 150KB。
- 教师机位音频以约 1 秒的块连续存放（约 2700 段、21MB）；录屏文件音频与视频逐帧交错（55000+ 段），不适合按样本取。
- 教学电脑屏幕时钟可能比录像时间快几分钟，引用以录像时间为准。
- **哪一路有老师的声音不固定**：同一间教室，某天教师机位音轨只有约 11kbps、平均 −87dB，学生机位 −80dB，录屏 −68dB，三路都没有可识别的语音（当天平台也没有转写）；另一天三路都有清晰语音（教师机位最响，−21dB）。也有教室只有录屏那一路声音清楚。因此识别前按 VAD 抽样选择音源。
- 部分教室有白板：录屏长时间不变时老师在板书，教师机位 1080p 画面上部的白板文字基本可辨认，画面下部是学生。

## 教务课表（ehall.nwafu.edu.cn，EMAP wdkbby）

- 与 nwafu-bksxk 相同：先访问应用 index（CAS ticket）再调 `funauthapp/api/getAppConfig/wdkbby-5959167891382285.do`。
- `cxxszhxqkb.do` 的 `JXBID`（教学班 ID）= 云课堂的 `classIds`（测试过的课程全部一致；个别课程的 ID 带一段字母数字前缀，按末尾 18 位比对）。课程名带“【本】”前缀。
- 课表已反映调课结果；`xsdkkc.do` 给出调课记录（原/新 周次、星期、节次、教室）。
- 冬令/夏令作息：5 月 1 日–10 月 1 日下午晚上推后 30 分钟；录像通常提前约 1 分钟开始。

## 本机环境注意（在作者的 macOS 上观察到）

- 校园网 DNS 对部分 `*.nwafu.edu.cn` 域名在 macOS 系统解析器中偶尔每次耗时 30 秒（dig 直接查询很快）。`netfix.py` 对学校域名做缓存 + 超时后直接 UDP 查询。
- Homebrew ffmpeg 9 在本机无法解析域名，所以 ffmpeg 只读本地数据或经 127.0.0.1 代理。
- whisper.cpp 自带 `--vad` 时 token 时间落在去除静音后的时间轴上（实测偏差可达 30 秒），因此改为先用 `whisper-vad-speech-segments` 切分语音块再逐块识别。
