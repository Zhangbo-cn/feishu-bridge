#!/usr/bin/env python
"""发飞书消息 (文本/卡片/文件) —— 双通道 + 多应用。

通道 A: 自建应用 + 收件人 (群 chat_id / 私聊 open_id / 邮箱)。可指定任一群, 可升级成双向。
通道 B: 群自定义机器人 incoming webhook。单向, 不需要凭证体系。

凭证查找链 (找到第一个就用): $FEISHU_HOME → cwd → git 仓库根 → ~/.feishu/ → ~ → skill 目录。
凭证文件 (建议 600 权限 + gitignore, 脚本不会把它们明文打印):
  .feishu_apps.json   {"default":"pi","apps":{"pi":{"app_id":"cli_...","app_secret":"...",
                       "chat_id":"oc_..."}, "openclaw":{...}}}      ← 推荐, 多应用并存
  .feishu_app         一行: 默认用哪个 app (等价 --app)
  .feishu_chat        一行: 默认收件人 (oc_.../ou_.../邮箱), 无则用 app 内的 chat_id
  .lark_key           两行 app_id/app_secret (单应用时代的旧格式, 仍兼容)
  .feishu_webhook     一行 webhook URL; .feishu_secret 一行签名密钥 (可选)

用法:
  scripts/feishu_send.py --text "跑完了"
  scripts/feishu_send.py --title 结论 --stdin < summary.txt
  scripts/feishu_send.py --to me@corp.com --text "私聊"
  scripts/feishu_send.py --app pi --status      # 看通道与凭证 (会验 token)
  scripts/feishu_send.py --file 报表.xlsx --title 结论 --text 要点   # 文本 + 文件
  scripts/feishu_send.py --selftest             # 内建假飞书自检, 不碰真群

发文件只能走自建应用 (需 im:resource 权限): 群自定义机器人 webhook 没有上传端点。

exit: 0 成功 / 4 没有可用通道 / 5 没内容 / 6 webhook 是占位符 / 7 发送失败 / 8 自检失败
      9 --file 指的文件不存在
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _cred_roots() -> list:
    """凭证搜索链 (按优先级)。写成多根是为了跟着 skill 跑到哪个项目都能用:

      1. $FEISHU_HOME        显式指定, 最高优先
      2. 当前工作目录        项目自带凭证 (每个项目发不同的群)
      3. 向上找到 git 根    在子目录里跑也能找到仓库根的凭证
      4. ~/.feishu/          个人全局默认 (推荐放这)
      5. ~                   兼容直接丢家目录的写法
      6. skill 目录         跟 skill 一起分发的凭证 (不推荐, 会进备份)

    找到第一个存在的文件就用, 不合并 —— 否则项目想覆盖全局配置时会拿到半新半旧的组合。
    """
    roots = []
    env = os.environ.get("FEISHU_HOME", "").strip()
    if env:
        roots.append(Path(os.path.expanduser(env)))
    cwd = Path.cwd()
    roots.append(cwd)
    for p in cwd.resolve().parents:            # 向上找仓库根
        if (p / ".git").exists():
            roots.append(p)
            break
    roots += [Path.home() / ".feishu", Path.home(), REPO]
    out, seen = [], set()
    for r in roots:
        k = str(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def cred_path(name: str) -> Path:
    """在搜索链里找 `name`; 都没有就返回首选位置 (给报错消息用)。"""
    for r in _cred_roots():
        p = r / name
        if p.exists():
            return p
    return _cred_roots()[0] / name


DEFAULT_WEBHOOK_FILE = cred_path(".feishu_webhook")
DEFAULT_SECRET_FILE = cred_path(".feishu_secret")
LARK_KEY_FILE = cred_path(".lark_key")           # 旧格式: 两行 app_id / app_secret
APPS_FILE = cred_path(".feishu_apps.json")       # 新格式: 多应用
DEFAULT_CHAT_FILE = cred_path(".feishu_chat")    # 一行: 默认收件人
APP_NAME_FILE = cred_path(".feishu_app")         # 一行: 默认 app 名
TOKEN_CACHE_DIR = Path("/tmp")
API_BASE = "https://open.feishu.cn"
MAX_CHARS = 18000        # 单条上限 (留余量), 超了截断并标注
FILE_MAX = 30 * 1024 * 1024   # 飞书 im/v1/files 单文件上限 30MB
# 扩展名 → 飞书 file_type。不在表里的用 stream (通用二进制, 能下载但无在线预览)。
# file_type 只影响客户端能不能预览, 不影响能不能发。
FILE_TYPES = {".xls": "xls", ".xlsx": "xls", ".csv": "xls",
              ".doc": "doc", ".docx": "doc",
              ".ppt": "ppt", ".pptx": "ppt",
              ".pdf": "pdf", ".mp4": "mp4", ".opus": "opus"}
HOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"
TRIES = 3                # 网络/5xx 重试次数 (业务错误不重试)


# ─────────────────────────── 凭证 ───────────────────────────

def _read1(p: Path) -> str:
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def load_cred(webhook: str = "") -> tuple[str, str]:
    """群机器人: --webhook > $FEISHU_WEBHOOK > .feishu_webhook。secret 同理。"""
    w = webhook or os.environ.get("FEISHU_WEBHOOK", "") or _read1(DEFAULT_WEBHOOK_FILE)
    s = os.environ.get("FEISHU_SECRET", "") or _read1(DEFAULT_SECRET_FILE)
    return w.strip(), s.strip()


def sign(secret: str, ts: str) -> str:
    """飞书签名: base64(hmac_sha256(key=f"{ts}\\n{secret}", msg=b""))。"""
    return base64.b64encode(hmac.new(f"{ts}\n{secret}".encode(), b"",
                                     digestmod=hashlib.sha256).digest()).decode()


def cred_problem(w: str) -> tuple[int, str]:
    """0=没问题 1=可疑(只警告) 2=致命(别发)。只有非 ASCII 算致命 —— 那一定是占位符。"""
    if not w:
        return 1, "未配 webhook"
    if any(ord(c) > 127 for c in w):
        return 2, "webhook 里有非 ASCII 字符, 是占位符没换"
    if not w.startswith(HOOK_PREFIX):
        return 1, f"webhook 形状可疑, 不像是 {HOOK_PREFIX}... 开头"
    return 0, ""


def load_apps() -> dict:
    """所有可用应用: .feishu_apps.json 优先, 再兜底旧的 .lark_key, 再兜底环境变量。"""
    apps: dict = {}
    if APPS_FILE.exists():
        try:
            raw = json.loads(APPS_FILE.read_text(encoding="utf-8"))
            for name, v in (raw.get("apps") or {}).items():
                if isinstance(v, dict) and v.get("app_id") and v.get("app_secret"):
                    apps[name] = {"app_id": v["app_id"].strip(),
                                  "app_secret": v["app_secret"].strip(),
                                  "chat_id": (v.get("chat_id") or "").strip()}
        except Exception as e:
            print(f"⚠ {APPS_FILE.name} 解析失败({e}), 忽略", file=sys.stderr)
    if not apps:
        lines = [l.strip() for l in _read1(LARK_KEY_FILE).splitlines() if l.strip()]
        if len(lines) >= 2:
            apps["legacy"] = {"app_id": lines[0], "app_secret": lines[1], "chat_id": ""}
    if not apps and os.environ.get("FEISHU_APP_ID"):
        apps["env"] = {"app_id": os.environ["FEISHU_APP_ID"],
                       "app_secret": os.environ.get("FEISHU_APP_SECRET", ""), "chat_id": ""}
    return apps


def default_app_name(apps: dict) -> str:
    if not apps:
        return ""
    if APPS_FILE.exists():                       # 配置文件里的 default 字段
        try:
            d = (json.loads(APPS_FILE.read_text(encoding="utf-8")).get("default") or "").strip()
            if d in apps:
                return d
        except Exception:
            pass
    n = os.environ.get("FEISHU_APP", "") or _read1(APP_NAME_FILE)
    if n in apps:
        return n
    return next(iter(apps))


def load_app(name: str = "", app_id: str = "", app_secret: str = "") -> tuple[str, dict]:
    """返回 (名字, {"app_id","app_secret","chat_id"})。--app-id/--app-secret 直接用时名字为空。"""
    if app_id and app_secret:
        return "", {"app_id": app_id.strip(), "app_secret": app_secret.strip(), "chat_id": ""}
    apps = load_apps()
    n = (name or os.environ.get("FEISHU_APP", "") or _read1(APP_NAME_FILE)
         or default_app_name(apps))
    if n in apps:
        return n, apps[n]
    return "", {}


def load_chat(chat_id: str = "", app: dict | None = None) -> str:
    """收件人: --to/--chat-id > $FEISHU_CHAT > .feishu_chat > app 配置里的 chat_id。"""
    return (chat_id or os.environ.get("FEISHU_CHAT_ID", "") or _read1(DEFAULT_CHAT_FILE)
            or ((app or {}).get("chat_id") or "")).strip()


def read_app(apps: dict) -> tuple[str, dict]:
    """挑一个**能读消息**的 app: 优先 openclaw(有 im:message 读权限), 其余按配置顺序。"""
    for name in ("openclaw", *apps):
        if name in apps:
            return name, apps[name]
    raise SystemExit("没有可用的飞书应用凭证")


def id_type_of(rid: str) -> str:
    """按收件人长相判 receive_id_type: oc_=群, ou_=用户open_id, on_=union_id, 含@=邮箱, 其余当 user_id。"""
    if rid.startswith("oc_"):
        return "chat_id"
    if rid.startswith("ou_"):
        return "open_id"
    if rid.startswith("on_"):
        return "union_id"
    return "email" if "@" in rid else "user_id"


# ─────────────────────────── 发送 ───────────────────────────

class FeishuError(RuntimeError):
    """发送失败, 带可读原因 (HTTP 状态 + 飞书返回体 / 网络 / URL 非法)。"""


def _unwrap(d: dict) -> str:
    """飞书失败也返 HTTP 200, 真实原因在 body 的 code —— 必须解出来, 否则会假报成功。"""
    code = d.get("code", d.get("StatusCode"))
    if code not in (0, None):
        raise FeishuError(f"飞书拒收 code={code} msg={d.get('msg') or d.get('StatusMessage')}")
    return json.dumps(d, ensure_ascii=False)


def _post(url: str, body: dict, headers: dict, *, tries: int = TRIES, tag: str = "") -> dict:
    """POST + 解析 JSON。网络类错误与 5xx/429 指数退避重试; 业务错误立即上抛。"""
    last = ""
    for i in range(max(1, tries)):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:      # 4xx/5xx, 原因多在 body
            raw = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                last = f"HTTP {e.code} {raw[:120]}"
                time.sleep(1.5 * (2 ** i))
                continue
            raise FeishuError(f"HTTP {e.code}: {raw[:300]}") from None
        except UnicodeEncodeError as e:
            raise FeishuError(f"URL/报文含非法字符 (占位符没换?): {e}") from None
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as e:
            last = f"{type(e).__name__} {e}"
            if i < tries - 1:
                time.sleep(1.5 * (2 ** i))
                continue
            raise FeishuError(f"网络不可达 ({last}); 需要能出外网") from None
    raise FeishuError(f"重试 {tries} 次仍失败: {last}")


def tenant_token(app_id: str, app_secret: str, api_base: str = API_BASE) -> str:
    """拿 tenant_access_token (2h)。缓存到 /tmp/lark_tok_<hash>.json, 剩 <5min 才重取。"""
    cache = TOKEN_CACHE_DIR / ("lark_tok_" +
                               hashlib.md5(f"{api_base}|{app_id}".encode()).hexdigest()[:10] + ".json")
    if cache.exists():
        try:
            d = json.loads(cache.read_text())
            if d.get("exp", 0) - time.time() > 300:
                return d["token"]
        except Exception:
            pass
    d = _post(f"{api_base}/open-apis/auth/v3/tenant_access_token/internal",
              {"app_id": app_id, "app_secret": app_secret}, {"Content-Type": "application/json"})
    if d.get("code") != 0:
        raise FeishuError(f"应用凭证无效 code={d.get('code')} msg={d.get('msg')}"
                          " (app_id/app_secret 对不上?)")
    cache.write_text(json.dumps({"token": d["tenant_access_token"],
                                 "exp": time.time() + d.get("expire", 7200)}))
    return d["tenant_access_token"]


def md_text(text: str) -> str:
    """卡片(lark_md)正文: 把字面 `\\n`/`\\t` 转成真换行/制表。

    Feishu 的 **text** 消息自己认 `\\n` 转义, 但 **lark_md** 不认 (会原样显示 "\\n")。
    CLI 上传参很难敲真换行 (shell 单引号不解释转义), 所以卡片模式统一转一下。
    """
    return text.replace("\\n", "\n").replace("\\t", "\t")


def card_of(text: str, title: str) -> dict:
    """卡片的 msg_type + content(JSON 串)。文本模式则是 {"text": ...}。"""
    if title:
        return {"msg_type": "interactive", "content": json.dumps(
            {"config": {"wide_screen_mode": True},
             "header": {"title": {"tag": "plain_text", "content": title}},
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md_text(text)}}]},
            ensure_ascii=False)}
    return {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}


def clip(text: str) -> str:
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n…(已截断, 原 {len(text)} 字)"
    return text


def send_app(text: str, rid: str, app: dict, title: str = "", *,
             app_name: str = "", api_base: str = API_BASE, id_type: str = "") -> str:
    """自建应用发消息。rid 可以是群 chat_id / open_id / 邮箱 / user_id。"""
    tok = tenant_token(app["app_id"], app["app_secret"], api_base)
    t = id_type or id_type_of(rid)
    c = card_of(clip(text), title)
    d = _post(f"{api_base}/open-apis/im/v1/messages?receive_id_type={t}",
              {"receive_id": rid, "msg_type": c["msg_type"], "content": c["content"]},
              {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    return _unwrap(d)


def file_type_of(path: Path) -> str:
    """按扩展名定 file_type; 认不出来就 stream。"""
    return FILE_TYPES.get(path.suffix.lower(), "stream")


def _multipart(fields: dict, fname: str, fdata: bytes,
               field: str = "file") -> tuple:
    """手搓 multipart/form-data, 返回 (body, content_type)。

    不用 requests: 本文件全程只依赖标准库 —— 开机报告/cron 场景要能在没装
    依赖的解释器下直接跑。

    文件名用 UTF-8 直写 header (飞书收 UTF-8 文件名正常, 不需 RFC2231);
    但双引号必须换掉, 否则带引号的文件名会把 header 截断。
    """
    boundary = "----pi" + hashlib.md5(f"{time.time()}{fname}".encode()).hexdigest()
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode("utf-8")
    safe = fname.replace('"', "'")
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{safe}"\r\n').encode("utf-8")
    out += b"Content-Type: application/octet-stream\r\n\r\n"
    out += fdata + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def upload_file(path: Path, app: dict, *, api_base: str = API_BASE,
                file_type: str = "", name: str = "") -> str:
    """上传到 im/v1/files, 返回 file_key。需应用权限 `im:resource`。

    缺权限也是99991672, 所以 deliver_file 用与文本一致的回退链。
    """
    if not path.is_file():
        raise FeishuError(f"文件不存在: {path}")
    size = path.stat().st_size
    if size == 0:
        raise FeishuError(f"文件是空的: {path}")
    if size > FILE_MAX:
        raise FeishuError(f"文件 {size / 1048576:.1f}MB 超过飞书 30MB 上限: {path.name}")
    tok = tenant_token(app["app_id"], app["app_secret"], api_base)
    fname = name or path.name
    body, ctype = _multipart(
        {"file_type": file_type or file_type_of(path), "file_name": fname},
        fname, path.read_bytes())
    # 上传自己发请求: _post 会把 body 当 JSON 序列化, 塞不进二进制。
    last = ""
    d = None
    for i in range(TRIES):
        req = urllib.request.Request(
            f"{api_base}/open-apis/im/v1/files", data=body,
            headers={"Content-Type": ctype, "Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:   # 30MB 上行比发消息慢
                d = json.loads(r.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and i < TRIES - 1:
                last = f"HTTP {e.code} {raw[:120]}"
                time.sleep(1.5 * (2 ** i))
                continue
            raise FeishuError(f"上传 HTTP {e.code}: {raw[:300]}") from None
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as e:
            last = f"{type(e).__name__} {e}"
            if i < TRIES - 1:
                time.sleep(1.5 * (2 ** i))
                continue
            raise FeishuError(f"上传网络不可达 ({last})") from None
    if d is None:
        raise FeishuError(f"上传重试 {TRIES} 次仍失败: {last}")
    _unwrap(d)          # code!=0 就报错, 别拿空 file_key 往下跑
    key = ((d.get("data") or {}).get("file_key") or "").strip()
    if not key:
        raise FeishuError(f"上传没拿到 file_key: {json.dumps(d, ensure_ascii=False)[:200]}")
    return key


def send_file(path: Path, rid: str, app: dict, *, api_base: str = API_BASE,
              id_type: str = "", file_type: str = "", name: str = "") -> str:
    """上传 + 发一条 msg_type=file。两步必须同一个 app —— file_key 不跨 app。"""
    key = upload_file(path, app, api_base=api_base, file_type=file_type, name=name)
    tok = tenant_token(app["app_id"], app["app_secret"], api_base)
    t = id_type or id_type_of(rid)
    d = _post(f"{api_base}/open-apis/im/v1/messages?receive_id_type={t}",
              {"receive_id": rid, "msg_type": "file",
               "content": json.dumps({"file_key": key}, ensure_ascii=False)},
              {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    return _unwrap(d)


def deliver_file(path: Path, *, rid: str, app: dict, app_name: str = "",
                 api_base: str = API_BASE, id_type: str = "", fallback: bool = True,
                 file_type: str = "", name: str = "") -> tuple:
    """发文件, 带与 deliver 一致的缺权限回退链。返回 (通道描述, 回执)。

    webhook 通道**发不了文件**: 群自定义机器人只有 text/post/interactive,
    没有上传端点。所以没收件人+app 就直接报错, 不默默降级成发文本。
    """
    if not (rid and app):
        raise FeishuError("发文件必须走自建应用 (需收件人 + app); "
                  "群自定义机器人 webhook 不支持上传文件")
    order = [(app_name, app)]
    if fallback:
        for n, a in load_apps().items():
            if a["app_id"] != app["app_id"]:
                order.append((n, a))
    errs = []
    for i, (n, a) in enumerate(order):
        try:
            r = send_file(path, rid, a, api_base=api_base, id_type=id_type,
                          file_type=file_type, name=name)
            who = n or a["app_id"][:12] + "…"
            desc = f"文件→自建应用({who})→{rid}"
            if i:
                desc += f" [首选({app_name or app['app_id'][:12] + '…'})无权限, 已回退]"
            return desc, r
        except FeishuError as e:
            errs.append(f"{n or a['app_id'][:12]}: {e}")
            if "99991672" not in str(e):
                raise
    raise FeishuError("所有 app 都发不出文件: " + " | ".join(errs))


def send(text: str, webhook: str, secret: str = "", title: str = "") -> str:
    """群自定义机器人 webhook 发送 (可选签名校验)。"""
    if not webhook:
        raise FeishuError("没有 webhook")
    c = card_of(clip(text), title)
    body: dict = ({"msg_type": "interactive", "card": json.loads(c["content"])}
                  if title else {"msg_type": "text", "content": json.loads(c["content"])})
    if secret:
        ts = str(int(time.time()))
        body["timestamp"], body["sign"] = ts, sign(secret, ts)
    return _unwrap(_post(webhook, body, {"Content-Type": "application/json"}))


def deliver(text: str, *, webhook: str = "", secret: str = "", rid: str = "",
            app: dict | None = None, app_name: str = "", title: str = "",
            api_base: str = API_BASE, id_type: str = "", fallback: bool = True,
            allow_bot: bool = False) -> tuple[str, str]:
    """选通道发送, 返回 (通道描述, 飞书回执)。

    有收件人就走自建应用 (推荐); 否则走群机器人 webhook。
    自建应用首选报"缺权限"(99991672) 时自动回退到另一个已配 app —— 免得某个 app
    权限还没批下来就把通知搞停。allow_bot=True 才允许在只配了 webhook 时用 webhook。
    """
    if rid and app:
        order = [("", app)]
        if fallback:
            for n, a in load_apps().items():
                if a["app_id"] != app["app_id"]:
                    order.append((n, a))
        if not app_name and order:
            app_name = ""
        errs = []
        for i, (n, a) in enumerate(order):
            try:
                r = send_app(text, rid, a, title, app_name=n, api_base=api_base,
                             id_type=id_type)
                who = n or a["app_id"][:12] + "…"
                desc = f"自建应用({who})→{rid}"
                if i:
                    desc += f" [首选({app_name or app['app_id'][:12] + '…'})无权限, 已回退]"
                return desc, r
            except FeishuError as e:
                errs.append(f"{n or a['app_id'][:12]}: {e}")
                if "99991672" not in str(e):
                    raise
        raise FeishuError("所有 app 都发不出: " + " | ".join(errs))
    if webhook or allow_bot:
        return "群机器人 webhook", send(text, webhook, secret, title)
    raise FeishuError("没有可用通道")


# ─────────────────────────── 自检 ───────────────────────────

def selftest() -> int:
    """内建假飞书 (token + 发消息两个端点), 跑 10 个用例。不碰真群。"""
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[dict] = []
    globals()["TOKEN_CACHE_DIR"] = Path(tempfile.mkdtemp(prefix="lark_selftest_"))

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/"):
                # 上传端点: 不解 JSON, 只记下原始报文供断言 (文件名/字段/字节都在里面)
                seen.append({"path": self.path, "auth": self.headers.get("Authorization", ""),
                             "multipart": raw, "ctype": ctype})
                r = ({"code": 99991672, "msg": "Access denied. scopes required"}
                     if self.headers.get("Authorization") == "Bearer t-cli_bad"
                     else {"code": 0, "data": {"file_key": "file_mock123"}})
            else:
                b = json.loads(raw or b"{}")
                seen.append({"path": self.path, "auth": self.headers.get("Authorization", ""),
                             "body": b})
                # token 端点按 app_id 发不同 token, 好让后续端点区分"缺权限的那个 app"
                r = ({"code": 0, "expire": 7200,
                      "tenant_access_token": "t-" + b.get("app_id", "")}
                     if "tenant_access_token" in self.path else
                     ({"code": 99991672, "msg": "Access denied. scopes required"}
                      if self.headers.get("Authorization") == "Bearer t-cli_bad"
                      else {"code": 0, "data": {"message_id": "om_mock"}}))
            o = json.dumps(r).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(o)))
            self.end_headers()
            self.wfile.write(o)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    ok_app = {"app_id": "cli_selftest", "app_secret": "s"}
    fails: list[str] = []

    def chk(name: str, cond: bool, extra: str = "") -> None:
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + extra) if extra and not cond else ''}")
        if not cond:
            fails.append(name)

    try:
        chk("文本: 发得出且回执可读", "om_mock" in send_app("你好", "oc_test", ok_app, api_base=base))
        b = seen[-1]["body"]
        chk("文本: receive_id / msg_type / content",
            b["receive_id"] == "oc_test" and b["msg_type"] == "text" and "你好" in b["content"],
            str(b)[:120])

        send_app("A\\nB", "oc_test", ok_app, title="T", api_base=base)
        b = seen[-1]["body"]
        chk("卡片: msg_type=interactive", b["msg_type"] == "interactive", str(b)[:120])
        inner = json.loads(b["content"])["elements"][0]["text"]["content"]
        chk("卡片: 字面 \\n 转真换行", "\n" in inner and "\\n" not in inner, repr(inner))

        send_app("x" * 25000, "oc_test", ok_app, api_base=base)
        chk("超长: 截断并标注",
            "已截断, 原 25000 字" in json.loads(seen[-1]["body"]["content"])["text"])

        chk("收件人类型自动判定",
            (id_type_of("oc_1"), id_type_of("ou_1"), id_type_of("on_1"), id_type_of("a@b.c"),
             id_type_of("u1")) == ("chat_id", "open_id", "union_id", "email", "user_id"))

        chk("占位符: 中文 webhook 判致命", cred_problem("https://x/hook/真实KEY")[0] == 2)
        chk("占位符: 正常 webhook 放行", cred_problem(HOOK_PREFIX + "a" * 36)[0] == 0)

        try:
            _unwrap({"code": 19001, "msg": "token invalid"})
            chk("飞书 body code≠0 会抛错 (不假报成功)", False)
        except FeishuError:
            chk("飞书 body code≠0 会抛错 (不假报成功)", True)

        n_tok = sum(1 for s in seen if "tenant_access_token" in s["path"])
        chk("token 缓存生效 (不重复换 token)", n_tok == 1, f"token 端点被调 {n_tok} 次")

        # 回退链路: 首选 app 缺权限(99991672) -> 自动换已配的第二个 app 发出去
        real = load_apps
        globals()["load_apps"] = lambda: {
            "good": {"app_id": "cli_selftest", "app_secret": "s", "chat_id": "oc_test"}}
        try:
            desc, r = deliver("x", rid="oc_test",
                              app={"app_id": "cli_bad", "app_secret": "s"}, api_base=base)
            chk("首选 app 缺权限时自动回退", "已回退" in desc and "om_mock" in r, desc)
        except FeishuError as e:
            chk("首选 app 缺权限时自动回退", False, str(e))
        finally:
            globals()["load_apps"] = real

        # ---- 文件通道 ----
        tmpd = Path(tempfile.mkdtemp(prefix="lark_file_"))
        f_xlsx = tmpd / "判官对比_v1.xlsx"
        f_xlsx.write_bytes(b"PK\x03\x04" + b"x" * 500)          # 假 xlsx, 只验报文
        key = upload_file(f_xlsx, ok_app, api_base=base)
        chk("文件: 上传拿到 file_key", key == "file_mock123", key)
        up = seen[-1]
        chk("文件: 走 multipart 而非 JSON",
            "multipart" in up and up["path"].endswith("/im/v1/files"), up.get("path", ""))
        raw = up.get("multipart", b"")
        chk("文件: file_type 按扩展名 → xls",
            b'name="file_type"' in raw and b"xls" in raw)
        chk("文件: 中文文件名以 UTF-8 原样进 header",
            "判官对比_v1.xlsx".encode() in raw)
        chk("文件: 二进制内容完整带上", b"PK\x03\x04" in raw and b"x" * 500 in raw)

        send_file(f_xlsx, "oc_test", ok_app, api_base=base)
        b2 = seen[-1]["body"]
        chk("文件: 发 msg_type=file + file_key",
            b2["msg_type"] == "file" and json.loads(b2["content"])["file_key"] == "file_mock123",
            str(b2)[:120])

        chk("文件: 未知扩展名 → stream", file_type_of(Path("a.bin")) == "stream")
        try:
            (tmpd / "empty.txt").write_bytes(b"")
            upload_file(tmpd / "empty.txt", ok_app, api_base=base)
            chk("文件: 空文件被拦", False)
        except FeishuError:
            chk("文件: 空文件被拦", True)
        try:
            deliver_file(f_xlsx, rid="", app=None, api_base=base)
            chk("文件: 无收件人/app 直接报错 (不静默降级)", False)
        except FeishuError as e:
            chk("文件: 无收件人/app 直接报错 (不静默降级)", "webhook" in str(e))

        globals()["load_apps"] = lambda: {
            "good": {"app_id": "cli_selftest", "app_secret": "s", "chat_id": "oc_test"}}
        try:
            desc, r = deliver_file(f_xlsx, rid="oc_test",
                                   app={"app_id": "cli_bad", "app_secret": "s"}, api_base=base)
            chk("文件: 首选 app 缺权限时自动回退", "已回退" in desc and "om_mock" in r, desc)
        except FeishuError as e:
            chk("文件: 首选 app 缺权限时自动回退", False, str(e))
        finally:
            globals()["load_apps"] = real

    finally:
        srv.shutdown()
    print(f"\n自检: {'全部通过' if not fails else '失败 ' + ', '.join(fails)}")
    return 0 if not fails else 8


def main() -> int:
    ap = argparse.ArgumentParser(
        description="发飞书消息: 自建应用(群/私聊) 或 群自定义机器人(webhook)")
    ap.add_argument("--text", default="")
    ap.add_argument("--stdin", action="store_true", help="把标准输入当正文")
    ap.add_argument("--title", default="", help="带标题则发卡片 (lark_md)")
    ap.add_argument("--to", default="", help="收件人: oc_群 / ou_用户 / 邮箱 / user_id")
    ap.add_argument("--chat-id", default="", help="--to 的同义 (群)")
    ap.add_argument("--id-type", default="", help="强制 receive_id_type (默认按长相判)")
    ap.add_argument("--app", default="", help="用哪个 app (见 .feishu_apps.json)")
    ap.add_argument("--app-id", default="")
    ap.add_argument("--app-secret", default="")
    ap.add_argument("--webhook", default="")
    ap.add_argument("--api-base", default=API_BASE, help="测试时可指向本地 mock")
    ap.add_argument("--file", default="",
                    help="发文件 (im/v1/files 上传后发 msg_type=file)。可与 --text 共用: "
                         "先发文本/卡片再发文件。仅自建应用通道支持。")
    ap.add_argument("--file-name", default="", help="覆盖发送给飞书的文件名")
    ap.add_argument("--file-type", default="",
                    help="强制 file_type (xls/doc/pdf/mp4/stream…); 默认按扩展名定")
    ap.add_argument("--dedup-key", default="", help="同 key 在 --dedup-ttl 秒内只发一次")
    ap.add_argument("--dedup-ttl", type=int, default=600)
    ap.add_argument("--dry", action="store_true", help="只报告将发什么/走哪条通道")
    ap.add_argument("--status", action="store_true", help="报告通道/凭证 (会验 token)")
    ap.add_argument("--selftest", action="store_true", help="跑内建假飞书自检")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    w, s = load_cred(a.webhook)
    lvl, bad = cred_problem(w)
    name, app = load_app(a.app, a.app_id, a.app_secret)
    rid = load_chat(a.to or a.chat_id, app)

    if a.status:
        print("── 通道 ──")
        srcw = ("参数" if a.webhook else "环境变量" if os.environ.get("FEISHU_WEBHOOK") else "文件")
        print(f"webhook: {'已配 [' + srcw + '] ' + HOOK_PREFIX + '***' if w else '未配'}")
        if bad and not rid:
            print(f"   {'✗' if lvl == 2 else '⚠'} {bad}")
        elif bad:
            print(f"   (webhook 不可用: {bad} —— 已有收件人, 不影响)")
        print(f"secret : {'已配' if s else '无 (机器人没开签名校验就正常)'}")
        print(f"收件人 : {rid or '未配'}  (类型 {id_type_of(rid) if rid else '-'})")
        print("── 应用 ──")
        apps = load_apps()
        if not apps:
            print("(未配 .feishu_apps.json / .lark_key)")
        for n, v in apps.items():
            mark = "*" if n == (name or default_app_name(apps)) else " "
            print(f" {mark} {n}: {v['app_id'][:14]}… secret {v['app_secret'][:4]}… "
                  f"chat={v.get('chat_id') or '-'}")
        if name:
            try:
                tenant_token(app["app_id"], app["app_secret"])
                print(f"   token: ✓ ({name})")
            except FeishuError as e:
                print(f"   token: ✗ ({name}) {e}")
        print("── 凭证搜索链 (找到第一个就用) ──")
        for r in _cred_roots():
            hit = [n for n in (".feishu_apps.json", ".lark_key", ".feishu_chat",
                               ".feishu_owner", ".feishu_app",
                               ".feishu_webhook", ".feishu_secret")
                   if (r / n).exists()]
            print(f"  {r}{'  ← ' + ', '.join(hit) if hit else ''}")
        print("── 实际用的文件 ──")
        print(f"  {APPS_FILE} / {LARK_KEY_FILE} / {DEFAULT_CHAT_FILE} / {APP_NAME_FILE}")
        print(f"  {DEFAULT_WEBHOOK_FILE} / {DEFAULT_SECRET_FILE}")
        print("→ 通道:", f"自建应用 {name or '?'} → {rid}" if (rid and app) else
              ("群机器人 webhook" if (lvl < 2 and w) else "无可用通道"))
        return 0 if ((rid and app) or lvl < 2) else 4

    text = sys.stdin.read() if a.stdin else a.text
    fpath = Path(os.path.expanduser(a.file)) if a.file else None
    if fpath and not fpath.is_file():
        print(f"✗ 文件不存在: {fpath}", file=sys.stderr)
        return 9
    # 带 --file 时正文可以为空 (光发文件); 不带 --file 则必须有正文
    if not text.strip() and not fpath:
        print("没内容: 用 --text / --stdin / --file", file=sys.stderr)
        return 5
    if a.dry:
        ch = (f"自建应用({name or app.get('app_id', '')[:12]}…) → {rid} [{id_type_of(rid)}]"
              if (rid and app) else (f"群机器人 webhook (正文 {len(text)} 字)"
                                     if lvl < 2 else "✗ 无可用通道"))
        if text.strip():
            print(f"(--dry) {ch}, {'卡片' if a.title else '文本'}, {len(text)} 字"
                  f"{', 截断到 ' + str(MAX_CHARS) if len(text) > MAX_CHARS else ''}")
        if fpath:
            sz = fpath.stat().st_size
            note = "" if (rid and app) else "  ✗ webhook 通道发不了文件"
            print(f"(--dry) 文件 {fpath.name} {sz / 1048576:.2f}MB "
                  f"file_type={a.file_type or file_type_of(fpath)} → {rid or '?'}{note}"
                  + ("  ✗ 超 30MB" if sz > FILE_MAX else ""))
        return 0
    if not rid and lvl == 2:
        print(f"✗ 不发送: {bad}\n  两条路: ① 写收件人到 {DEFAULT_CHAT_FILE} + 配 {APPS_FILE};"
              f" ② 写 webhook 到 {DEFAULT_WEBHOOK_FILE}", file=sys.stderr)
        return 6
    if a.dedup_key:
        f = TOKEN_CACHE_DIR / ("feishu_sent_" + hashlib.md5(a.dedup_key.encode()).hexdigest()[:10])
        if f.exists() and time.time() - f.stat().st_mtime < a.dedup_ttl:
            print(f"(跳过: dedup-key={a.dedup_key} 在 {a.dedup_ttl}s 内已发过)")
            return 0
    if not rid and lvl == 1:
        print(f"⚠ {bad}", file=sys.stderr)
    try:
        if text.strip():
            desc, r = deliver(text, webhook=w, secret=s, rid=rid, app=app, app_name=name,
                              title=a.title, api_base=a.api_base, id_type=a.id_type,
                              allow_bot=not rid)
            print(f"[{desc}] 飞书返回: {r}")
        if fpath:
            desc, r = deliver_file(fpath, rid=rid, app=app, app_name=name,
                                   api_base=a.api_base, id_type=a.id_type,
                                   file_type=a.file_type, name=a.file_name)
            print(f"[{desc}] 飞书返回: {r}")
    except FeishuError as e:
        print(f"✗ 发送失败: {e}", file=sys.stderr)
        return 7
    if a.dedup_key:
        (TOKEN_CACHE_DIR / ("feishu_sent_" +
                            hashlib.md5(a.dedup_key.encode()).hexdigest()[:10])).write_text(str(time.time()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
