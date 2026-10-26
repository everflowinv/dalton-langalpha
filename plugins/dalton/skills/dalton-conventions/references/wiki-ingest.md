# Company Wiki 入库流程（`ingest_company_wiki`）

`company-wiki` 默认只读。只有当用户明确要求入库（如“入库”、“存一下”、“加到 wiki”）时，才在 Chief of Staff（Home）或公司工作区（Analyst）中按本规范调用 `ingest_company_wiki`。

---

## 1. 先分 sales notes 还是 wiki（卖方材料不进 wiki）

入库前先判断材料性质：

- **卖方材料（严禁进 wiki，请提示用户走 sales notes）**：
  - 券商研报、卖方深度/点评报告
  - 卖方 sales color、交易台观点汇总
  - 卖方分析师电话会纪要
  - 券商撰写的会议 takeaways / NDR 总结报告
- **允许进 company-wiki 的材料**：
  - 公司管理层一手交流纪要（Group Meeting、1x1、公司发言的 NDR 纪要、Fireside Chat）
  - 专家访谈（TB Call、CIC Call、高临访谈、券商组织的专家一手访谈）
  - 买方交流纪要、团队自撰的季度研究笔记 / Earnings Update / 研究笔记 / 投资备忘
  - 行业趋势、新闻更新、Flomo 笔记、微信文章、IPO 招股书

---

## 2. Slug 与内容类型（`content_type`）规则

### 2.1 目标与 Slug（`target_type` / `target`）
- `target_type` 取 `"company"` 或 `"sector"`。
- **公司（`company`）**：
  - 已有目录或已在 `config.json` 配置别名的公司可直接传入（美股直接用大写 ticker，如 `"NVDA"`、`"META"`）。
  - 新建公司目录必须匹配 `^[A-Z][A-Z0-9-]{0,29}$`（如 `"UNITREE"`）。
  - `temp*` 前缀保留禁用；**纯数字 ticker（如 `"00700"`、`"600519"`）直接拒绝**。
  - **A 股 / 港股 / 中文新名称不自动加别名**：若 bridge 返回 `status: "needs_alias"`，告知用户联系管理员在 OpenClaw `company-wiki/config.json` 的 `aliases` 中添加映射后再入库。
- **行业（`sector`）**：
  - 已有或预定义行业（`us-semiconductor`、`china-consumer`、`china-internet`、`us-software`、`us-financials`、`us-megtech`、`macro`）或匹配 `^[a-z][a-z0-9-]{0,39}$` 的小写连字符标识（如 `"china-robotics"`）。

### 2.2 允许的 `content_type` 列表
`content_type` 必须取自以下允许值（传卖方类型会被直接拒绝并提示走 sales notes）：
- `管理层会议纪要`
- `专家访谈`
- `季度研究笔记`
- `Earnings Update`
- `NDR纪要`
- `Fireside Chat`
- `买方交流`
- `研究笔记`
- `新闻更新` / `News Update`
- `行业趋势` / `Industry Trends`
- `Flomo笔记`
- `微信文章`
- `IPO Prospectus`
- `投资备忘` / `Investment Memo`

### 2.3 内容与文件限制
- `content`（纯文本）与 `file_path`（当前工作区或 Home 内的文件路径）二选一：
  - 文本长度 ≤ 200,000 字符，非空。
  - 文件大小 ≤ 20 MB，扩展名白名单：`.md`、`.txt`、`.pdf`、`.docx`、`.png`、`.jpg`、`.webp`；禁止符号链接与越界路径。
  - 含疑似密钥（API key、token、PEM 私钥）或重复内容（正文 sha256 已入库，或同 `slug + date + content_type` 且正文高度相似）会被拒绝（返回 `secret_detected` 或 `duplicate`）。

---

## 3. 两步入库流程：预览（`confirm=False`）→ 用户确认 → 写入（`confirm=True`）

严禁跳过预览直接传 `confirm=True`。必须分两步执行：

1. **第一步：调用预览（`confirm=False`）**
   ```python
   ingest_company_wiki(
       target_type="company",
       target="NVDA",
       content_type="专家访谈",
       date="2026-10-25",
       source="Expert Call",
       content="<正文内容>",
       confirm=False,
   )
   ```
   或入库当前工作区文件：
   ```python
   ingest_company_wiki(
       target_type="company",
       target="NVDA",
       content_type="管理层会议纪要",
       date="2026-10-25",
       file_path="notes/nvda_ir_call.md",
       confirm=False,
   )
   ```
   向用户展示预览结果：解析后的 `target`（slug）、`content_type`、目标文件名 `filename` / `filepath`、大小（`size_bytes` / `char_count`）、是否疑似卖方（`suspected_sell_side`）及是否重复（`duplicate`）。
2. **第二步：用户确认后正式写入（`confirm=True`）**
   收到用户明确确认后，使用相同参数并设 `confirm=True` 调用：
   ```python
   ingest_company_wiki(
       target_type="company",
       target="NVDA",
       content_type="专家访谈",
       date="2026-10-25",
       source="Expert Call",
       content="<正文内容>",
       confirm=True,
   )
   ```

---

## 4. 入库后回读核对与自动标签检查

`confirm=True` 返回 `doc_id`（即 `wiki:<doc_id>`）与 `filepath` 后，必须执行入库后核验：

1. **回读核对**：在 `execute_code` 中调用 `researchdata.research(route="company-wiki", operation="read", document="wiki:<doc_id>")`，核对入库文档标题、日期、来源及正文内容完整无误。
2. **检查自动标签**：检查入库返回及回读结果中的自动标签（`tags`、`related_companies`、`related_sectors`），并向用户汇报入库路径、`wiki:<doc_id>` 与自动生成的标签。
