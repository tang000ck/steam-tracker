# Steam 饰品行情

每小时在 GitHub 云端读取 Steam 公开库存（CS2 和 Dota 2），查询社区市场最低售价并记录历史。价格可以在手机网页上查看，涨跌异常时通过 Bark 推送到 iPhone。

## 文件

| 文件 | 作用 |
|---|---|
| `tracker.py` | 读库存、查价格、写入 `data/`、发推送 |
| `config.json` | Steam ID、游戏、提醒阈值等设置 |
| `index.html` | 手机网页（GitHub Pages） |
| `.github/workflows/update.yml` | 每小时定时运行 |
| `data/` | 自动生成的价格数据，不要手动改 |

## 价格来源

Steam 官方市场接口屏蔽机房 IP，云端查不了，所以按游戏分开取价（`config.json` 里的 `sources` 可调，靠前的优先）：

- **CS2**：SteamDT（需要 `STEAMDT_KEY`，取其中的 Steam 平台价），失败时退回 Skinport
- **Dota 2**：Skinport，免 key；取可立即交易的最低挂单价，没有挂单时用冷却中挂单的价格
- **steam**：只有在本机运行时能用（家宽 IP 不被屏蔽）

Skinport 是站外现金交易平台，不收 Steam 的 15% 手续费，**价格比 Steam 市场低，但涨跌趋势一致**。

## 设置项（config.json）

- `alert_pct`：单件饰品 24 小时涨跌超过这个百分比就推送，默认 10
- `alert_cooldown_hours`：同一件饰品两次提醒的最短间隔（小时）
- `report_hours`：每隔几小时推送一次行情汇总，默认 6
- `currency`：23 表示人民币

## Bark 推送

仓库 Settings → Secrets and variables → Actions → New repository secret：
名称填 `BARK_KEY`，值填 Bark App 首页显示的推送地址或其中的 key。

## 本地测试

```bash
python tracker.py --no-push
```
