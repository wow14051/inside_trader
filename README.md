# Inside Trader Insider Bot

这是一个用 Python 实现的 SEC Form 4 内部人交易提醒工具。它会检查指定美股代码最近的 Form 4 披露，筛选达到金额阈值的大额买入或卖出，然后通过钉钉机器人或 Discord Webhook 发送提醒。

项目现在不再依赖 Java/Maven，本地和 GitHub Actions 都只需要 Python 3.11+。

## 核心功能

- 查询 SEC EDGAR Form 4 内部人交易披露。
- 默认检查最近 4 天，可通过参数或环境变量修改。
- 只保留 officer/director 相关披露。
- 只提醒真实买入 `P` 和卖出 `S`，跳过授予、行权等非买卖交易。
- 按交易金额阈值过滤，默认 `40000` 美元。
- 优先使用 SEC daily master index，找不到时自动 fallback 到 browse-edgar。
- 并行下载 SEC index 和 Form 4 文件，减少运行时间。
- 支持钉钉加签 Webhook。
- 继续兼容 Discord Webhook。
- 未提供股票代码时，可自动从项目目录或桌面导入 `.ebk`、`.txt`、`.csv` 股票列表文件。
- 消息按股票分组，同一只股票的词条放在一起；股票组按最大买入金额优先排序。

## 通知渠道

通知发送优先级如下：

1. 如果配置了 `DING_WEBHOOK_URL`，发送到钉钉。
2. 如果没有配置钉钉，但配置了 `DISCORD_WEBHOOK_URL`，发送到 Discord。
3. 如果两个 Webhook 都没有配置，提醒内容会打印到运行日志。

也就是说，旧版 Discord Webhook 支持仍然保留。只要设置 `DISCORD_WEBHOOK_URL`，并且没有设置 `DING_WEBHOOK_URL`，就会走 Discord。

钉钉机器人单次请求有 body 大小限制。提醒内容较多时，程序会在接近限制前自动分段发送，尽量按股票和交易块拆分，避免漏掉后面的提醒。每个钉钉分段开头都会先加两条分割线，再加醒目的 `# ⏰`、一级标题格式的当前时间和分割线；只有拆成多段时才显示类似 `(1/3)` 的段落序号，方便在连续消息里识别新一段内容。

钉钉加签机器人需要同时配置：

```text
DING_WEBHOOK_URL
DING_WEBHOOK_SIGN
```

Discord 只需要配置：

```text
DISCORD_WEBHOOK_URL
```

## GitHub Actions 配置

在你的 GitHub 仓库里打开：

```text
Settings -> Secrets and variables -> Actions
```

建议添加以下 Secrets：

```text
DING_WEBHOOK_URL       钉钉机器人 Webhook 地址
DING_WEBHOOK_SIGN      钉钉机器人加签 secret
DISCORD_WEBHOOK_URL    Discord Webhook 地址；未使用钉钉时生效
SEC_USER_AGENT         可选，SEC User-Agent
SEC_CONTACT_EMAIL      可选，SEC From header 邮箱
```

当前 workflow 默认使用仓库里的 `美股.ebk` 股票列表，并在北京时间每天 15:00 自动运行一次。GitHub Actions 的 cron 使用 UTC，所以 workflow 中对应写成 `0 7 * * *`。定时任务默认使用最小交易金额 `40000` 美元、回看 `4` 天。

当前 workflow 也支持手动运行。打开 GitHub Actions，选择 `Daily Insider Check`，点击 `Run workflow`，可以临时填写：

```text
tickers      可选，临时覆盖股票列表的股票代码；留空使用 美股.ebk
threshold    最小交易金额；留空默认 40000
lookback     回看天数；留空默认 4
```

手动输入会覆盖 workflow 里的默认值。每次运行会在 Actions 日志里打印本次实际使用的 `THRESHOLD_USD` 和 `LOOKBACK_DAYS`。

## 本地运行

本项目只使用 Python 标准库，不需要安装第三方依赖。

如果已经安装了本机 CLI，可以直接用 `sib`：

