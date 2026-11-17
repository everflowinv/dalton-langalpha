# cn-hk-findata

A/H/US 股票数据分析 Skill，基于 AkShare + cninfo，支持自然语言查询。

## 功能覆盖

- **87 个 intent** — 覆盖资金流、财务三表、业绩预告、涨跌停、股东分析、行情、指数、板块、热度、情绪、新闻、龙虎榜、两融、IPO、ESG 等
- **5593 条公司名映射** — 支持 "茅台"、"腾讯"、"英伟达" 等自然语言名称
- **70+ 同义词** — 支持口语化表达（"贵不贵"、"谁在买"、"业绩暴雷"）
- **组合查询** — "茅台的业绩预告以及股东户数变化" 自动拆分合并
- **市场智能路由** — 港股 ticker 自动跳过 A 股接口，反之亦然

## 快速开始

```bash
# 1. 安装依赖（自动创建 venv）
bash run.sh --self-test --json

# 2. 自然语言查询
bash run.sh --json --backend akshare --ask "茅台资金流向"
bash run.sh --json --backend akshare --ask "今日涨停"
bash run.sh --json --backend akshare --ask "腾讯财报怎么样"
bash run.sh --json --backend akshare --ask "业绩预告"

# 3. 组合查询
bash run.sh --json --backend akshare --ask "茅台的业绩预告以及股东户数变化"

# 4. 确定性命令（cninfo 公告）
bash run.sh --json --ticker 00700 --limit 120 --max-pages 8 --list-filings
```

## 架构

```
├── SKILL.md                          # 完整功能文档（给 AI agent 读的）
├── run.sh                            # 入口脚本（自动 venv + 依赖安装）
├── requirements.txt                  # Python 依赖
├── fetch_announcements.py            # cninfo 公告抓取（legacy）
└── scripts/
    ├── intent_registry.yaml          # 87 个 intent 定义（YAML 驱动）
    ├── financemcp_adapter.py         # 核心路由引擎
    └── stock_name_cache.json         # 5593 条公司名→代码映射
```

### 路由流程

1. **同义词归一化** — 口语 → 标准金融术语
2. **Registry 精确匹配** — YAML 关键词打分 + 模糊匹配
3. **Market filter** — 按 ticker 格式过滤不兼容的 intent
4. **多候选 fallback** — 第一个失败自动尝试下一个
5. **通用 fallback** — 遍历全部 399 个 AkShare stock_* 函数兜底

### 添加新 intent

编辑 `scripts/intent_registry.yaml`，加一条：

```yaml
- id: my_intent
  keywords: [关键词1, 关键词2]
  function: stock_xxx_em
  needs_ticker: true
  args:
    symbol: "{ticker6}"
  description: 中文描述
```

不用改任何代码。运行 `--self-test` 验证即可。

## 依赖

- Python 3.10+
- akshare >= 1.16.0
- pyyaml >= 6.0

## License

MIT
