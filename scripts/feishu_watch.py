#!/usr/bin/env python3
"""长任务进度看护: 盯日志, 状态有变化就往飞书推一条。

为什么由桥进程推进、不让 agent 自己 sleep 轮询:

桥给每个 agent 回合 1800 秒上限。sleep 600 两次就用满一个回合, 中途什么都看不见,
回合被掐断后连已跑的部分都拿不回来。看护器挂在桥的轮询循环里, 每个 tick 读一次
日志尾部, 只在**状态变化**时发消息, 完全不占 agent 回合。

    python3 feishu_watch.py add --name b1 --log /abs/path/log
    python3 feishu_watch.py add --name b1 --cmd ".scratch/x/r2.sh 'tail -c 200000 logs/b1.log'"
    python3 feishu_watch.py ls
    python3 feishu_watch.py status --name b1
    python3 feishu_watch.py rm --name b1
    python3 feishu_watch.py run --interval 20      # 桥没开时独立跑

两种取数方式:
    --log   本地文件, 只读尾部 256 KB
    --cmd   shell 命令, stdout 当日志内容 (远程机器的日志走这条)

三个时间参数:
    --check 60      取数间隔, 决定多快能发现变化
    --every 900     进度消息最小间隔, 防止一条一个进度条
    --idle 3600     长时间没变化时的心跳间隔
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from feishu_send import (  # noqa: E402
    FeishuError,
    deliver,
    load_apps,
    load_chat,
    read_app,
)

MAX_TAIL = 4 * 1024 * 1024
# 固定放在家目录下: 跟启动目录无关, 换一个 cwd 重启桥不会丢看护名单。
STATE_FILE = Path(os.environ.get("FEISHU_WATCH_STATE")
                  or Path.home() / ".feishu" / "watches.json")

# ─────────────────────────── 日志解析 ───────────────────────────

ARM_RE = re.compile(r"\[arm ([0-9\-: ]+)\] (\S+) =")
RUN_RE = re.compile(r"\[run\s+([0-9\-: ]+)\] (\S+) 全量开跑")
DONE_RE = re.compile(r"\[done ([0-9\-: ]+)\] (\S+) 退出码 (\d+)")
STAGE_RE = re.compile(r"\[stage done ([0-9\-: ]+)\] (\w+)")
# 自动接力脚本 (chain_*.sh) 的进度行, 整行原样转发
CHAIN_RE = re.compile(r"\[chain ([0-9\-: ]+)\] (.+)")
GATE_RE = re.compile(r"\[gate\] 显存空闲 (\d+)MiB")
EVAL_RE = re.compile(r"'eval_loss':\s*'([\d.]+)'[^}]{0,200}?'epoch':\s*'([\d.]+)'")
STEP_RE = re.compile(r"(\d+)/(\d+) \[([0-9]+:[0-9:]+)<([0-9:?]+),\s*([\d.]+)(s/it|it/s)\]")
SCHED_RE = re.compile(r"\[schedule\] steps_per_epoch=(\d+) total_steps=(\d+) warmup=(\d+)")
BAD_RE = re.compile(r"Traceback \(most recent call last\)|CUDA out of memory|"
                    r"torch\.OutOfMemoryError|RuntimeError:|No space left on device")


def read_tail(path: str, max_bytes: int = MAX_TAIL) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return ""
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", "replace")


def run_probe(cmd: str, timeout: int = 30) -> str:
    p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
    out = p.stdout or ""
    if p.returncode and not out:
        out = p.stderr or ""
    return out


def probe(w: dict, timeout: int = 30) -> str:
    if w.get("cmd"):
        return run_probe(w["cmd"], timeout)
    return read_tail(w.get("log") or "")


def parse(text: str) -> dict:
    """从日志尾部抽状态。tqdm 用 \\r 刷新, 所以两种换行都当分隔。"""
    body = (text or "").replace("\r", "\n")
    out: dict = {"arm": "", "arm_ts": "", "run": "", "run_ts": "",
                 "done": [], "stage_done": [], "evals": [], "gate": "",
                 "step": 0, "total": 0, "sit": 0.0, "elapsed": "", "bad": [], "chain": []}
    for m in ARM_RE.finditer(body):
        out["arm"], out["arm_ts"] = m.group(2), m.group(1).strip()
        out["arm_pos"] = m.end()
    for m in RUN_RE.finditer(body):
        out["run"], out["run_ts"] = m.group(2), m.group(1).strip()
    out["done"] = [(m.group(1).strip(), m.group(2), int(m.group(3)))
                   for m in DONE_RE.finditer(body)]
    out["stage_done"] = [(m.group(1).strip(), m.group(2))
                         for m in STAGE_RE.finditer(body)]
    out["chain"] = [(m.group(1).strip(), m.group(2).strip()) for m in CHAIN_RE.finditer(body)]
    out["evals"] = [(m.group(1), m.group(2)) for m in EVAL_RE.finditer(body)]
    # 当前臂的 eval: 只取最后一个 [arm] 标记之后的
    out["arm_evals"] = [(m.group(1), m.group(2))
                        for m in EVAL_RE.finditer(body, out.get("arm_pos", 0))]
    gates = GATE_RE.findall(body)
    if gates:
        out["gate"] = gates[-1]
    steps = STEP_RE.findall(body)
    picked = pick_bar(body, [m for m in STEP_RE.finditer(body)],
                      [int(m.group(2)) for m in SCHED_RE.finditer(body)])
    if picked is not None:
        cur, tot, el, _left, rate, unit = picked
        out["step"], out["total"], out["elapsed"] = int(cur), int(tot), el
        r = float(rate)
        out["sit"] = r if unit == "s/it" else (1.0 / r if r else 0.0)
    if not steps:
        out["step"] = 0
    for line in body.split("\n"):
        if BAD_RE.search(line):
            out["bad"].append(line.strip()[:200])
    return out


def pick_bar(body: str, bars: list, plan_totals: list[int]):
    """从多个 tqdm 进度条里挑「训练」那条。

    同一个日志里有两种条: 训练条 (total = 总步数) 和 eval 条 (total = 样本数,
    跑完紧跟一行 `{'eval_loss': …}`)。直接取最后一条会把 eval 当成训练进度。
    判据递进:
      1. `[schedule] total_steps=N` 出现过 → 取 total 等于 N 的最后一条 (准确)
      2. 否则取 total 最大的最后一条 (训练总步数通常大于 eval 样本数)
      3. 再否则取最后一条
    4 MB 的尾巴能装下整个臂的日志, 判据 1 几乎总能命中。
    """
    if not bars:
        return None
    if plan_totals:
        want = plan_totals[-1]
        hits = [m for m in bars if int(m.group(2)) == want]
        if hits:
            return tuple(hits[-1].group(i) for i in range(1, 7))
    big = max(int(m.group(2)) for m in bars)
    hits = [m for m in bars if int(m.group(2)) == big]
    return tuple(hits[-1].group(i) for i in range(1, 7))


def fmt_secs(sec: float) -> str:
    sec = max(int(sec), 0)
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return f"{h} h {m:02d} min" if h else f"{m} min"


def fmt_eval(evals: list[tuple[str, str]], keep: int = 3) -> str:
    """把 eval_loss 列表压成一行, 探针那条单独标出来。"""
    if not evals:
        return ""
    parts = []
    for val, epoch in evals[-keep:]:
        try:
            e = float(epoch)
        except ValueError:
            e = 0.0
        label = f"第 {int(round(e))} 轮" if abs(e - round(e)) < 0.02 and e >= 1 else "探针"
        parts.append(f"{val}（{label}）")
    return "eval_loss " + " → ".join(parts)


def status_text(name: str, p: dict) -> str:
    arm = p["arm"] or p["run"] or (p["done"][-1][1] if p["done"] else "")
    head = f"[{name}] {arm or '未知臂'}"
    # 日志里可能有好几个臂: 结束状态只看当前臂自己的 [done]
    mine = [d for d in p["done"] if d[1] == arm]
    if mine:
        ts, _, code = mine[-1]
        head += f" 已结束 退出码 {code} ({ts[11:16]})"
    else:
        head += " 运行中"
    lines = [head]
    if p["total"]:
        pct = int(p["step"] * 100 / p["total"])
        line = f"步 {p['step']}/{p['total']} ({pct}%)"
        if p["sit"]:
            line += f" · 每步 {p['sit']:.1f} s · 已跑 {p['elapsed']}"
            left = (p["total"] - p["step"]) * p["sit"]
            if left:
                line += f" · 剩约 {fmt_secs(left)}"
        lines.append(line)
    ev = fmt_eval(p.get("arm_evals", p["evals"]))
    if ev:
        lines.append(ev)
    if p["stage_done"]:
        lines.append(f"阶段 {p['stage_done'][-1][1]} 已收尾 ({p['stage_done'][-1][0][11:16]})")
    if p["gate"]:
        lines.append(f"显存门禁 {p['gate']}MiB")
    if p["bad"]:
        lines.append("异常行: " + p["bad"][-1])
    return "\n".join(lines)


# ─────────────────────────── 状态推进 ───────────────────────────

def sane_windows(every: int, idle: int, check: int) -> tuple[int, int, int]:
    """把三个间隔拉回相互合理的范围。

    约束来自两条失效: 心跳比进度还密 → 群里被心跳刷屏; 心跳比进度稀疏太多 →
    对端判失联时心跳还没到, 等于没有心跳。所以 idle 夹在 [every, 4*every]。
    """
    every = max(int(every), 60)
    check = int(check) if int(check) <= 0 else max(int(check), 15)
    idle = max(int(idle), every)
    idle = min(idle, every * 4)
    check = min(check, every)
    return every, idle, check


def default_watch(name: str, log: str, cmd: str, every: int, idle: int, check: int) -> dict:
    every, idle, check = sane_windows(every, idle, check)
    return {"name": name, "log": log, "cmd": cmd, "every": every, "idle": idle,
            "check": check, "created": time.time(), "checked": 0, "finished": False,
            "reported": {}}


def num(w: dict, key: str, dflt: float) -> float:
    """取数窗口参数。0 是合法值(每个 tick 都推进), 所以不能用 `or` 兜底。"""
    v = w.get(key)
    return float(dflt if v is None else v)


def note_fail(w: dict, reason: str) -> str:
    """取数失败: 头两次静默重试, 第 3 次告警并把取数间隔拉长 (熔断)。

    不熔断的后果: 远端挂了以后每个 tick 都空等 --watch-timeout 秒, 而且每次都
    发一条同样的报错。间隔拉到 600 秒后每个 tick 的开销可忽。
    """
    w["probe_fail"] = int(w.get("probe_fail") or 0) + 1
    n = w["probe_fail"]
    if n < 3:
        return ""
    base = num(w, "check", 60) or 15
    if not w.get("probe_alert"):
        w["check_base"] = base
        w["check"] = min(base * 2, 600)
        w["probe_alert"] = True
        return (f"[{w['name']}] 连续 {n} 次取数失败（{reason}）\n"
                f"取数间隔从 {base:.0f}s 拉到 {w['check']:.0f}s, 恢复后自动调回")
    w["check"] = min(num(w, "check", 600) * 2, 600)
    return ""


def note_recover(w: dict) -> str:
    """取数恢复: 调回原间隔, 发一条恢复告知。"""
    if not w.get("probe_alert"):
        w["probe_fail"] = 0
        return ""
    w["check"] = num(w, "check_base", 60)
    w.pop("check_base", None)
    w.pop("probe_alert", None)
    w["probe_fail"] = 0
    return f"[{w['name']}] 取数恢复正常, 间隔调回 {w['check']:.0f}s"


def advance(w: dict, now: float, timeout: int = 30) -> str:
    """推进一个看护项, 返回要发出去的文字 (没有变化就返回 "")。

    推送规则:
      - 新臂 / 开跑 / 结束 / 阶段收尾 / 新 eval / 新异常: 立即发, 不等间隔
      - 进度条: 距上次消息超过 every 且步数有前进才发
      - 长时间没变化: 距上次消息超过 idle 发一条心跳
      - 取数失败: 头两次静默, 第 3 次告警并拉长间隔, 恢复后自动调回
    事件即时、进度限流、无变化时给心跳, 这就是「中间状态也要给出变化」的实现。
    """
    rep = w.setdefault("reported", {})
    if now - float(w.get("checked") or 0) < num(w, "check", 60):
        return ""
    w["checked"] = now
    try:
        p = parse(probe(w, timeout))
    except subprocess.TimeoutExpired:
        return note_fail(w, f"取数超时 {timeout}s")
    except Exception as e:                                        # noqa: BLE001
        return note_fail(w, f"{type(e).__name__}: {e}")

    recovered = note_recover(w)
    if recovered:
        return recovered

    events: list[str] = []
    if p["arm"] and p["arm"] != rep.get("arm"):
        rep["arm"] = p["arm"]
        events.append(f"新臂 {p['arm']} 开始 ({p['arm_ts'][11:16]})")
    if p["run"] and p["run"] != rep.get("run"):
        rep["run"] = p["run"]
        events.append(f"{p['run']} 全量开跑 ({p['run_ts'][11:16]})")
    if len(p["done"]) > rep.get("done", 0):
        for ts, arm, code in p["done"][rep.get("done", 0):]:
            flag = "正常结束" if code == 0 else f"失败 退出码 {code}"
            events.append(f"{arm} {flag} ({ts[11:16]})")
        rep["done"] = len(p["done"])
    if len(p["stage_done"]) > rep.get("stage_done", 0):
        for ts, stage in p["stage_done"][rep.get("stage_done", 0):]:
            events.append(f"阶段 {stage} 全部收尾 ({ts[11:16]})")
        rep["stage_done"] = len(p["stage_done"])
        until = w.get("until") or ""
        if not until or any(stage == until for _, stage in p["stage_done"]):
            w["finished"] = True
    if len(p["chain"]) > rep.get("chain", 0):
        for ts, text in p["chain"][rep.get("chain", 0):]:
            events.append(f"[自动接力 {ts[11:16]}] {text}")
        rep["chain"] = len(p["chain"])
    if len(p["evals"]) > rep.get("evals", 0):
        rep["evals"] = len(p["evals"])
        val, epoch = p["evals"][-1]
        try:
            e = float(epoch)
        except ValueError:
            e = 0.0
        label = f"第 {int(round(e))} 轮" if abs(e - round(e)) < 0.02 and e >= 1 else "探针"
        events.append(f"eval_loss {val}（{label}）")
    if len(p["bad"]) > rep.get("bad", 0):
        rep["bad"] = len(p["bad"])
        events.append("异常: " + p["bad"][-1])

    last = float(rep.get("ts") or w.get("created") or now)

    def compose(head: str, body: str = "") -> str:
        return "\n".join(x for x in [recovered, head, body] if x)

    if events:
        rep["ts"] = now
        rep["step"] = p["step"]
        return compose(f"[{w['name']}] " + "\n".join(events), status_text(w["name"], p))

    if w.get("finished"):
        return recovered

    every = num(w, "every", 900)
    if p["step"] and p["step"] != rep.get("step") and now - last >= every:
        rep["ts"], rep["step"] = now, p["step"]
        return compose(f"[{w['name']}] 进度", status_text(w["name"], p))

    idle = num(w, "idle", 3600)
    if now - last >= idle:
        rep["ts"] = now
        state = "没有新进展" if not p["step"] else "步数没变"
        return compose(f"[{w['name']}] 仍在跑（{state}）", status_text(w["name"], p))
    return recovered


# ─────────────────────────── 看护名单 ───────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
            st.setdefault("watches", {})
            return st
        except Exception:                                         # noqa: BLE001
            pass
    return {"watches": {}}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def tick(st: dict, now: float | None = None, timeout: int = 30) -> list[str]:
    """推进全部看护项, 返回这一轮要发的消息。桥的循环和独立模式都用它。"""
    now = time.time() if now is None else now
    msgs = []
    for name, w in st.get("watches", {}).items():
        if w.get("finished"):
            continue
        m = advance(w, now, timeout)
        if m:
            msgs.append(m)
    return msgs


def list_text(st: dict) -> str:
    ws = st.get("watches") or {}
    if not ws:
        return "没有看护项。加一个:\nfeishu_watch.py add --name <名字> --log <日志>"
    lines = [f"看护项 {len(ws)} 个"]
    for name, w in ws.items():
        src = w.get("cmd") or w.get("log") or ""
        flag = "已收尾" if w.get("finished") else "在看"
        lines.append(f"- {name} [{flag}] 每 {w.get('every')}s 报进度, 每 {w.get('check')}s 取数\n  {src}")
    return "\n".join(lines)


# ─────────────────────────── 发飞书 ───────────────────────────

def notifier(chat: str = "", app_name: str = ""):
    """返回一个 send(text, title) 函数, 复用桥的凭证链与回退逻辑。"""
    apps = load_apps()
    if not apps:
        raise SystemExit("没有飞书应用凭证, 先按 feishu_send.py --status 配好")
    name, app = read_app(apps)
    rid = load_chat(chat, app) or chat
    if not rid:
        raise SystemExit("没有收件人, 给 --chat 或写 .feishu_chat")

    def send(text: str, title: str = "") -> None:
        desc, _ = deliver(text, rid=rid, app=app, app_name=app_name or name,
                          id_type="chat_id", title=title, fallback=True)
        print(f"[发] {desc} {len(text)} 字", flush=True)
    return send


# ─────────────────────────── 命令行 ───────────────────────────

def cmd_add(a: argparse.Namespace) -> int:
    if not a.log and not a.cmd:
        raise SystemExit("要么给 --log <本地日志>, 要么给 --cmd '<取数命令>'")
    st = load_state()
    w = default_watch(a.name, a.log, a.cmd, a.every, a.idle, a.check)
    if a.until:
        w["until"] = a.until
    old = st["watches"].get(a.name)
    if old:
        w["created"] = old.get("created", w["created"])
        w["reported"] = old.get("reported", {})
    st["watches"][a.name] = w
    save_state(st)
    print(f"[add] {a.name} → {a.cmd or a.log}")
    print(f"      取数 {w['check']}s · 进度最小间隔 {w['every']}s · 心跳 {w['idle']}s"
          + (f" · 直到阶段 {a.until} 收尾" if a.until else " · 任一阶段收尾即停"))
    if a.notify:
        notifier(a.chat)(f"[{a.name}] 开始看护\n{a.cmd or a.log}", "看护已开")
    return 0


def cmd_rm(a: argparse.Namespace) -> int:
    st = load_state()
    if a.name not in st["watches"]:
        print(f"[rm] 没有 {a.name}")
        return 1
    st["watches"].pop(a.name)
    save_state(st)
    print(f"[rm] {a.name}")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    st = load_state()
    names = [a.name] if a.name else list(st["watches"])
    if not names:
        print(list_text(st))
        return 0
    for n in names:
        w = st["watches"].get(n)
        if not w:
            print(f"[status] 没有 {n}")
            continue
        print(status_text(n, parse(probe(w))))
        ts = w["reported"].get("ts") or w["created"]
        print(f"  上次消息 {time.strftime('%m-%d %H:%M', time.localtime(ts))}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    st = load_state()
    if a.name:
        st["watches"] = {k: v for k, v in st["watches"].items() if k == a.name}
        if not st["watches"]:
            raise SystemExit(f"没有看护项 {a.name}")
    send = print if a.dry else notifier(a.chat, a.model_app)
    print(f"[run] 看护 {list(st['watches'])} 每 {a.interval}s 推进一次", flush=True)
    while True:
        msgs = tick(st, timeout=a.probe_timeout)
        if msgs:
            save_state(st)
            for m in msgs:
                if a.dry:
                    print("─" * 20 + "\n" + m, flush=True)
                else:
                    send(m, "进度")
        if a.once:
            return 0
        time.sleep(a.interval)


def cmd_ls(a: argparse.Namespace) -> int:
    print(list_text(load_state()))
    return 0


# ─────────────────────────── 自检 ───────────────────────────

FAKE_LOG = """\
[schedule] steps_per_epoch=530 total_steps=1590 warmup=47
[gate] 显存空闲 24126MiB >= 20000MiB，开跑
=================== [arm 2026-11-05 20:19:20] a2_ffn_full =====================
{'eval_loss': '2.162', 'eval_runtime': '191.6', 'epoch': '0.003775'}
[run  2026-11-05 20:23:08] a2_ffn_full 全量开跑
 10%|█         | 159/1590 [26:30<3:58:20, 10.00s/it]
 50%|█████     | 795/1590 [2:12:30<1:19:30, 10.00s/it]
