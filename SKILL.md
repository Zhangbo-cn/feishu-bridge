---
name: feishu-bridge
description: >
  飞书双向通道: 往飞书发 (文本/卡片/文件) + 从飞书收指令驱动 pi agent。
  发送侧 —— 当用户说"发飞书 / 飞书通知我 / 跑完提醒我 / 把结果推给我 / 把报表发我 /
  推送到飞书", 或需要把跑批结论、报告摘要、告警、长日志、xlsx/pdf 报表送达时用。
  接收侧 —— 用户想"在飞书里给 agent 派活 / 远程看机器状态 / 手机上盯跑批"时,
  起一个轮询桥: 群里发文字 → pi agent 在本机跑一个回合 → 结果回飞书;
  内置 /status /ps /tail /send /run 等即时指令。
  不依赖飞书事件订阅 (那个一个 app 只能挂一个消费者), 用 im/v1/messages 轮询读,
  所以能跟已有的其他机器人共存。多应用并存 + 首选缺权限时自动回退。
compatibility: Python 3.9+ (发送侧仅标准库; 接收侧需 requests)。需要飞书自建应用凭证。
---

# 飞书双向桥 (feishu-bridge)

两个脚本, 一个发一个收:

| 脚本 | 方向 | 依赖 |
|---|---|---|
| `scripts/feishu_send.py` | 发: 文本 / 卡片 / **文件** | 仅标准库 |
| `scripts/feishu_bridge.py` | 收: 轮询群消息 → 派给 pi agent | `requests` + 上面那个 |

## 先跑这一句

```bash
python3 scripts/feishu_send.py --status
```

它会打印: 走哪条通道、有哪些 app(掩码)、token 能不能换、**凭证搜索链**上哪些位置命中了。
配置有问题时先看这个, 别猜。

## 凭证

搜索链 (找到第一个就用, 不合并):

```
$FEISHU_HOME → 当前目录 → 向上的 git 仓库根 → ~/.feishu/ → ~ → skill 目录
```

个人全局用推荐放 `~/.feishu/`; 某个项目要发到不同的群, 就在项目根放一份覆盖。

| 文件 | 内容 |
|---|---|
| `.feishu_apps.json` | 多应用 (推荐)。见下方格式 |
| `.feishu_chat` | 一行: 默认收件人 `oc_群` / `ou_用户` / 邮箱 |
| `.feishu_owner` | 一行: 只认这个 `ou_` 的指令 (**接收侧的唯一防线**) |
| `.feishu_app` | 一行: 默认用哪个 app |
| `.lark_key` | 两行 app_id / app_secret (旧格式, 仍兼容) |
| `.feishu_webhook` / `.feishu_secret` | 群自定义机器人 (只能发文本, 发不了文件) |

```json
{"default": "pi",
 "apps": {"pi":       {"app_id": "cli_...", "app_secret": "...", "chat_id": "oc_..."},
          "openclaw": {"app_id": "cli_...", "app_secret": "...", "chat_id": "oc_..."}}}
```

**多应用不是冗余设计**: 首选 app 报 `99991672`(缺发消息权限) 时自动换另一个已配 app 发出去,
回执里注明「首选(x)无权限, 已回退」。给某个 app 申请权限的期间通知不会断。

权限 600 + 进 gitignore。脚本只打印掩码, 不会把 secret/token 写进日志。

飞书侧需要开的 scope: `im:message:send_as_bot` (发消息)、`im:resource` (发文件)、
`im:message` 读权限 (接收侧轮询)。开完要**创建版本并发布**, 再把机器人**拉进目标群**。

## 发送侧

```bash
S="python3 scripts/feishu_send.py"

$S --text "跑批完成: CR0.5547"                   # 纯文本
$S --title "判官A/B 结论" --stdin < /tmp/summary.txt  # 卡片 + 长正文
$S --to me@corp.com --text "私聊"                    # 收件人类型按长相自动判
$S --file 判官对比.xlsx                               # 纯发文件
$S --title 结论 --text 要点 --file 报表.xlsx          # 卡片 + 文件 (推荐)
$S --text "..." --dedup-key daily-report             # 同 key 600s 内只发一次
$S --file 报表.xlsx --dry                      # 先看走哪条通道
$S --selftest                                        # 内建假飞书, 21 个用例
```