```powershell
sib
sib 3
sib 3 stocklist
```

首次安装或更新 CLI：

```powershell
python -m pip install -e .
```

`sib` 的短参数规则：

```text
第一个参数：回看天数，例如 3
第二个参数：股票列表文件名，扩展名可以写也可以不写
```

例如 `sib 3 stocklist` 会先在项目目录查找名为 `stocklist` 的股票列表文件，扩展名可以省略；项目目录找不到，再去桌面查找。还是找不到的话，就回到原来的股票代码解析逻辑。

直接传入股票代码：

```bash
python stock_insider_bot.py "AAPL,GOOGL,MSFT" --threshold=40000 --lookback=4
```

也可以使用显式参数：

```bash
python stock_insider_bot.py --tickers=AAPL,GOOGL,MSFT --threshold=40000 --lookback=4
```

指定某个股票列表文件：

```bash
python stock_insider_bot.py --stock-list=stocklist --lookback=4
python stock_insider_bot.py --stock-list=美股 --lookback=4
```

PowerShell 环境变量示例：

```powershell
$env:TICKERS="AAPL,GOOGL,MSFT"
$env:THRESHOLD_USD="40000"
$env:LOOKBACK_DAYS="4"
$env:DING_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=..."
$env:DING_WEBHOOK_SIGN="SEC..."
python stock_insider_bot.py
```

如果要本地测试 Discord：

```powershell
$env:DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
Remove-Item Env:DING_WEBHOOK_URL -ErrorAction SilentlyContinue
python stock_insider_bot.py --tickers=AAPL,MSFT
```

注意：如果本机已经设置了 `TICKERS` 环境变量，它会优先于命令行位置参数。想临时用位置参数时，可以先清掉：

```powershell
Remove-Item Env:TICKERS -ErrorAction SilentlyContinue
python stock_insider_bot.py "AAPL,MSFT"
```

## 参数优先级

股票代码来源优先级：

```text
--tickers 参数 -> --stock-list 指定文件 -> TICKERS 环境变量 -> 命令行第一个位置参数 -> 自动导入股票列表文件
```

金额阈值来源优先级：

```text
--threshold 参数 -> THRESHOLD_USD 环境变量 -> 默认 40000
```

回看天数来源优先级：

```text
--lookback 参数 -> LOOKBACK_DAYS 环境变量 -> 默认 4
```

调试日志来源优先级：

```text
--debug 参数 -> DEBUG 环境变量 -> 默认 true
```

## 自动导入股票列表

如果没有提供 `--tickers`、`TICKERS` 或命令行位置参数，程序会自动寻找股票列表文件。

搜索顺序：

1. 先搜索项目目录，也就是 `stock_insider_bot.py` 所在目录。
2. 如果项目目录没有找到合格文件，再搜索当前用户桌面。
3. 如果找到多个合格文件，会全部导入，并自动去重。

支持的文件后缀：

```text
.ebk
.txt
.csv
```

支持的内容示例：

```text
AAPL
MSFT
NASDAQ:NVDA
BRK-B
```

```csv
company,ticker
Apple,AAPL
Nvidia,NVDA
Zoetis,ZTS
```

EBK 示例：

```text
31#CRSP
31#BNTX
31#TEM
31#RKLB
```

程序会做宽松判断：如果文件看起来像股票列表，就导入；普通笔记、README、日志这类文本会尽量跳过。

导入后的股票代码会统一转成大写、去重，并按字母顺序排序。调试日志里的 `DEBUG: Tickers:` 会显示排序后的列表，方便检查是否遗漏。

## 消息排序和展示

提醒消息会按股票分组，不再把同一只股票拆散。

股票组排序规则：

1. 有买入的股票排在前面。
2. 按该股票的最大买入金额从大到小排序。
3. 没有买入的股票，再按最大交易金额从大到小排序。
4. 金额相同时按股票代码排序。

同一只股票内部排序规则：

1. `BUY` 在前，`SELL` 在后。
2. 同类交易按金额从大到小排序。

