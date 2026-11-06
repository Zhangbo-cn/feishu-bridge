#!/usr/bin/env python3
"""飞书 → 本机 pi agent 的远程控制桥。

在群里轮询指定 owner 的文本消息当成指令:

    /ping              连通性
    /status            机器状态(主机/负载/内存/GPU/关键进程/盘)
    /ps [关键字]        进程
    /tail <文件> [n]    看日志尾部
    /send <文件>        把本机文件上传发回飞书 (报表/日志)
    /run <命令>         执行 shell (需启动时加 --allow-run)
    /help              帮助
    /stop              停掉本桥
    其他任何文字        交给 pi agent 跑一个回合, 结果回飞书

pi agent 用**独立会话**(默认 --session-id feishu-bridge), 不会污染你在终端里的会话。

    nohup .venv/bin/python scripts/feishu_agent_bridge.py \\
        --owner ou_xxxx > /tmp/feishu_bridge.log 2>&1 &

只处理 sender_type=user 且 open_id 命中 --owner 的消息; 机器人/其他群成员说的话一律忽略。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from feishu_send import (  # noqa: E402
    API_BASE,
    FeishuError,
    MAX_CHARS,
    _read1,
    cred_path,
    deliver,
    deliver_file,
    load_app,
    load_apps,
    load_chat,
    read_app,
    tenant_token,
)
from feishu_watch import (  # noqa: E402
    advance as watch_advance,
    list_text as watch_list_text,
    load_state as load_watches,
    parse as watch_parse,
    probe as watch_probe,
    save_state as save_watches,
    status_text as watch_status_text,
)

# 收件人 / owner 不写死: 从凭证链读 (见 feishu_send.cred_path)。
# .feishu_owner 一行 open_id —— 只有它发的消息算指令, 其他人/机器人全忽略。
# 空着不填 = 任何人都能驱动这台机器, 危险, 所以 loop() 会先警告。
ACK_CHARS = 60
CLIP_OUT = 12000 # 回飞书的正文上限(留余量给标题/路径), 超了存文件
WORKDIR_DEFAULT = str(Path.cwd())          # 跟着启动目录跑, 不是 skill 目录


# ─────────────────────────── 凭证 / 读消息 ───────────────────────────

def fetch_messages(app: dict, chat: str, page_size: int = 20) -> list[dict]:
    tk = tenant_token(app["app_id"], app["app_secret"])
    r = requests.get(
        f"{API_BASE}/open-apis/im/v1/messages",
        params={"container_id_type": "chat", "container_id": chat,
                "sort_type": "ByCreateTimeDesc", "page_size": page_size},
        headers={"Authorization": f"Bearer {tk}"}, timeout=20)
    d = r.json()
    if d.get("code"):
        raise FeishuError(f"读消息失败 {d.get('code')}: {str(d.get('msg'))[:120]}")
    return list(reversed((d.get("data") or {}).get("items") or []))   # 时间升序


def _body(item: dict) -> dict:
    """消息 body.content 是 **JSON 字符串**, 解出来。坏 json 不能让轮询挂掉。"""
    try:
        return json.loads((item.get("body") or {}).get("content") or "{}")
    except Exception:                                  # noqa: BLE001
        return {}


def msg_text(item: dict) -> str:
    """消息里的文字。

    text  → {"text": "..."}
    post  → 富文本: {"content": [[{"tag":"text","text":"..."}, ...], ...]}
            **带附件发消息时飞书就发 post** —— 文字和文件在同一条里。
            只认 text/a/at 这几个 tag 的可见文字, 其余(img 等)跳过。
    file/image → 没有文字, 返回 ""; 文件本身由 msg_files() 取。
    """
    mt = item.get("msg_type")
    b = _body(item)
    if mt == "text":
        return str(b.get("text") or "").strip()
    if mt == "post":
        out = []
        for line in (b.get("content") or []):
            for seg in (line or []):
                if isinstance(seg, dict) and seg.get("tag") in ("text", "a", "at"):
                    out.append(str(seg.get("text") or seg.get("href") or ""))
            out.append("\n")
        return "".join(out).strip()
    return ""


def msg_files(item: dict) -> list:
    """消息里的附件 [(file_key, 文件名, 类型)]。类型 = file | image。

    三种载体:
      msg_type=file   {"file_key": "...", "file_name": "..."}
      msg_type=image  {"image_key": "..."}          ← 键名不一样, 别漏
      msg_type=post   富文本里混排 {"tag":"img"/"file", ...}
    """
    mt = item.get("msg_type")
    b = _body(item)
    out = []
    if mt == "file":
        k = b.get("file_key")
        if k:
            out.append((k, b.get("file_name") or "", "file"))
    elif mt == "image":
        k = b.get("image_key") or b.get("file_key")
        if k:
            out.append((k, b.get("file_name") or "", "image"))
    elif mt == "post":
        # 附件可能挂在顶层 files/images, 也可能混在 content 的 seg 里
        for f in (b.get("files") or []):
            if f.get("file_key"):
                out.append((f["file_key"], f.get("file_name") or "", "file"))
        for im in (b.get("images") or []):
            k = im.get("image_key") or im.get("file_key")
            if k:
                out.append((k, "", "image"))
        for line in (b.get("content") or []):
            for seg in (line or []):
                if not isinstance(seg, dict):
                    continue
                if seg.get("tag") == "img":
                    k = seg.get("image_key") or seg.get("file_key")
                    if k:
                        out.append((k, "", "image"))
                elif seg.get("tag") == "file" and seg.get("file_key"):
                    out.append((seg["file_key"], seg.get("file_name") or "", "file"))
    # 同一个 key 可能在 files 和 content 里各出现一次 → 去重, 保序
    seen, uniq = set(), []
    for k, nm, ty in out:
        if k not in seen:
            seen.add(k)
            uniq.append((k, nm, ty))
    return uniq


def safe_name(name: str, fallback: str = "file") -> str:
    """文件名消毒: 去掉路径分隔符与控制字符, 防 ../ 穿越。

    飞书的文件名来自用户, 直接拿去拼路径等于让远端决定往哪写 —— 这是入站方向
    最危险的一步。只保留 basename, 再滤掉可疑字符。
    """
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r'[<>:"|?*]', "_", name).strip(" .")
    return name or fallback


def download_resource(app: dict, message_id: str, file_key: str, dest: Path,
                   res_type: str = "file") -> int:
    """下载消息附件到 dest, 返回字节数。需权限 im:resource。

    端点是 messages/{message_id}/resources/{file_key}?type=file|image
    —— 必须带 message_id, 光有 file_key 下不了 (这是飞书的设计, 附件绑在消息上)。
    """
    tk = tenant_token(app["app_id"], app["app_secret"])
    r = requests.get(
        f"{API_BASE}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
        params={"type": res_type},
        headers={"Authorization": f"Bearer {tk}"}, timeout=300, stream=True)
    if r.status_code != 200:
        # 失败时 body 是 JSON 错误; 成功时是二进制流
        try:
            d = r.json()
            raise FeishuError(f"下载失败 code={d.get('code')} msg={d.get('msg')}")
        except ValueError:
            raise FeishuError(f"下载失败 HTTP {r.status_code}: {r.text[:200]}") from None
    ct = (r.headers.get("Content-Type") or "").lower()
    if ct.startswith("application/json"):      # 200 但其实是错误体
        raise FeishuError(f"下载失败(200 但返回 JSON): {r.text[:200]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with dest.open("wb") as f:
        for chunk in r.iter_content(65536):
            if chunk:
                f.write(chunk)
                n += len(chunk)
    return n


def extract_text(path: Path, max_chars: int = 200_000) -> dict:
    """调同目录的 extract_doc.py 抽正文。返回 {ok, kind, chars, truncated, text|error}。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from extract_doc import extract          # noqa: PLC0415
        return extract(path, max_chars)
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"解析器异常 {type(e).__name__}: {e}"}