正文 ≤18000 字自动截断并标注。文件 ≤30MB, 按扩展名定 `file_type`(认不出用 `stream`,
只影响能不能在线预览, 不影响能不能发)。

**表格类结论一律发 xlsx, 不要发 Markdown 表格** —— 飞书渲染不了 md 表格, 列一多就全糊。

退出码: 0 成功 / 4 无可用通道 / 5 没内容 / 6 webhook 是占位符 / 7 发送失败 / 8 自检失败 /
9 `--file` 指的文件不存在。

## 接收侧 (双向)

```bash
# 先在不带执行权限的前提下起 (推荐)
setsid nohup python3 -u scripts/feishu_bridge.py \
    > /tmp/feishu_bridge.log 2>&1 < /dev/null & disown

ps -ef | grep feishu_bridge          # 确认活着
tail -f /tmp/feishu_bridge.log
```

起在**哪个目录**就以哪个目录为 agent 的 cwd (`--workdir` 可改)。

群里可用:

| 指令 | 作用 |
|---|---|
| `/ping` | 连通性 |
| `/status` | 主机/负载/内存/GPU/关键进程/盘 |
| `/ps [关键字]` | 进程 |
| `/tail <文件> [行数]` | 日志尾部 (上限 300 行) |
| `/watch` | 看护中的任务 |
| `/watch add <名> <日志>` | 盯日志推进度 (事件即时, 进度限流, 空跑发心跳) |
| `/watch now <名>` | 立刻取一次当前状态, 不等 tick |
| `/watch rm <名>` | 取消看护 |
| `/send <文件>` | 把本机文件上传发回飞书 |
| `/run <命令>` | 执行 shell, **需启动时加 `--allow-run`** |
| `/help` `/stop` | 帮助 / 停桥 |
| 其他任何文字 | 派给 pi agent 跑一个回合, 结果回飞书 |

agent 用独立会话 (`--session-id feishu-bridge`), 不污染终端里的会话。
单条任务超时 1800s(`--timeout`), 超了中断并把已产出的内容发回来。
回执 >12000 字时落盘 `/tmp/feishu_bridge_out_*.txt` 并在消息里给路径。

### ⚠ 这是远程执行, 看清楚再开

桥收到消息后跑的是 `pi --session-id <id> -p --approve <消息>`。
`--approve` = **自动批准所有工具调用**, agent 会自主改文件、跑命令, 不再向你确认。
加 `--allow-run` 则等于把 shell 直接暴露到群里。

防护只有一层: `--owner` / `.feishu_owner` 白名单 (只认你的 `open_id`, 其他人和机器人全忽略)。
**没配 owner 就等于群里任何人都能驱动这台机器** —— 桥启动时会为此打警告。
飞书消息是明文信道, 谁能用你的账号就能用这台机器。

建议: 先不带 `--allow-run` 起; 真要放开时想清楚这台机器上有什么。

### 为什么用轮询而不是事件订阅

飞书的"收消息"官方走事件订阅, 但**一个 app 的事件订阅只能挂一个消费者 URL** ——
已经被别的机器人(如 OpenClaw)占着的话, 你 @ 机器人的消息推给了它, 这边收不到;
而且事件订阅需要一个外网可达的回调地址。

本桥改用 `GET im/v1/messages` 轮询群历史 (需 `im:message` 读权限), 于是:
不抢事件订阅、不需要公网地址、能跟现有机器人共存。代价是延迟 = 轮询间隔(默认 5s)。

轮询失败按退避走: 前 `--backoff-after`(默认 3) 次失败保持原间隔, 之后翻倍到
`--backoff-max`(默认 300s) 封顶, 每次加 0.8—1.2 的抖动。飞书的 DNS 抖动与 `2200`
通常几秒就过, 所以头几次不退避; 真的掉线时也不再用 5s 一条刷屏。

### 长任务: 让看护器推, 不要让 agent 睡

agent 一个回合最多 1800s。用 `sleep 600; 查一次` 盯训练, 一个回合只够盯两次,
中间什么都看不见, 回合被挱掉时连已跑的进度都拿不回来。

