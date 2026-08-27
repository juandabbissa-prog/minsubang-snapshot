# 满房帮 M-1 数据采集器（木鸟首发平台）README

**版本 v0.1（2026-08-07）· 依据：K3任务单_M-1 + M1-变更-001（首发平台途家→木鸟）**

## 一、这是什么

满房帮数据底座。从木鸟民宿公开页面采集大连民宿数据：

- **发现层**（`run_discover.py`）：大连列表翻页，发现新房源、更新房源主档。每周 2 次
- **追踪层**（`run_track.py`）：对已入池房源逐一抓详情页，落每日快照（价格/评分/点评数/坐标）。每日 1 次，22:00 前完成

## 二、快速开始

```bash
pip install -r requirements.txt   # 仅 requests；传输层用系统 curl
cp .env.example .env              # 填入飞书 Webhook 与 SMTP 配置（密钥只进 .env，绝不进代码）
python tests/test_parse.py        # 离线单测（用留档真实样本，不触网）
python run_discover.py            # 全量发现（约 10 页 / 6 分钟）
python run_track.py               # 全量追踪（房源数 × 3.5 秒）
# 冒烟模式：python run_discover.py --max-pages 1 && python run_track.py --limit 5
```

## 三、定时调度方案（系统 cron / Windows 任务计划）

| 任务 | 时间 | 命令 |
|---|---|---|
| 追踪层 | 每日 20:00 | `python run_track.py` |
| 发现层 | 每周三、周日 03:00 | `python run_discover.py` |
| 心跳 | 随任一运行自动执行（每日 ≤1 次） | — |

Linux cron 示例：
```
0 20 * * * cd /opt/manfangbang/crawler && /usr/bin/python3 run_track.py >> logs/cron.log 2>&1
0 3 * * 0,3 cd /opt/manfangbang/crawler && /usr/bin/python3 run_discover.py >> logs/cron.log 2>&1
```

失败重试机制：单请求失败自动重试 1 次（间隔 10 秒）；重定向（到底/登录墙/验证码）不重试、立即计数并按告警场景上报。整轮失败由 crawl_log 记录，次日 cron 自然重跑。

## 四、环境变量清单（.env）

| 变量 | 用途 | 缺省行为 |
|---|---|---|
| `FEISHU_WEBHOOK_URL` | 飞书告警 + 每日心跳 | 跳过飞书通道，仅邮件 |
| `SMTP_HOST/PORT/USER/PASS` | 邮件告警 | 跳过邮件通道，仅飞书 |
| `ALERT_EMAIL_TO` | 收件人（项目方部署时配置） | — |
| `REQUEST_INTERVAL` | 请求间隔秒数 | 3.5 |
| `REQUEST_TIMEOUT` | 单请求超时秒数 | 25 |

## 五、告警（任务单第七节四场景）

| 场景 | 触发 | 实现位置 |
|---|---|---|
| HTTP 非 200 | 追踪层首次非 200 即报 | `track.py` |
| 数据量骤降 | 今日快照 < 7 日均值 ×70% | `track.py` 末尾 |
| 采集超时 | 追踪层 >3h | `track.py` 循环内 deadline |
| 单日失败率 | >5% | `track.py` 末尾 |

心跳：每日首次运行自动发飞书心跳；心跳失败自动转邮件告警（`alerts.py`）。

## 六、合规自律（验收对照）

- 实名 UA `满房帮爬虫/1.0`（UTF-8 字节直发，curl 传输）
- 每次运行先核查 `robots.txt` 并留档 `logs/robots_YYYYMMDD.txt`；监控路径被新增 Disallow 则中止运行并告警
- 间隔 ≥3.5 秒、单线程、无并发；不下载图片；不碰登录态数据
- 遇验证码墙（302→/Login/LimitingCaptcha）**立即停手**，绝不绕过——这是设计行为，不是缺陷
- Cookie 仅做标准会话保持（`data/cookies.txt`），不用于伪装身份

## 七、已知边界与后续项

- **房态（soldout_flag）本期为 NULL**：木鸟详情页无当日房态直读字段，需日历接口二期验证；满房推断暂不可用，PRD F2 卖房估算口径需在 M3 前另议
- **坐标系未标定**（config `coord_system: unverified`）：数值已验证为大连真实坐标，gcj02/bd09 待与高德反查比对后标定（需项目方申请高德 Key，免费额度够用）
- 木鸟 WAF 有日频限（本日侦察实测约 40+ 请求触发验证码墙）：生产每日追踪 300 间 ≈ 18 分钟匀速请求，远低于阈值；但**同日不要重复全量跑**
- 途家已认领房源追踪接口、途家 sitemap（502）、飞猪 FlyAI 验证：附属侦察项，结论另出报告

## 八、目录结构

```
crawler/
  config.json            # 平台参数（翻页规则/告警阈值/路径）
  .env.example           # 环境变量模板（仅占位符）
  run_discover.py        # 发现层入口
  run_track.py           # 追踪层入口
  muniao_crawler/        # 包：http_client/parse/storage/alerts/discover/track/bootstrap
  tests/test_parse.py    # 离线单测（真实留档样本）
  data/                  # SQLite 库、状态、Cookie（运行生成）
  logs/                  # 按天滚动日志、robots 留档（运行生成）
```