# ─────────────────────────── 本地命令 ───────────────────────────

def sh(cmd: str, timeout: int = 20) -> str:
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"(超时 {timeout}s)"
    except Exception as e:                                   # noqa: BLE001
        return f"(失败 {type(e).__name__}: {e})"


def cmd_status() -> str:
    parts = [f"主机 {socket.gethostname()} | {time.strftime('%m-%d %H:%M:%S')}",
             sh("uptime | sed 's/^ *//'"),
             sh("free -g | awk 'NR<=2{print}'"),
             sh("df -h / /home 2>/dev/null | tail -n +2"),
             "GPU: " + sh("nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total "
                          "--format=csv,noheader 2>/dev/null || echo '(无 nvidia-smi)'"),
             "进程: " + sh("ps -ef | grep -E 'feishu_agent_bridge|notify_feishu|rag' | grep -v grep "
                           "| awk '{print $2, $8, $9, $10}' | head -8")]
    return "\n".join(parts)


def cmd_ps(pat: str) -> str:
    pat = pat or "python|pi "
    return sh(f"ps -ef | grep -E {shlex.quote(pat)} | grep -v grep | head -25")


def cmd_tail(args: str) -> str:
    toks = args.split()
    if not toks:
        return "用法: /tail <文件> [行数]"
    f = os.path.expanduser(toks[0])
    n = int(toks[1]) if len(toks) > 1 and toks[1].isdigit() else 40
    n = max(1, min(n, 300))
    if not Path(f).exists():
        return f"没有这个文件: {f}"
    return sh(f"tail -n {n} {shlex.quote(f)}")