现在把「等」从 agent 回合里拿出来, 交给桥的看护器:

```bash
# 本地日志
python3 scripts/feishu_watch.py add --name b1 --log /abs/path/train.log --notify

# 远程机器的日志 (stdout 当日志内容)
python3 scripts/feishu_watch.py add --name b1 \
    --cmd "ssh -p 41216 host 'tail -c 300000 logs/train.log'" --check 120 --notify

python3 scripts/feishu_watch.py status --name b1   # 现在就查一次, 不推进
python3 scripts/feishu_watch.py ls
python3 scripts/feishu_watch.py rm --name b1
```

推送节奏 (中间状态也会发, 但不会刷屏):

| 变化 | 时机 |
|---|---|
| 新臂开始 / 全量开跑 / 结束 / 阶段收尾 / 新 eval_loss / 异常行 | **立即发** |
| 进度条 (步数、每步耗时、剩余时间) | 距上次消息超过 `--every`(默认 900s) 且步数有前进 |
| 什么都没变 | 距上次消息超过 `--idle`(默认 3600s) 发一条心跳 |
| 取数失败 | 前 2 次静默, 第 3 次告警并把取数间隔翻倍到 600s 封顶, 恢复后自动调回 |
| 阶段收尾 | 自动停止看护 |

三个间隔相互约束, 传进来会被拉回合理范围: `every ≥ 60`、`every ≤ idle ≤ 4×every`、
`check ≤ every`。心跳比进度密会被心跳刷屏, 稀太多就赶不上对端判失联的阈值。

取数间隔 `--check`(默认 60s) 与控制发消息间隔分开: 远程进程多的时候把它调到 120s 以上。
看护名单存在 `~/.feishu/watches.json`(可用 `FEISHU_WATCH_STATE` 改), 跟启动目录无关, 桥重启不丢。
桥没开的时候可以自己跑: `python3 scripts/feishu_watch.py run --interval 20`。

能认的日志特征 (已对着 `run_matrix_*.sh` / `train_qlora.py` 的输出调过):
`[arm …] 名称`、`[run …] 名称 全量开跑`、`[done …] 名称 退出码 N`、`[stage done …] 阶段`、
`[gate] 显存空闲 …MiB`、tqdm 进度条(`\r` 刷新的那种也算)、`'eval_loss': '…'`、
`Traceback` / `CUDA out of memory` / `No space left on device`。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `99991672 Access denied. scopes required` | 该 app 没开对应 scope。去 `https://open.feishu.cn/app/<app_id>/auth` 开通 → 启用**机器人** → **创建版本并发布** → 把机器人**加进群** |
| `230002 bot is not in the chat` | 权限发了但机器人还没被拉进群 |
| `19001 incoming webhook access token invalid` | webhook 是占位符, 不是真 URL |
| `19021 sign match fail` | 机器人开了签名校验, 需 `.feishu_secret` |
| 发文件报"必须走自建应用" | webhook 通道没有上传端点, 只能用 app + 收件人 |
| 桥没反应 | ① 进程在不在 ② 起的时候带了 `-u` 吗 (不带 print 会被缓冲, 日志看着像死了) ③ 发消息的人是不是 owner |
| 桥日志空但功能正常 | 缺 `-u`。改看 `/tmp/feishu_bridge_state.json` 的 `last_ts` |
| 电脑重启后桥没了 | 它是普通进程, 不会自启。要自启进 crontab `@reboot`, 改之前先 `crontab -l > /tmp/ct.bak` |

## 铁律

1. **先 `--dry` 再真发**, 长正文和文件尤其。
2. **后台起进程用 `setsid nohup … -u … < /dev/null > log 2>&1 &`**。少了 `< /dev/null`, SSH 客户端等不到 EOF 会假超时; 少了 `-u`, 日志被缓冲住看着像死了。
3. **飞书失败也返 HTTP 200** —— 必须解 body 的 `code`(脚本已处理)。别看着 200 就当发成功了。
4. **别打印 webhook / app_secret / token**, 诊断只报掩码。
5. **表格发 xlsx**, 别在飞书里发 Markdown 表格。
6. **接收侧默认不开 `/run`**; 开之前想清楚这台机器上有什么。
