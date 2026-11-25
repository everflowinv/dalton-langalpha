# 存量模型滚动维护、安全写入与版本管理流程（`rollforward.md`）

本文规定模型目录判定、新版本命名、共用谱系记录（`model_lineage.json`）、安全扩列、预测转实际切换、外部插件与四层外部链接清理，以及保留扩展部件的包级 XML（Extensible Markup Language，可扩展标记语言）写入流程。

---

## 1. 目录统一、双目录判定与新版本命名

1. **统一正式目录**：单公司与行业多公司工作区统一将正式 `.xlsx` 模型存放在工作区根目录的 `model/` 下，匹配知识层模型台账路径规范（`^model/.+\.xlsx$`）。当次生成的更新脚本、`style_profile.json`、`model_lineage.json`、`mapping.md` 与 `part_diff.json` 存放在 `research/<YYYY-MM-DD>-model-<动作>/` 下。
2. **`model/` 与历史 `models/` 并存判定**：
   - 先计算两侧 `.xlsx` 文件的 `SHA-256` 哈希：哈希相同视为同一文件的重复副本，以 `model/` 下的路径为准；
   - 哈希不同时，先读取 `data/models.json` 的 `current` 版本标识（如 `"v12"`），在 `versions[]` 数组中找到 `id` 等于 `current` 的条目并读取其 `path` 字段，结合用户在当前会话中明确指定的底稿确定唯一维护起点；
   - 若 `data/models.json` 尚未登记且用户未指定底稿，先列出两边文件的路径、修改时间、版本号与核心页差异交用户确认起点，后续新版本统一写入 `model/`，原 `models/` 下的文件保持只读留存。
3. **原件只读与递增版本命名**：
   - `sources/` 中的用户底稿与 `model/` 下全部历史版本始终保持字节级只读；更新前记录基准文件 `SHA-256` 并写入新版本 `README & Changelog` 页，交付前核对原件哈希未变。
   - 单公司工作区新版本命名为 `model/<TICKER>-model-v<NN>.xlsx`（两位递增版本号）。
   - 行业多公司工作区新版本命名为 `model/<Industry_Slug>_Model_v<NN>_<YYYYMMDD>.xlsx`（空格转为下划线）。

---

## 2. 共用版本解析与 `model_lineage.json` 谱系记录

每次生成或修订输出模型时，调用 `model_style.py lineage` 在当次研究目录写入 `model_lineage.json`：

```bash
python .agents/skills/dalton-model/scripts/model_style.py lineage \
  --file research/<YYYY-MM-DD>-model-<动作>/model_lineage.json \
  --output model/<新版本文件名.xlsx> \
  --baseline <实际输入底稿路径.xlsx> \
  --profile research/<YYYY-MM-DD>-model-<动作>/style_profile.json \
  [--fiscal-year-end-month <结账月>]
```

- **最终交付指针（`final_delivery`）**：同一运行产生初稿与修订稿时，中间草稿可加 `--draft` 仅写入 `entries[]`；最终交付稿不加 `--draft`，将 `final_delivery` 更新为最终交付文件的路径、`SHA-256` 哈希、实际输入底稿路径与哈希及样式画像路径。
- **全链路统一读取**：后续的 `style_audit.py --lineage <model_lineage.json>`、中断恢复执行、只读核查与完成契约验证统一从 `final_delivery` 读取交付文件与实际基准原件，避免按磁盘文件名排序选版或按版本号减一猜底稿。
- **未预测留空登记**：可在 `model_lineage.json` 的 `blank_forecast_registrations` 字段，或在 `mapping.md` / `README & Changelog` 中登记允许留空的未预测科目（含 `sheet`、`row`、`period`、`reason` 四项）。

---

## 3. 输出副本扩列与边界求和区间重建

1. **只在输出副本上调用 `insert.py`**：先复制原底稿或通过 `--out` 生成新版本副本，再调用 `.agents/skills/xlsx/scripts/insert.py columns` 在新年度列与新季度列插入位扩列，并传 `--copy-style-from <相邻同类列>` 复制列宽、行高与行级样式：

```bash
python .agents/skills/xlsx/scripts/insert.py columns <基准或工作副本.xlsx> \
  --sheet "<工作表名>" --at <插入列字母> --count <新增列数> \
  --copy-style-from <相邻同类列字母> --out <新版本路径.xlsx>
```

