# feishu-bridge

飞书双向桥, 给 pi coding agent 用:

- **收**: 群里发文字 → 本机 pi agent 跑一个回合 → 结果回群。用 `GET im/v1/messages` 轮询,
  不抢事件订阅, 不需要公网地址, 能跟已有机器人共存。
- **发**: 文本、卡片、文件 (`im/v1/files` 上传)。
- **看**: 长任务进度看护器。盯日志, 状态有变化就往群里推一条, 不占 agent 回合。

## 为什么要有看护器

桥给每个 agent 回合 1800 秒上限。用 `sleep 600; 查一次` 盯训练:

- 一个回合只够盯两次, 中间什么都看不见;
- 回合被掐断时, 已经跑出来的进度拿不回来, 连任务是否还在跑都要重新查。

看护器把「等」从回合里拿出来, 挂在桥的轮询循环里, 每 60 秒读一次日志尾部,
只在状态变化时发消息。推送规则:

| 变化 | 时机 |
|---|---|
| 新臂 / 全量开跑 / 结束 / 阶段收尾 / 新 eval_loss / 异常行 | 立即发 |
| 进度条 (步数、每步耗时、剩余时间) | 距上次消息超过 `--every`(默认 900s) 且步数有前进 |
| 什么都没变 | 距上次消息超过 `--idle`(默认 3600s) 发一条心跳 |
| 取数失败 | 前 2 次静默, 第 3 次告警并把取数间隔翻倍到 600s 封顶, 恢复后自动调回 |
| 阶段收尾 | 自动停止看护 |

三个间隔相互约束: `every ≥ 60`、`every ≤ idle ≤ 4×every`、`check ≤ every`。
桥的入站轮询失败也退避: 前 3 次保持原间隔, 之后翻倍到 300s 封顶, 带 0.8—1.2 抖动。

## 快速开始

```bash
# 1. 配凭证 (自建应用 app_id/app_secret, 目标群 chat_id, 白名单 owner open_id)
python3 scripts/feishu_send.py --status      # 打印走哪条通道、凭证搜索链哪命中

# 2. 发一条
python3 scripts/feishu_send.py --text "hello"

# 3. 起接收桥 (agent 的 cwd = 启动目录)
setsid nohup python3 -u scripts/feishu_bridge.py \
    --chat oc_xxx --owner ou_xxx > bridge.log 2>&1 < /dev/null &

# 4. 盯一个长任务的日志
python3 scripts/feishu_watch.py add --name train --log /abs/path/train.log --notify
python3 scripts/feishu_watch.py status --name train
python3 scripts/feishu_watch.py ls
python3 scripts/feishu_watch.py rm --name train
```

远程机器的日志走 `--cmd`, stdout 当作日志内容:

```bash
python3 scripts/feishu_watch.py add --name train --check 120 \
    --cmd "ssh -p 22 host 'tail -c 300000 logs/train.log'"
```

桥没开的时候看护器能自己跑: `python3 scripts/feishu_watch.py run --interval 20`。

## 群里可用指令

| 指令 | 作用 |
|---|---|
| `/ping` `/status` `/ps [关键字]` `/tail <文件> [行数]` | 连通性 / 机器状态 / 进程 / 日志尾部 |
| `/watch` `/watch add <名> <日志>` `/watch now <名>` `/watch rm <名>` | 看护名单 / 登记 / 立刻取一次状态 / 取消 |
| `/send <文件>` | 把本机文件发回群 |
| `/run <命令>` | 执行 shell, 需启动时加 `--allow-run` |
| `/help` `/stop` | 帮助 / 停桥 |
| 其他任何文字 | 交给 pi agent 跑一个回合 |

## 安全

桥收到消息后跑 `pi --session-id <id> -p --approve <消息>`。`--approve` = 自动批准所有工具调用。
防护只有一层: `--owner` / `.feishu_owner` 白名单 (只认这个 `open_id`, 其他人和机器人全忽略)。
**没配 owner 等于群里任何人都能驱动这台机器**。飞书消息是明文信道。

## 自检

```bash
python3 scripts/feishu_watch.py selftest    # 日志解析 / 推送节奏 / 熔断 / 名单读写, 37 项
python3 scripts/feishu_bridge.py --selftest  # 消息与附件解析 / 指令派发 / 退避, 28 项
python3 scripts/feishu_send.py --selftest    # 发送通道
```

## 文件

| 文件 | 作用 | 依赖 |
|---|---|---|
| `scripts/feishu_send.py` | 发送侧: 文本 / 卡片 / 文件 / 凭证链 | 仅标准库 |
| `scripts/feishu_bridge.py` | 接收侧: 轮询 → 派发 pi agent, 内置看护 tick | `requests` |
| `scripts/feishu_watch.py` | 进度看护器: 解析日志, 按变化推送 | `requests` (仅发送时) |
| `scripts/extract_doc.py` | 入站附件抽正文 (docx/xlsx/pptx/pdf) | 见文件头 |

## 已知限制

- 轮询延迟 = `--interval`(默认 5s)。
- 飞书偶发 `NameResolutionError` / `2200 internal error`, 每次一行日志, 5 秒后重试。
- 首选的 app 缺发送权限时自动回退到另一个 app, 消息里会标注 `[首选无权限, 已回退]`。
