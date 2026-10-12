# POI 研究规则 v3 人工复核结果

依据用户 2026-10-12 最新 24 项最终决定，按 class_code 精确匹配生成；未重审其他类别。

|版本|include|exclude|review|合计|
|---|---:|---:|---:|---:|
|v2 最终判断|578|17|7|602|
|v3|585|17|0|602|

版本：`poi-research-v3.0.0`。24/24 项落实，实际决定改变 8 项（exclude → include 1 项、review → include 6 项、review → exclude 1 项）。

比较基准为 v2 完整表的 decision，已逐条核对等于 v2_changes 的 new_decision；没有使用代表 v1 的 old_decision。

## 实际改变的决定

|class_code|原始 class_name|v2 判断|v3 判断|
|---|---|---|---|
|02030047|CONSTRUCTION PLANT|exclude|include|
|03190257|DESIGNATED SCENIC FEATURES|review|exclude|
|06340433|ELECTRICAL FEATURES|review|include|
|06340437|GAS FEATURES|review|include|
|06340438|METEOROLOGICAL FEATURES|review|include|
|06340443|TELECOMMUNICATIONS FEATURES|review|include|
|10560754|RIVERS AND CANAL ORGANISATIONS AND INFRASTRUCTURE|review|include|
|10570738|RAILWAY STATIONS, JUNCTIONS AND HALTS|review|include|

02030047（CONSTRUCTION PLANT）已由 exclude 改为 include，新理由明确覆盖此前排除指示。所有 8 项的新理由只记录本次人工决定及原/新判断，不推断未提供的事实。

## 待分组清单

以下 8 项全部为 include，原 poi_function_group 空值保留；未填组，未降为 review。

|class_code|原始 class_name|decision|poi_function_group|
|---|---|---|---|
|02030047|CONSTRUCTION PLANT|include|（原空值）|
|06340433|ELECTRICAL FEATURES|include|（原空值）|
|06340437|GAS FEATURES|include|（原空值）|
|06340438|METEOROLOGICAL FEATURES|include|（原空值）|
|06340443|TELECOMMUNICATIONS FEATURES|include|（原空值）|
|06340802|WIFI HOTSPOTS|include|（原空值）|
|10560754|RIVERS AND CANAL ORGANISATIONS AND INFRASTRUCTURE|include|（原空值）|
|10570738|RAILWAY STATIONS, JUNCTIONS AND HALTS|include|（原空值）|

## 核对范围

- 保留全部 602 条、原始行列顺序、代码前导零、类别名称、来源分类、rule_origin 和全部原始空值。
- 全部功能组逐值不变；未指定类别仅更新版本，不改决定、理由或其他字段。
- 仅实际改变的 8 项重写 decision_reason，其余理由保留；10540832 的 class_name 仍为空。
- v1/v2 原表及历史交付不覆盖；不改代码、测试或运行配置。当前清洗流程仍使用 v1，v3 规则接入需要后续工作。
- 文件对账仅验证本轮规则表，不代表 POI 点位清洗、全源分析或第 11 步整体已验收。

完整核验：`poi_research_rules_v3_validation.json`。独立复核和快照归档：
`/mnt/data/work/mazhichuan/analysis_Digimap_POI_v2/outputs/step11-rules-v3-20261012T130234Z`。