2. **核查返回结果**：每次调用后检查返回 JSON 中 `status == "success"` 且 `self_check.ok == true`，逐条审阅 `warnings`。
3. **显式重建边界外汇总公式**：`insert.py` 仅在插入点位于原求和区间内部时自动扩展 `SUM(起:止)`；在末尾追加新季度列或新年度列属于边界外扩列，扩列后必须显式写入：
   - 新季度加总年的流量求和 `=SUM(Q1:Q4)`（或四格相加）、时点余额 `=Q4`、加权股本 `=AVERAGE(Q1:Q4)` / `=Q4`、年度 `EPS` 与衍生比率反算公式；
   - 顺延 `CAGR` 列的起止年份引用与幂次；
   - 核对横向汇总区间、扩展条件格式（`<extLst>` 中的 `sqref`）和图表数据区域（`xl/charts/*.xml` 中的 `<c:f>`）已覆盖新增列。

---

## 4. 预测转实际与外部插件/四层外链抽离替换

### 4.1 财报后预测转实际

1. 将刚披露季度的列头由预测标签切换为原表一致的实际标签（如 `3Q26E` → `3Q26`，或 `2026Q3E` → `2026Q3`）。
2. 将一手法定财报披露的实际值写入对应单元格，套用原表实际值样式（默认蓝字 `0000FF`、无底色、无边框），并在最右侧红字备注列写明财报来源与期间。
3. 核对包含该季度的当财年加总年公式自动吸收新实际值，重新检查现金、有息债、利息与股本的期初衔接。

### 4.2 外部插件函数与四层外部工作簿链接替换

原模型中若含 `Capital IQ`（`_xll.ciqfunctions`、`CIQ`）、彭博终端函数（`BQL`、`BDH`、`BDP`）、`RTD`、`WEBSERVICE` 或指向外部 `.xlsx` 的链接：

1. 在对应工作表下方或 `Inputs` 区建立独立的静态输入格（蓝字输入样式），在最右侧红字备注列写明可核验的数据来源与截至日期（如最新收盘价、汇率、总股本、同行可比数据）。
2. 将原插件公式或外链单元格改写为引用上述静态输入格的标准 Excel 公式，并同步清理指向外部工作簿的命名区域（`definedNames`）。
3. 清理完所有外链公式与命名区域后，使用 `model_style.py` 同步清除压缩包内的全部四层外部链接残留：
   - `xl/workbook.xml` 中的 `<externalReferences>` / `<externalReference>` 节点；
   - `xl/_rels/workbook.xml.rels` 中的 `externalLink` 关系条目；
   - `xl/externalLinks/*` 全部部件（含 `.xml` 与 `_rels/*.xml.rels`）；
   - `[Content_Types].xml` 中 `/xl/externalLinks/...` 的 `<Override>` 声明。

```bash
python .agents/skills/dalton-model/scripts/model_style.py clean-external-links \
  <已替换外链公式的工作副本.xlsx> --out <清理外链后新版本.xlsx>
```

---

## 5. 保留扩展部件的包级 XML 补丁写入与写后校验

当 `style_profile.json` 的 `package_info` 显示原工作簿包含 `<extLst>` 扩展节点（如条件格式扩展）或 `xl/media/*` 媒体部件（如 `.emf` / `.wmf` 矢量图）时，直接用 `openpyxl` 整本保存会丢失这些部件。此时改用 `model_style.py` 提供的包级补丁函数 `patch_workbook_xml`：

- 在 `insert.py` 输出的 ZIP 包基础上，用 `lxml` 仅更新目标工作表的 `<sheetData>` 单元格节点，关闭 `xl/workbook.xml` 中的迭代计算（`iterate="0"`、`fullCalcOnLoad="1"`），按需平移 `<extLst>` 的 `sqref` 与图表 `<c:f>` 引用区域，并原样保留工作表尾部的 `<extLst>`、`<legacyDrawing>` 及全部媒体与绘图部件。
- 写盘后执行部件对比并确认文件可正常重算与重新打开：

```bash
python .agents/skills/dalton-model/scripts/model_style.py part-diff \
  <原模型.xlsx> <新模型.xlsx>
```

核对返回 JSON 中 `extlst_preserved == true`、`media_preserved == true`、`charts_preserved == true` 且 `new_external_link_parts == 0`，随后运行 `recalc.py` 与 `style_audit.py --strict`。