100%|██████████| 1590/1590 [4:29:41<00:00, 10.18s/it]{'eval_loss': '1.558', 'eval_runtime': '192.6', 'epoch': '1'}
{'eval_loss': '1.443', 'eval_runtime': '191.5', 'epoch': '2'}
[done 2026-11-06 00:53:05] a2_ffn_full 退出码 0
[stage done 2026-11-06 00:53:05] a2
"""


def selftest() -> int:
    ok = True

    def chk(cond: bool, msg: str) -> None:
        nonlocal ok
        ok = ok and bool(cond)
        print(("  ✓ " if cond else "  ✗ ") + msg)

    print("selftest: 日志解析")
    p = parse(FAKE_LOG)
    chk(p["arm"] == "a2_ffn_full", "取到臂名")
    chk(p["run"] == "a2_ffn_full", "取到开跑事件")
    chk(p["step"] == 1590 and p["total"] == 1590, "取到进度 1590/1590")
    chk(len(p["evals"]) == 3, f"取到 3 条 eval_loss (实际 {len(p['evals'])})")
    chk(p["done"] == [("2026-11-06 00:53:05", "a2_ffn_full", 0)], "取到退出码 0")
    chk(p["stage_done"][-1][1] == "a2", "取到阶段收尾")
    chk(p["gate"] == "24126", "取到显存门禁数值")
    chk(parse("").get("step") == 0, "空日志不炸")
    chk(parse("Traceback (most recent call last):").get("bad"), "认出异常行")

    print("selftest: 训练条不拿 eval 条顶替")
    mix = ("[schedule] steps_per_epoch=530 total_steps=1590 warmup=47\n"
           " 40%|#| 636/1590 [1:44:00<2:36:00, 9.80s/it]\n"
           "100%|#| 471/471 [03:11<00:00, 2.46it/s]"
           "{'eval_loss': '1.5', 'eval_runtime': '191', 'epoch': '1'}\n")
    pm = parse(mix)
    chk(pm["step"] == 636 and pm["total"] == 1590, "有 schedule 时取 total 对得上的那条")
    pe = parse("100%|#| 471/471 [03:11<00:00, 2.46it/s]"
               "{'eval_loss': '2.16', 'eval_runtime': '191', 'epoch': '0.003'}\n")
    chk(pe["total"] == 471 and pe["step"] == 471, "没有 schedule 时按最大 total 取条")

    print("selftest: 时间与 eval 格式化")
    chk(fmt_secs(59 * 60) == "59 min", "小于一小时按分钟")
    chk(fmt_secs(3900) == "1 h 05 min", "大于一小时带小时")
    chk("第 2 轮" in fmt_eval([("1.443", "2")]), "整数 epoch 标轮次")
    chk("探针" in fmt_eval([("2.162", "0.003775")]), "小数 epoch 标探针")

    print("selftest: 推送节奏")
    box = {"text": ""}

    def fake_probe(*_a, **_k):
        return box["text"]

    real = globals()["probe"]
    globals()["probe"] = fake_probe
    try:
        t = 1_000_000.0
        w = default_watch("t", "", "", every=900, idle=3600, check=0)
        box["text"] = ""
        chk(advance(w, t) == "", "空日志不发消息")

        box["text"] = "[arm 2026-11-05 20:19:20] armA ===\n" \
                      " 10%|# | 100/1000 [00:10<01:30, 0.10s/it]\n"
        m1 = advance(w, t + 1)
        chk("新臂" in m1, "新臂立即发")
        box["text"] = ("[arm 2026-11-05 20:19:20] armA ===\n"
                       " 50%|# | 500/1000 [00:50<00:50, 0.10s/it]\n")
        chk(advance(w, t + 60) == "", "进度未到 every 不重复发")
        m2 = advance(w, t + 901)
        chk("进度" in m2 and "500/1000" in m2, "到间隔且步数前进才发进度")
        chk(advance(w, t + 1800) == "", "步数没变不再发")

        box["text"] = ("[arm 2026-11-05 20:19:20] armA ===\n"
                       " 50%|# | 500/1000 [00:50<00:50, 0.10s/it]\n"
                       "{'eval_loss': '1.52', 'eval_runtime': '1', 'epoch': '1'}\n")
        m3 = advance(w, t + 1850)
        chk("eval_loss 1.52" in m3 and "第 1 轮" in m3, "新 eval 立即发")
        box["text"] += "[done 2026-11-06 00:53:05] armA 退出码 3\n"
        m4 = advance(w, t + 1900)
        chk("退出码 3" in m4 and "失败" in m4, "失败退出码标失败")
        box["text"] += "[stage done 2026-11-06 00:53:05] b1\n"
        m5 = advance(w, t + 1950)
        chk("阶段 b1" in m5 and w["finished"], "阶段收尾后停止看护")
        chk(advance(w, t + 2000) == "", "收尾后不再发")

        w2 = default_watch("t2", "", "", every=900, idle=3600, check=0)
        box["text"] = "[arm 2026-11-05 20:19:20] armB ===\n"
        advance(w2, t + 1)
        m6 = advance(w2, t + 3700)
        chk("仍在跑" in m6, "长时间无变化发心跳")
    finally:
        globals()["probe"] = real

    print("selftest: 取数失败熔断与恢复")
    box = {"text": "", "boom": False}

    def flaky_probe(*_a, **_k):
        if box["boom"]:
            raise subprocess.TimeoutExpired("cmd", 30)
        return box["text"]

    real2 = globals()["probe"]
    globals()["probe"] = flaky_probe
    try:
        t = 2_000_000.0
        w3 = default_watch("t3", "", "", every=900, idle=3600, check=60)
        box["text"] = "[arm 2026-11-05 20:19:20] armC ===\n"
        advance(w3, t)
        box["boom"] = True
        chk(advance(w3, t + 65) == "", "第 1 次失败静默")
        chk(advance(w3, t + 130) == "", "第 2 次失败静默")
        m = advance(w3, t + 195)
        chk("连续 3 次取数失败" in m, "第 3 次失败告警")
        chk(w3["check"] == 120, f"告警时取数间隔翻倍 (实际 {w3['check']})")
        chk(advance(w3, t + 316) == "", "熔断后不再重复报错")
        box["boom"] = False
        m = advance(w3, t + 560)
        chk("恢复正常" in m and w3["check"] == 60, "恢复后调回原间隔")
    finally:
        globals()["probe"] = real2

    print("selftest: 心跳与进度窗口")
    e, i, c = sane_windows(900, 100, 60)
    chk(i == 900, "心跳不得比进度更密")
    e, i, c = sane_windows(900, 99999, 6000)
    chk(i == 3600, "心跳不得比进度稀疏 4 倍以上")
    e, i, c = sane_windows(10, 3600, 0)
    chk(e == 60 and c == 0, "every 下限 60s, check=0 表示每 tick")

    print("selftest: 多阶段接力")
    box = {"text": ""}
    real3 = globals()["probe"]
    globals()["probe"] = lambda *_a, **_k: box["text"]
    try:
        t = 3_000_000.0
        w4 = default_watch("t4", "", "", every=900, idle=3600, check=0)
        w4["until"] = "b4"
        box["text"] = "[arm 2026-11-10 23:00:00] b3_x ===\n[stage done 2026-11-11 03:15:00] b3\n"
        m = advance(w4, t)
        chk("阶段 b3" in m and not w4["finished"], "until=b4 时 b3 收尾不停")
        box["text"] += "[chain 2026-11-11 03:16:00] b3 最优 b2_r64 alpha=128，启动 b4\n"
        m = advance(w4, t + 10)
        chk("[自动接力 03:16]" in m and "启动 b4" in m, "接力行原样转发")
        chk(advance(w4, t + 20) == "", "同一接力行不重复转发")
        box["text"] += "[stage done 2026-11-11 07:05:00] b4\n"
        m = advance(w4, t + 30)
        chk("阶段 b4" in m and w4["finished"], "until 阶段收尾后停")
    finally:
        globals()["probe"] = real3

    print("selftest: 名单读写")
    tmp = Path(".feishu_watch_selftest.json").resolve()
    global STATE_FILE
    keep = STATE_FILE
    STATE_FILE = tmp
    try:
        st = load_state()
        chk(st == {"watches": {}}, "空文件给出空名单")
        st["watches"]["x"] = default_watch("x", "/tmp/a.log", "", 900, 3600, 60)
        save_state(st)
        chk("x" in load_state()["watches"], "写入后读回")
        chk("看护项 1 个" in list_text(load_state()), "名单渲染")
    finally:
        STATE_FILE = keep
        tmp.unlink(missing_ok=True)

    print("selftest:", "通过" if ok else "失败")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="长任务进度看护 (推送到飞书)")
    sub = ap.add_subparsers(dest="op", required=True)

    a1 = sub.add_parser("add", help="加一个看护项")
    a1.add_argument("--name", required=True)
    a1.add_argument("--log", default="", help="本地日志路径")
    a1.add_argument("--cmd", default="", help="取数命令, stdout 当日志 (远程日志用这条)")
    a1.add_argument("--every", type=int, default=900, help="进度消息最小间隔秒")
    a1.add_argument("--idle", type=int, default=3600, help="无变化时心跳间隔秒")
    a1.add_argument("--check", type=int, default=60, help="取数间隔秒")
    a1.add_argument("--notify", action="store_true", help="登记后立刻发一条确认")
    a1.add_argument("--until", default="",
                    help="直到这个阶段收尾才停 (多阶段接力时用, 例如 b4); 不给则任一阶段收尾即停")
    a1.add_argument("--chat", default="")
    a1.set_defaults(fn=cmd_add)

    a2 = sub.add_parser("rm", help="删掉看护项")
    a2.add_argument("--name", required=True)
    a2.set_defaults(fn=cmd_rm)

    a3 = sub.add_parser("ls", help="列看护项")
    a3.set_defaults(fn=cmd_ls)

    a4 = sub.add_parser("status", help="看当前状态 (不推进, 不发消息)")
    a4.add_argument("--name", default="")
    a4.add_argument("--probe", action="store_true")
    a4.set_defaults(fn=cmd_status)

    a5 = sub.add_parser("run", help="独立推进看护项并推送")
    a5.add_argument("--name", default="")
    a5.add_argument("--interval", type=float, default=20.0)
    a5.add_argument("--probe-timeout", type=int, default=30)
    a5.add_argument("--chat", default="")
    a5.add_argument("--model-app", default="")
    a5.add_argument("--dry", action="store_true", help="只打印不发送")
    a5.add_argument("--once", action="store_true", help="只推进一次")
    a5.set_defaults(fn=cmd_run)

    sub.add_parser("selftest", help="自检").set_defaults(fn=lambda _a: selftest())

    a = ap.parse_args()
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
