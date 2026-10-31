# sector 文档字段速查（`data/sector.json`）

校验为 Draft 2020-12 JSON Schema（`additionalProperties: false`，多余字段整条被拒）；根级必填只有 5 个：`schema_version`（服务端补 1）、`name`、`definition`、`coverage_stage`、`one_liner`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `unit_id` | string，可选 | 对应 `dalton_coverage_units.unit_id`；绑定关系以注册表为准，此处仅引用 |
| `kind` | `sector` / `theme` / `value_chain`，可选 | 覆盖形态；同一套知识模型 |
| `name` / `name_zh` | string，必填 name | 单元展示名 |
| `definition` | string，必填 | 研究对象定义 |
| `boundaries` | string | 研究边界：纳入与排除的范围 |
| `scope.regions` / `scope.markets` | string[]（两者同出现） | 地区 / 市场范围 |
| `value_chain[]` | `{stage, note?}` | 产业链环节，按上下游顺序 |
| `wiki_slug` | string | 共享 wiki 行业目录标识（只读映射，不当研究身份） |
| `research_tz` | string | IANA 时区；**P1 只保存，不参与到期计算**（按它调度属后续阶段） |
| `coverage_stage` | `onboarding`/`initial`/`full`/`maintenance`/`paused`，必填 | 覆盖阶段 |
| `members[]` | 见下 | 成员公司清单，**唯一权威来源**（P2 关联表届时作为它的投影） |
| `one_liner` | string ≤200，必填 | 一句话判断 |
| `covered_since` | `YYYY-MM-DD` | 开始覆盖日期 |

`members[]` 每项（id 形如 `M1`，`added_at` / `workspace_created` / `id` 可省略由服务端补）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string，必填 | 公司名 |
| `ticker` | string，可选 | 正规证券代码才填；**没有就省略，禁止为行业或未确认成员编 ticker** |
| `unit_id` | string，可选 | 该公司已登记公司覆盖单元时填其 unit_id |
| `workspace_created` | boolean，默认 false | 是否已建公司工作区——登记成员**不**自动建工作区 |
| `note` | string | 备注（如成员的业务范围限定） |
| `added_at` | isoTime | 登记时间 |

维护方式：`append /members`、`set /members/M1/...`、`remove /members/M2` 均可——成员清单是活文档（公司退出覆盖是正常编辑，不是历史）；行业论点的不可变约束在 thesis，不在这里。