def cmd_run(cmd: str, allow: bool, timeout: int) -> str:
    if not allow:
        return "没开执行权限。要开放请重启时加 --allow-run。"
    if not cmd.strip():
        return "用法: /run <命令>"
    return sh(cmd, timeout=timeout)


def help_text(allow_run: bool) -> str:
    return ("🤖 **pi agent 远程桥**\n"
            "直接发需求文字 → 我(独立会话)在本机执行并把结果回你\n\n"
            "**直接把文件拖进群** → 我下载+解析; 同时打字说要求 → 我按要求处理那个文件\n\n"
            "/ping  /status  /ps [关键字]  /tail <文件> [行数]  /help  /stop\n"
            "/watch              看护中的任务\n"
            "/watch add <名> <日志>   盯日志推进度 (事件即时, 进度限流, 空跑发心跳)\n"
            "/watch now <名>      立刻取一次当前状态\n"
            "/watch rm <名>\n"
            "/send <本机路径>   把这台机器上的文件发回群 (出站, 与上面相反)\n"
            + ("/run <命令>  执行 shell\n" if allow_run else "/run          (未开启, 需 --allow-run)\n")
            + f"\n单条回执上限 {MAX_CHARS} 字, 超长正文落盘给路径。当前会话: pi --session-id <见 --session-id>")


# ─────────────────────────── pi agent ───────────────────────────

def run_agent(text: str, pi_bin: str, session_id: str, workdir: Path,
              timeout: int, model: str = "", extra: list[str] | None = None) -> tuple[int, str]:
    cmd = [pi_bin, "--session-id", session_id, "-p", "--approve"]
    if model:
        cmd += ["--model", model]
    cmd += list(extra or [])
    cmd.append(text)
    try:
        p = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            out += f"\n\n[exit {p.returncode}] {err[-1500:]}"
        return p.returncode, out or "(agent 没有输出)"
    except subprocess.TimeoutExpired as e:
        got = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, f"⏱ agent 超过 {timeout}s 还没跑完, 已中断。已产出:\n{got[-3000:]}"


# ─────────────────────────── 派发 ───────────────────────────

