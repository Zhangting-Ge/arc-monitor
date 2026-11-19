# 始祖鸟德国站监控（正价站 + 奥莱）

定时抓取始祖鸟德国正价站和奥莱的商品与库存数据，发现上新、降价、补货后通过 Bark 推送到 iPhone。用 GitHub Actions 免费运行。

## 能做什么

| 功能 | 说明 |
|---|---|
| 两个站点 | 正价站 `arcteryx.com/de`、奥莱 `outlet.arcteryx.com/de`，全站扫描（男、女、配件、背包、鞋） |
| 全站动态 | 上新 / 老款新配色 / 降价 / 售罄后重新上架，每个站点合并成一条推送 |
| 关注清单 | 按关键词或商品链接关注，精确到**尺码和颜色**。补货、新上架商品符合条件、降价时单独推送，级别为 timeSensitive，**在专注模式下也能收到** |
| 每晚日报 | 汇总当天各站变化、折扣最大的几款，以及你关注的商品现在有没有货 |
| 不刷屏 | 首次运行只记录当前状态（基线），之后有变化才推送；抓取失败要**连续 3 次**才提醒一次，恢复后再提醒一次 |
| 防误报 | 商品连续两次不在列表才算下架；颜色只有从没出现过才算新配色；降价只推近 60 天新低；商品数量突然剧变先忽略两次；详情页偶发 404 保持原状态 |
| 多人 | 自己和朋友各用各的 Bark，各设各的关注清单 |

推送样例：

```
🔔 补货了 · 奥莱
Gamma MX Hoody Men's
M（Black）有货了（库存紧张）
€280（原价€400，-30%） · 关注：Gamma MX 黑色 M
```

```
奥莱：上新 2 · 降价 3
🆕 Beta LT Jacket Men's €320（原价€400，-20%）
🆕 Atom Vest Men's €140（原价€200，-30%）
📉 Squamish Hoody Men's €140→€120（原价€200，-40%）
...
```

点推送会直接打开商品页面。

## 部署步骤