买入和卖出会使用彩色菱形标记区分：`🔸 BUY` 表示买入，`🔹 SELL` 表示卖出。每条提醒使用两行短格式；明细会省略交易人姓名，职位会放在标题末尾。`CEO`、`CFO`、`COO`、`EVP`、`SVP` 等明确职位保留英文简称；`Group President` 显示为 `GP`，`Co-Chairman` 显示为 `COCH`，`Chairman` 显示为 `CHAIR`，`President` 显示为 `PRES`；`OFF` 显示为 `高管`，`DIR` 显示为 `董事`，没有职位时显示 `N/A`。标题里的交易金额最多保留 3 个有效数字，例如 `$597.6K` 会显示为 `$598K`；日期行会显示本次交易数量占交易前持仓的比例和成交价，价格最多保留 4 个有效数字。买入比例为正，卖出比例为负；如果是新建仓，没有可用比例时显示 `NEW`。遇到 `See Remarks` 时，会优先根据 SEC 的 `isOfficer` / `isDirector` 字段推断为 `高管` 或 `董事`，推断不了才显示 `REM`。日期行开头使用全角空格保留缩进，避免钉钉把普通行首空格吞掉。Discord 和钉钉都支持 Markdown 的一部分，但它们的 Webhook Markdown 都不支持任意字体颜色，所以不能像 HTML 一样指定蓝色或红色字体。

岗位显示规则：

| SEC 原始职位内容 | 提醒中显示 |
| --- | --- |
| `Chief Executive Officer` / `CEO` / `President and CEO` | `CEO` |
| `Chief Financial Officer` / `CFO` | `CFO` |
| `Chief Operating Officer` / `COO` | `COO` |
| `Chief Technology Officer` / `CTO` | `CTO` |
| `Executive Vice President` / `EVP` | `EVP` |
| `Senior Vice President` / `SVP` | `SVP` |
| `Vice President` / `VP` | `VP` |
| `Group President` | `GP` |
| `Co-Chairman` / `Co-Chairwoman` / `Co-Chair` | `COCH` |
| `Chairman` / `Chairwoman` / `Chair` | `CHAIR` |
| `President` | `PRES` |
| `Director` | `董事` |
| `Officer` | `高管` |
| `See Remarks` | 优先根据 `isOfficer` / `isDirector` 推断为 `高管` 或 `董事`，否则显示 `REM` |
| 空职位 / `Unknown Position` | `N/A` |

示例：

```text
🔸 TMUS · BUY · $1M · CEO
　  2026-05-01   +13%@ $196.2
```

## 性能参数

默认已经开启并行下载。一般不用调整；如果 SEC 访问不稳定，可以适当降低 worker 数。

```text
INDEX_WORKERS    SEC master-index 并行下载数，默认 4
FORM4_WORKERS    Form 4 文件并行下载数，默认 8
```

PowerShell 示例：

```powershell
$env:INDEX_WORKERS="2"
$env:FORM4_WORKERS="4"
python stock_insider_bot.py --tickers=AAPL,MSFT
```

## 测试

运行单元测试：

```bash
python -m unittest discover -v
```

运行语法检查：

```bash
python -m py_compile stock_insider_bot.py tests/test_stock_insider_bot.py
```

## 文件说明

```text
stock_insider_bot.py                 Python 主程序
美股.ebk                             默认股票列表，GitHub Actions 使用
tests/test_stock_insider_bot.py      单元测试
.github/workflows/daily-check.yml    GitHub Actions 定时任务
```

## 安全说明

- 不要把 Webhook URL、钉钉 sign、邮箱等敏感配置写死到代码里。
- GitHub 上使用 Secrets 保存敏感值。
- 本地可以用环境变量调试。
- `.env` 和 `.env.*` 已加入 `.gitignore`，不会被 Git 默认提交。

## 数据来源

数据来自 SEC EDGAR。SEC 对自动化访问有 User-Agent 和访问频率要求；建议配置 `SEC_USER_AGENT` 和 `SEC_CONTACT_EMAIL`，并避免把并发数调得过高。