class Bridge:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        apps = load_apps()
        if not apps:
            raise SystemExit("没有 .feishu_apps.json / .lark_key, 发不了也读不了。"
                             "凭证放哪里看: feishu_send.py --status")
        self.read_name, read_api = read_app(apps)
        self.apps = apps
        self.chat = load_chat(a.chat, read_api) or a.chat
        if not self.chat:
            raise SystemExit("没收件人: 给 --chat 或写 .feishu_chat (一行 oc_…)")
        # owner 白名单: --owner > .feishu_owner。空着 = 任何人能驱动这台机器 → 开跑前警告。
        self.owner = (a.owner or _read1(cred_path(".feishu_owner"))).strip()
        self.token_app = read_api
        self.state_path = Path(a.state)
        st = {}
        if self.state_path.exists():
            try:
                st = json.loads(self.state_path.read_text())
            except Exception:                                # noqa: BLE001
                st = {}
        # 入站文件落盘目录。默认 <workdir>/.feishu_inbox/<日期>/
        self.inbox = Path(a.inbox).expanduser() if a.inbox else \
            Path(a.workdir) / ".feishu_inbox"
        self.seen: list[str] = st.get("seen", [])[-500:]
        self.last_ts: int = int(st.get("last_ts") or 0)
        if not self.last_ts:                                 # 首次: 只看启动之后的消息
            self.last_ts = int(time.time() * 1000)

    # ---- 状态 ----
    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"seen": self.seen[-500:], "last_ts": self.last_ts},
                                              ensure_ascii=False, indent=1))

    # ---- 回信 ----
    def say(self, text: str, title: str = "") -> None:
        if self.a.dry:
            print(f"[dry→飞书] {title}\n{text[:400]}")
            return
        try:
            desc, _ = deliver(text[:MAX_CHARS], rid=self.chat, app=self.token_app,
                              id_type="chat_id", title=title, fallback=True)
            print(f"[发] {desc} {len(text)} 字")
        except FeishuError as e:
            print(f"[发失败] {e}", file=sys.stderr)

    def say_long(self, text: str, title: str = "") -> None:
        if len(text) <= CLIP_OUT:
            self.say(text, title)
            return
        p = Path(f"/tmp/feishu_bridge_out_{time.strftime('%m%d_%H%M%S')}.txt")
        p.write_text(text)
        self.say(text[:CLIP_OUT] + f"\n\n…(共 {len(text)} 字, 全文见 {p})", title)

    def say_file(self, path: Path, note: str = "") -> None:
        """把本机文件发回飞书 (im/v1/files 上传 + msg_type=file)。"""
        if self.a.dry:
            print(f"[dry→飞书] 文件 {path}")
            return
        try:
            if note:
                self.say(note, "文件")
            desc, _ = deliver_file(path, rid=self.chat, app=self.token_app,
                                   app_name=self.read_name, id_type="chat_id")
            print(f"[发] {desc} {path.stat().st_size} 字节")
        except FeishuError as e:
            print(f"[发文件失败] {e}", file=sys.stderr)
            self.say(f"文件发不出去: {e}\n本机路径: {path}", "发送失败")

    # ---- 附件 ----
    def handle_files(self, item: dict, files: list, text: str) -> bool:
        """下载附件 → 解析正文 → (带文字则交 agent, 否则回摘要)。

        口径: **只给 agent 路径 + 预览, 不把全文塞进 prompt**。
        理由是 agent 有 read 工具, 能按需读、能反复读、能只读某一节; 而全文入 prompt
        一次就烧掉几万字上下文, 大文件还会直接撑爆单轮。
        """
        mid = item.get("message_id") or ""
        day = time.strftime("%Y%m%d")
        saved = []
        for key, name, res_type in files:
            fname = safe_name(name, f"{res_type}_{key[-8:]}")
            if res_type == "image" and "." not in fname:
                fname += ".png"
            dest = self.inbox / day / fname
            if dest.exists():                    # 同名不覆盖: 加时分秒
                dest = dest.with_name(f"{dest.stem}_{time.strftime('%H%M%S')}{dest.suffix}")
            if self.a.dry:
                print(f"[dry] 下载 {fname} → {dest}")
                saved.append((dest, {"ok": True, "kind": "dry", "chars":0,
                         "truncated": False, "text": ""}))
                continue
            try:
                n = download_resource(self.token_app, mid, key, dest, res_type)
                print(f"[下载] {dest} {n} 字节")
            except FeishuError as e:
                print(f"[下载失败] {fname}: {e}", file=sys.stderr)
                self.say(f"下载失败: {fname}\n{e}\n"
                         f"(要 im:resource 权限; 当前读消息用的 app={self.read_name})",
                         "下载失败")
                continue
            info = extract_text(dest, self.a.max_extract)
            saved.append((dest, info))

        if not saved:
            return True

        # 回执: 每个文件报类型/字数/落盘路径, 解析失败也说清为什么
        lines = []
        for dest, info in saved:
            size = dest.stat().st_size if dest.exists() else 0
            if info.get("ok"):
                lines.append(f"✓ {dest.name}  [{info['kind']}] {size} 字节 → 正文 "
                             f"{info['chars']} 字{' (已截断)' if info.get('truncated') else ''}")
            else:
                lines.append(f"⚠ {dest.name}  {size} 字节 — 解析不了: {info.get('error')}")
            lines.append(f"   {dest}")
        head = "\n".join(lines)

        if not text.strip():
            # 光发文件不带话: 存下来 + 报摘要, 不猜用户想干什么
            prev = ""
            for dest, info in saved:
                if info.get("ok") and info.get("text"):
                    prev = info["text"][:800]
                    break
            self.say(head + (f"\n\n── 开头 ──\n{prev}" if prev else "") +
                     "\n\n要我做什么? 直接说 (例: 总结要点 / 挑错 / 转成表格)。",
                     "已收到文件")
            return True

        # 带话: 把文件清单 + 预览拼进 prompt, 正文让 agent 自己 read
        ctx = ["[飞书附件] 以下文件已下载到本机, 用 read 工具按需读取:"]
        for dest, info in saved:
            if info.get("ok"):
                ctx.append(f"- {dest}  ({info['kind']}, 正文约 {info['chars']} 字)")
                if info.get("text"):
                    ctx.append(f"  开头: {info['text'][:400]}")
            else:
                ctx.append(f"- {dest}  (解析失败: {info.get('error')})")
        ctx.append("")
        ctx.append("注意: docx/xlsx/pptx/pdf 不能直接 read, 要先抽正文:")
        ctx.append(f"  python3 {Path(__file__).resolve().parent / 'extract_doc.py'} <文件>")
        ctx.append("")
        ctx.append(f"用户的要求: {text.strip()}")
        prompt = "\n".join(ctx)

        self.say(head + f"\n\n🤖 {text.strip()[:ACK_CHARS]}\n开始跑…", "已收到文件")
        t0 = time.time()
        rc, out = run_agent(prompt, self.a.pi_bin, self.a.session_id,
                            Path(self.a.workdir), self.a.timeout, self.a.model)
        print(f"[agent] rc={rc} {time.time() - t0:.1f}s {len(out)} 字")
        self.say_long(out, f"pi agent ({time.time() - t0:.0f}s)")
        return True

    # ---- 一条消息 ----
    def handle(self, text: str) -> bool:
        """返回 False 表示要退出。"""
        low = text.strip()
        cmd, _, rest = low.partition(" ")
        cmd = cmd.lower()
        print(f"[收] {text[:200]}")
        if cmd in ("/help", "帮助"):
            self.say(help_text(self.a.allow_run), "帮助")
        elif cmd == "/ping":
            self.say(f"pong 🏓 {socket.gethostname()} {time.strftime('%m-%d %H:%M:%S')}", "pong")
        elif cmd == "/status":
            self.say(cmd_status(), "机器状态")
        elif cmd == "/ps":
            self.say(cmd_ps(rest), "进程")
        elif cmd == "/tail":
            self.say_long(cmd_tail(rest), "tail")
        elif cmd == "/run":
            self.say_long(cmd_run(rest, self.a.allow_run, self.a.run_timeout), "run")
        elif cmd == "/watch":
            self.say_long(self.cmd_watch(rest), "看护")
        elif cmd in ("/send", "/file"):
            f = os.path.expanduser(rest.strip().strip('"\''))
            if not f:
                self.say("用法: /send <本机文件路径>", "send")
            elif not Path(f).is_file():
                self.say(f"没有这个文件: {f}", "send")
            else:
                self.say_file(Path(f))
        elif cmd == "/stop":
            self.say("桥已停。重启: 见 skill 的「启动」一节 (setsid nohup … feishu_bridge.py &)",
                     "停止")
            return False
        else:
            self.say(f"🤖 收到: {low[:ACK_CHARS]}{'…' if len(low) > ACK_CHARS else ''}\n开始跑(会话 {self.a.session_id})…",
                     "已收到")
            t0 = time.time()
            rc, out = run_agent(low, self.a.pi_bin, self.a.session_id,
                                Path(self.a.workdir), self.a.timeout, self.a.model)
            print(f"[agent] rc={rc} {time.time() - t0:.1f}s {len(out)} 字")
            self.say_long(out, f"pi agent ({time.time() - t0:.0f}s)")
        return True

    # ---- 看护 ----
    def cmd_watch(self, rest: str) -> str:
        """看护名单管理: 空 / add <名> <日志或命令> / rm <名>。"""
        sub, _, arg = rest.strip().partition(" ")
        st = load_watches()
        if not sub or sub == "ls":
            return watch_list_text(st)
        if sub == "add":
            name, _, src = arg.strip().partition(" ")
            src = src.strip().strip('"\'')
            if not name or not src:
                return "用法: /watch add <名> <日志路径>"
            p = Path(os.path.expanduser(src))
            w = {"name": name, "log": str(p) if p.exists() else src, "cmd": "" if p.exists() else src,
                 "every": 900, "idle": 3600, "check": 60, "created": time.time(),
                 "checked": 0, "finished": False, "reported": {}}
            st["watches"][name] = w
            save_watches(st)
            return f"已看护 {name}: {src}\n新臂/开跑/结束/eval 立即发, 进度每 15 分钟最多一条, 没变化每小时一条心跳。"
        if sub in ("now", "status"):
            name = arg.strip()
            w = st["watches"].get(name)
            if not w:
                return f"没有看护项 {name}"
            return watch_status_text(name, watch_parse(watch_probe(w, self.a.watch_timeout)))
        if sub in ("rm", "del"):
            if arg.strip() not in st["watches"]:
                return f"没有 {arg.strip()}"
            st["watches"].pop(arg.strip())
            save_watches(st)
            return f"已取消 {arg.strip()}"
        return "用法: /watch | /watch add <名> <日志> | /watch now <名> | /watch rm <名>"

    def tick_watches(self) -> None:
        """每轮轮询后推进看护项。不占 agent 回合, 状态变化各自发一条。"""
        st = load_watches()
        if not st.get("watches"):
            return
        moved = False
        for name, w in st["watches"].items():
            if w.get("finished"):
                continue
            try:
                msg = watch_advance(w, time.time(), self.a.watch_timeout)
            except Exception as e:                            # noqa: BLE001
                print(f"[看护异常] {name}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if msg:
                moved = True
                print(f"[看护] {name} 有新状态", flush=True)
                self.say_long(msg, "进度")
        if moved:
            save_watches(st)

    # ---- 轮询 ----
    def poll_once(self) -> int:
        n = 0
        for it in fetch_messages(self.token_app, self.chat, self.a.page_size):
            mid = it.get("message_id", "")
            ts = int(it.get("create_time") or 0)
            if not mid or mid in self.seen or ts <= self.last_ts:
                continue
            self.seen.append(mid)
            self.last_ts = max(self.last_ts, ts)
            sender = it.get("sender", {}) or {}
            if sender.get("sender_type") != "user":
                continue
            if self.owner and sender.get("id") != self.owner:
                print(f"[跳过] 非 owner {sender.get('id')}")
                continue
            text = msg_text(it)
            files = msg_files(it)
            if text.startswith("#"):          # 开头 = 群里的笔记, 不当指令
                continue
            if not text and not files:        # 表情/撤回/系统消息之类
                continue
            self.save()
            n += 1
            print(f"[收] type={it.get('msg_type')} 文字={len(text)}字 附件={len(files)}")
            if files:
                if not self.handle_files(it, files, text):
                    return -1
            elif not self.handle(text):
                return -1
        self.save()
        return n

    def backoff_delay(self, fails: int) -> float:
        """连续失败后的等待秒数。

        前 `--backoff-after` 次保持原间隔: 飞书的 DNS 抖动与 2200 大约几秒就过去,
        退得太快反而放慢恢复。超过后翻倍增长, 上限 `--backoff-max`, 再加 0.8—1.2
        的抖动 —— 多个桥或多次重启同时撞上同一个限流窗口时不会同步反弹。
        """
        after = max(int(self.a.backoff_after), 1)
        if fails <= after:
            return self.a.interval
        step = fails - after
        base = self.a.interval * (2 ** min(step, 10))
        delay = min(base, float(self.a.backoff_max))
        return delay * random.uniform(0.8, 1.2)

    def loop(self) -> int:
        print(f"桥启动: chat={self.chat} owner={self.owner or '(无白名单!)'} "
              f"读={self.read_name} 间隔={self.a.interval}s 会话={self.a.session_id} "
              f"cwd={self.a.workdir}", flush=True)
        if not self.owner:
            print("⚠ 没配 owner 白名单: 群里**任何人**发的话都会在这台机器上执行。"
                  "建议写 .feishu_owner 或给 --owner。", flush=True)
        if self.a.allow_run:
            print("⚠ --allow-run 已开: /run 能执行任意 shell。", flush=True)
        fails = 0
        while True:
            try:
                if self.poll_once() < 0:
                    return 0
                self.tick_watches()
                if fails:
                    print(f"[轮询恢复] 连续失败 {fails} 次后恢复", flush=True)
                    fails = 0
                delay = self.a.interval
            except FeishuError as e:
                fails += 1
                delay = self.backoff_delay(fails)
                print(f"[轮询失败] {e} (第 {fails} 次, 等 {delay:.0f}s)", file=sys.stderr)
            except Exception as e:                            # noqa: BLE001
                fails += 1
                delay = self.backoff_delay(fails)
                print(f"[轮询异常] {type(e).__name__}: {e} (第 {fails} 次, 等 {delay:.0f}s)",
                      file=sys.stderr)
            time.sleep(delay)


# ─────────────────────────── 自检 ───────────────────────────

def selftest() -> int:
    ok = True

    def chk(cond: bool, msg: str) -> None:
        nonlocal ok
        ok = ok and bool(cond)
        print(("  ✓ " if cond else "  ✗ ") + msg)

    print("selftest: 消息解析")
    chk(msg_text({"msg_type": "text", "body": {"content": '{"text":"你好"}'}}) == "你好", "text 解出正文")
    chk(msg_text({"msg_type": "image", "body": {"content": "{}"}}) == "", "非文本忽略")
    chk(msg_text({"msg_type": "text", "body": {"content": "坏 json"}}) == "", "坏 json 不炸")

    print("selftest: 附件解析")
    chk(msg_files({"msg_type": "file",
                   "body": {"content": '{"file_key":"fk1","file_name":"a.docx"}'}})
        == [("fk1", "a.docx", "file")], "file 消息取到 file_key")
    chk(msg_files({"msg_type": "image", "body": {"content": '{"image_key":"ik1"}'}})
        == [("ik1", "", "image")], "image 消息用 image_key (键名不同)")
    post_body = ('{"content":[[{"tag":"text","text":"\u603b\u7ed3\u4e00\u4e0b"}]],'
                 '"files":[{"file_key":"fk2","file_name":"b.xlsx"}]}')
    chk(msg_text({"msg_type": "post", "body": {"content": post_body}}) == "总结一下",
        "post 取到正文 (带附件发消息时飞书发 post)")
    chk(msg_files({"msg_type": "post", "body": {"content": post_body}})
        == [("fk2", "b.xlsx", "file")], "post 取到附件")
    dup = '{"files":[{"file_key":"k","file_name":"x"}],"content":[[{"tag":"file","file_key":"k"}]]}'
    chk(len(msg_files({"msg_type": "post", "body": {"content": dup}})) == 1,
        "同一 key 出现两次会去重")
    chk(msg_files({"msg_type": "text", "body": {"content": '{"text":"hi"}'}}) == [],
        "纯文本没有附件")
    chk(safe_name("../../etc/passwd") == "passwd", "文件名防路径穿越")
    chk(safe_name('a<b>c.docx') == "a_b_c.docx", "文件名滤掉非法字符")
    chk(safe_name("") == "file", "空文件名有兜底")

    print("selftest: 指令派发 (dry)")
    a = argparse.Namespace(chat="oc_selftest", owner="ou_selftest", interval=1, page_size=20,
                           session_id="feishu-bridge-selftest", timeout=5, model="", pi_bin="pi",
                           workdir=str(Path.cwd()), allow_run=False, run_timeout=10, dry=True,
                           inbox="/tmp/feishu_inbox_selftest", max_extract=200_000,
                           watch_timeout=5, backoff_after=3, backoff_max=300,
                           state="/tmp/feishu_bridge_selftest_state.json")
    b = Bridge(a)
    for t in ["/ping", "/help", "/ps python", "/tail /etc/hostname 3", "/run echo hi",
              "/status", "/send /etc/hostname", "/send /nope/nope", "/watch", "/watch now 没有"]:
        chk(b.handle(t) is True, f"处理 {t.split()[0]} {t.split()[1] if len(t.split())>1 else ''}")
    chk(b.handle("/stop") is False, "/stop 请求退出")

    print("selftest: 轮询退避")
    chk(b.backoff_delay(1) == b.a.interval, "前几次失败保持原间隔")
    chk(b.backoff_delay(3) == b.a.interval, "第 3 次仍不延长")
    d4 = b.backoff_delay(4)
    chk(0.8 * 2 <= d4 <= 1.2 * 2, f"第 4 次翻倍到 2s 附近 (实际 {d4:.2f})")
    d20 = b.backoff_delay(20)
    chk(0.8 * 300 <= d20 <= 1.2 * 300, f"退避有上限且带抖动 (实际 {d20:.1f})")
    chk(len({round(b.backoff_delay(6), 3) for _ in range(8)}) > 1, "抖动确实在变")
    print("selftest:", "通过" if ok else "失败")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书 → 本机 pi agent 远程控制桥")
    ap.add_argument("--chat", default="",
                    help="群 chat_id。默认从凭证链读 .feishu_chat / app 的 chat_id")
    ap.add_argument("--owner", default="",
                    help="只认这个 open_id 的指令。默认读 .feishu_owner; "
                         "显式传 --owner '' 之外留空即无白名单 (危险)")
    ap.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒")
    ap.add_argument("--page-size", type=int, default=20)
    ap.add_argument("--session-id", default="feishu-bridge", help="pi agent 会话 id (独立于终端会话)")
    ap.add_argument("--timeout", type=int, default=1800, help="单条 agent 任务超时秒")
    ap.add_argument("--model", default="", help="传给 pi 的 --model")
    ap.add_argument("--pi-bin", default="pi")
    ap.add_argument("--workdir", default=WORKDIR_DEFAULT,
                    help="agent 的工作目录 (默认 = 启动本桥时的 cwd)")
    ap.add_argument("--allow-run", action="store_true", help="放开 /run 执行 shell")
    ap.add_argument("--run-timeout", type=int, default=300)
    ap.add_argument("--watch-timeout", type=int, default=30,
                    help="看护项单次取数超时秒 (远程 ssh 取日志留余量)")
    ap.add_argument("--backoff-after", type=int, default=3,
                    help="连续失败多少次后开始退避 (之前保持原间隔)")
    ap.add_argument("--backoff-max", type=int, default=300,
                    help="退避等待上限秒 (带 0.8—1.2 抖动)")
    ap.add_argument("--inbox", default="",
                    help="入站文件落盘目录 (默认 <workdir>/.feishu_inbox/<日期>/)")
    ap.add_argument("--max-extract", type=int, default=200_000,
                    help="单个文件抽正文的字数上限 (默认 20 万, 超了截断并标注)")
    ap.add_argument("--state", default="/tmp/feishu_bridge_state.json")
    ap.add_argument("--dry", action="store_true", help="不真发、不真跑")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    Bridge(a).loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
