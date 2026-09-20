# run_batch 使用手册

**把协议 PDF 批量转成 SAP，一次一个 PDF，每个 PDF 一个 Codex session。**

核心性质：**额度不够时它会在干净的边界停下**，不会停在改到一半的状态。停下来时会发邮件，
进度记在一个永不清空的账本里，下一轮接着跑就行。

完整参数见 `./run_batch.sh --help`，这里只写日常真正会用到的。

---

## 0. 第一次运行：初始化项目账号

`run_batch.sh` 使用项目独立的 `.codex-home/`，所以第一次跑批前要先登录：

```bash
tools/init_codex_home.sh
./run_batch.sh --limit 2
```

初始化只需做一次。之后直接运行 `run_batch.sh` 即可。如果要切换 Codex 账号，重新登录并
明确覆盖原来的项目凭据：

```bash
tools/init_codex_home.sh --login --force
```

如果不想重新登录，也可以复制当前文件形式保存的全局凭据
`~/.codex/auth.json`：

```bash
tools/init_codex_home.sh --copy-current
```

如果项目凭据已经存在，复制时同样需要加 `--force`。`.codex-home/` 已被 Git 忽略；其中
的 `auth.json` 含有访问令牌，不能提交、分享或粘贴到日志和工单中。

---

## 1. 跑一批

```bash
./run_batch.sh --limit 10 --dry-run              # 只看会处理哪些，不跑
./run_batch.sh --limit 2                         # 冒烟测试
./run_batch.sh --limit 200 --session-budget 4h   # 正式跑
```

两点就够：

- **`--session-budget` 是最该给的参数。** 给了它，脚本只在「剩余时间够跑完一个 job」时
  才开新 job（按历史 p90 耗时估），所以不会开一个注定跑不完的 job。
- **重复跑同一条命令是安全的。** 已经生成的 `sap_<id>.md` 会被自动跳过。

---

## 2. 还剩多少额度

```bash
tools/budget.sh --probe
```

**不花 token**，输出：

```
primary   0%  resets Wed Sep 16 05:06:26 PDT 2026
secondary 0%  resets Tue Sep 22 13:50:45 PDT 2026
plan      plus
```

`primary` 是 5 小时窗口，`secondary` 是 7 天窗口。**哪个先到阈值都会触发 drain。**

---

## 3. 被 drain 之后怎么继续

停下来时终端和邮件都会写 `drained: <原因>`，退出码 **3**。

```bash
tools/budget.sh --probe          # 先确认额度回来了
./run_batch.sh --limit 200 --session-budget 4h --resume --ignore-stop
```

两个 flag 都不能省：

- **`--ignore-stop` 是必须的。** 上一轮留下的 `test_result/.stop` 是故意的刹车。
  不带这个 flag 直接重跑会**退出码 4 并拒绝启动**——这是设计，不是 bug。
- **`--resume`** 把已知需要 OCR 的 PDF（`UNSUPPORTED_SCAN`）从待办里剔掉，
  否则它们每一轮都会被重新提取一次。

---

## 4. 看进度 / 查出了什么事

```bash
tools/budget.sh --report        # 累计统计
cat test_result/_ledger.tsv     # 逐行明细，永不清空，跨 session 累积
```

**状态词**（`_ledger.tsv` 第 4 列）

| 状态 | 含义 |
|---|---|
| `OK` | 做完了，`sap_<id>.md` 已生成 |
| `SKIP` | 输出已存在，跳过 |
| `UNSUPPORTED_SCAN` | 扫描件，需要 OCR，本脚本处理不了 |
| `FAIL` | 真失败（agent 或 lint 没过），重跑会重试 |
| `LIMIT_HIT` | 撞到额度墙 —— **不是失败**，不消耗重试次数 |
| `DEFERRED` | 预算不够，压根没开始 |
| `INTERRUPTED` | 被 Ctrl-C / kill 打断 |

**退出码**

| 码 | 含义 | 该做什么 |
|---|---|---|
| `0` | 全部完成 | 无 |
| `1` | 有 job 真的失败了 | 看 `test_result/logs/<id>.log`，重跑会自动重试 |
| `3` | 预算用尽，还有剩余工作 | 等额度回来，然后走第 3 节 |
| `4` | 上一轮的 `.stop` 没清 | 确认额度后加 `--ignore-stop` |

某个 job 被额度打断时会留下 `work/<id>.progress.md`，记录当时磁盘上有什么。
**不需要读它才能继续**——重跑就行，它只是给人看的。

---

## 5. 邮件通知

**已经配好了**，这节只在换机器或换邮箱时才需要。

```bash
# 仓库根目录 .env（已被 .gitignore 忽略），或 ~/.run_batch_env
export RB_MAIL_TO="you@gmail.com"
export RB_MAIL_FROM="you@gmail.com"
export RB_SMTP_URL="smtps://smtp.gmail.com:465"
export RB_SMTP_USER="you@gmail.com"
export RB_SMTP_TOKEN="16 位应用专用密码"
```

- App Password 在 <https://myaccount.google.com/apppasswords> 生成，**需要先开两步验证**。
  不是 Google 账号登录密码。
- **变量名用 `RB_SMTP_TOKEN`，别用 `RB_SMTP_PASS`。** 写成 `PASS` 也能跑（有向后兼容），
  但 Codex 默认会从 agent 的环境里剥掉 `*TOKEN*`，**不剥** `*PASS*`——换个名字白捡一层防护。
- 验证：`./run_batch.sh --notify-test` —— 不跑批，只发一封假的摘要邮件。
- **建议用一个专门的发件小号。** `.env` 在 Codex 的 workspace 里，agent 读得到；
  而 Gmail 应用专用密码绕过两步验证，给的是整个邮箱的读取权限。

---

## 附录：文件都在哪

默认都在 `test_result/` 下：

| 路径 | 是什么 |
|---|---|
| `sap_<id>.md` | **最终产物** |
| `_ledger.tsv` | 累积账本，永不清空，跨 session 的唯一真相 |
| `_status.tsv` | 本轮视图，每轮从账本重新生成 |
| `_budget.tsv` | 额度观测记录 |
| `.stop` | 刹车文件，存在就表示上一轮是被 drain 停的 |
| `logs/<id>.log` | 人读的运行日志，排查 `FAIL` 看这个 |
| `work/<id>.progress.md` | 中断时的现场快照 |