1. **装 Bark**：在 App Store 装 [Bark](https://apps.apple.com/app/bark-customed-notifications/id1403753865)，打开后复制首页的推送链接（形如 `https://api.day.app/xxxxxxxx/...`）。
2. **建 GitHub 仓库**：建议设为 **Public**。公开仓库的 Actions 免费且不限分钟数。私有仓库每月只有 2000 免费分钟，每 10 分钟跑一次约需 4300 分钟/月，会超额。如果要用私有仓库，把 `.github/workflows/monitor.yml` 里的 cron 改成 `3-59/30 * * * *`（每 30 分钟）。
3. **上传文件**：把本项目全部文件传到仓库，包括 `.github/workflows/monitor.yml`。
4. **添加 Bark 密钥**：仓库 Settings → Secrets and variables → Actions → New repository secret，名称填 `BARK_ME`，值填第 1 步复制的链接（只填 key 也行）。朋友的用 `BARK_FRIEND_A` 等名称，与 `config.yaml` 里的 `bark:` 一一对应。
5. **允许写回状态**：Settings → Actions → General → Workflow permissions，选 **Read and write permissions** 并保存。
6. **连接测试**：打开 Actions 页 → 左侧选「始祖鸟监控」→ Run workflow，mode 选 `test`。手机收到「🧪 连接测试」并且两个站都显示 ✅，说明能正常抓取。
7. **建立基线**：再手动运行一次，mode 选 `run`。手机会收到「始祖鸟监控已启动」和「📋 关注清单已生效」。
8. 之后会自动运行：**每 10 分钟**检查一次，**每晚 20:07**（柏林冬令时，夏令时为 21:07）发日报。

> 不想公开关注清单：把整个 `config.yaml` 的内容放进名为 `MONITOR_CONFIG` 的 Secret，程序会优先读取它，仓库里的 `config.yaml` 可以删掉。注意 `state/watch.json` 里仍会出现订阅人名字和规则名，公开仓库里用昵称即可。Bark key 只在 Secrets 里，不会公开。

## 改配置

所有设置都在 `config.yaml` 里，改完提交即可，下次运行生效。

**feed（全站动态，不指定款式）**

| 字段 | 含义 |
|---|---|
| `new_products` | 推送哪些站的上新，如 `[outlet, main]` |
| `new_colours` | 老款新配色 |
| `price_drops` | 降价（正价站开始打折 / 奥莱继续降价） |
| `back_in_catalog` | 售罄下架后重新上架 |
| `gender` | `mens` / `womens`，不分性别的配件、背包照常推送 |
| `max_price` / `min_discount` | 只推价格不高于 X 欧 / 折扣至少 X% 的商品 |

**watch（关注清单，精确到尺码）**

| 字段 | 含义 |
|---|---|
| `name` | 规则名，会显示在推送里 |
| `site` | `outlet` / `main`，不写表示两个站都看 |
| `url` | 直接贴商品链接，只看这一款 |
| `keywords` | 名称里必须**同时**包含的词，如 `[beta, jacket]` |
| `exclude` | 名称里不能出现的词，如 `[pant]` |
| `gender` | `mens` / `womens` |
| `sizes` | 尺码，写法与官网一致：`M`、鞋码 `42 2/3`、裤子 `32` 或 `32-R` |
| `colours` | 颜色，写官网显示的颜色名；双色款写其中一个也行：`Black` 匹配 `Black`、`Black / Mongoose`，不匹配 `Black Sapphire` |
| `max_price` | 折后价上限（欧元） |
| `min_discount` | 最低折扣，`30` 表示至少便宜 30% |

规则改动后会被当作新规则：重新发一次「关注清单已生效」，不会把已有商品当成补货推送。

**加朋友**：`config.yaml` 末尾有一段注释掉的「朋友A」模板，去掉每行开头的 `# ` 并改 `name`、尺码等，再到 GitHub 添加同名 Secret（如 `BARK_FRIEND_A`）。更多朋友就再复制一段。`daily_digest: false` 可以关掉日报，`admin: true` 的人会收到抓取失败等系统消息。

## 关于"实时"

这是**准实时**，不是秒级：

- GitHub 定时任务最短间隔 5 分钟，整点前后经常延迟十几分钟。
- 网站本身也有缓存：列表接口缓存 15 分钟（max-age 900），详情页的 CDN 缓存最长约 1 小时。

适合关注奥莱上新、等打折、等自己的尺码补货。热门款几秒钟就卖空的抢购场景，这个工具不够快。

## 被拦截怎么办

始祖鸟网站用了 Fastly、Signal Sciences WAF 和 Kasada 反爬。已在浏览器里验证过：不带 cookie 的普通请求可以拿到数据（程序还会用 curl_cffi 模拟 Chrome 的网络特征），但 GitHub 的机房 IP 仍有可能被拦。

**症状**：收到「⚠️ 正价站已连续 3 次抓取失败：HTTP 403……」，或者连接测试显示 ❌。

**对策**（按顺序尝试）：

1. 降低频率：cron 改成每 20～30 分钟。
2. 把 `max_detail_pages` 调小（比如 15），减少每次的请求数。
3. 改到自己电脑或 VPS 上运行（家庭宽带 IP 一般不会被拦）：

```bash
cd arc-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
BARK_ME="https://api.day.app/你的key" .venv/bin/python -m arcmon test
BARK_ME="https://api.day.app/你的key" .venv/bin/python -m arcmon run
```

macOS / Linux 用 crontab 每 10 分钟运行（`crontab -e` 加一行，路径换成你的）：

```
*/10 * * * * cd /Users/你/arc-monitor && BARK_ME="https://api.day.app/你的key" .venv/bin/python -m arcmon run >> monitor.log 2>&1
7 20 * * * cd /Users/你/arc-monitor && BARK_ME="https://api.day.app/你的key" .venv/bin/python -m arcmon digest >> monitor.log 2>&1
```

注意：电脑合盖睡眠时 cron 不会运行。需要 24 小时在线就用 VPS。

## 本地测试

```bash
python -m arcmon test --dry-run     # 测试能否抓到数据，只打印不推送
python -m arcmon run --dry-run      # 完整跑一次，打印推送内容（会写 state/）
python tests/test_monitor.py        # 离线模拟：用真实抓到的接口数据模拟上新、降价、补货、被拦截
```

## 注意事项

- **60 天无活动**：公开仓库 60 天没有任何活动时，GitHub 会停用定时任务。本项目在状态变化时会自动提交，通常不会触发；如果被停用，到 Actions 页点 Enable workflow 重新启用即可。
- **网站改版**：接口结构变了程序就会失效，这时会收到抓取失败提醒，需要更新代码。
- **克制使用**：每次运行约 14 个列表请求，再加最多 `max_detail_pages` 个详情页请求，请求之间有随机停顿。请勿把频率调得很高。仅供个人使用，请遵守网站条款。

## 文件结构

| 路径 | 作用 |
|---|---|
| `config.yaml` | 站点、订阅人、全站动态、关注清单 |
| `arcmon/fetch.py` | 抓取：列表接口 `/api/catalog.getProductListingPage`、详情 `/_next/data/.../shop/<slug>.json` |
| `arcmon/monitor.py` | 对比快照、处理关注规则、日报、连接测试 |
| `arcmon/notify.py` | 推送文案、Bark 发送 |
| `arcmon/config.py` | 读取配置、匹配规则 |
| `arcmon/state.py` | 读写 `state/` 快照与事件日志 |
| `state/` | 运行时自动生成：商品快照、关注状态、近 8 天事件（供日报使用） |
| `.github/workflows/monitor.yml` | 定时任务：每 10 分钟运行 + 每晚日报 + 手动 test/run/digest |
| `tests/` | 离线测试与真实数据样本 |
