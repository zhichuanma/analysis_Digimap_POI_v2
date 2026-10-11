# Digimap POI 分析规范

本文件固定逐步重写期间的分析口径。若实现过程中需要改变口径，先说明原因、受影响的结果和验证方法，再修改本文件与 `PROGRESS.md`。本文件描述目标语义，不表示相关代码已经实现。

## 研究对象和目标

目标是统计 PUA 范围内充电站周围累计 500 m 和 1000 m 直线半径内、处于数据覆盖区域且通过后续 POI 筛选的 Digimap POI 数量及其原生分类组成，同时保留数据清洗、覆盖判断、审计和结果追溯信息。POI 可位于目标 PUA 外。

- 站点的分析单位是合并后的充电站实体，不是未经整理的 CSV 行。相同坐标但不同 `StationID` 的记录视为数据错误，合并成一个 `station_key`；所有原始行和 ID 必须保留在审计中，不能让这类错误重复影响站点统计。缺失 ID、同一 ID 的位置冲突以及其他无法确定的冲突仍须单独标记并按后续站点准备规则处理。
- 只有位于明确选定的目标 PUA 范围内的充电站实体进入分析。PUA 是研究范围过滤条件，不新增城市等级字段；目标范围外的站点不参与匹配或汇总。目标采用全部 PUA 还是指定子集须显式记录，不能用成功连接到 LAD 的子集悄悄代替目标清单。
- 第 09 步负责以新项目 `cities/` 的 PUA 定义连接 `data/raw/geography/` 的 LAD 边界，构建逐 PUA 及目标联合研究范围，并筛选充电站源记录。字段连接、版本和空间边界语义须在实施前明确；旧项目没有 PUA 数据，不能作为边界来源。
- POI 的计数单位是处于数据覆盖区域、通过后续业务筛选和数据清洗的 Digimap `ref_no` 记录，并按与入选可查询站点的距离进入各半径统计。用户于 2026-10-11 在第 10 步方案轮明确：仅 EVCS 要求在目标 PUA 内；POI 允许来自目标 PUA 外，只要落在站点 buffer 内且处于有数据覆盖的区域。POI 无需属于站点所在 LAD、PUA 或任一目标 PUA；源数据中的所有 POI 仍不会自动进入分析，后续筛选须记录保留/排除原因。
- 对两个不同的已合并站点，如果同一个合格 POI 落入各自半径，该 POI 可在两个站点的结果中各贡献一次；同一站点内一个 `ref_no` 只计一次。总体“唯一 POI”指标另按 `ref_no` 去重。
- 默认不把充电站 CSV 的站点补入 POI。默认排除明确的充电设施分类码 `10540832`；是否打开该设施开关必须显式记录。
- 暂不增加城市等级、回归、研究分类映射或其他未在路线中定义的指标；PUA 仅用于限定研究对象范围。

## 距离和坐标

- 半径只有 `500 m` 和 `1000 m`，都是累计圆：1000 m 结果包含 500 m 结果。
- 站点经纬度按 WGS84 解释，使用第 08 步固定、离线七参数 pipeline 转换为近似 EPSG:27700 坐标，完整定义见下文第 08 步。转换不确定性按米记录；不能依赖机器上偶然安装的网格或在线下载。
- 距离使用 POI 几何坐标和站点投影坐标计算平面欧氏距离：`sqrt((x_poi - x_station)^2 + (y_poi - y_station)^2)`。最终纳入条件是 `distance_m <= radius_m`，边界上的点保留，微小越界点排除。
- 不用经纬度差直接近似米制距离。属性坐标和几何坐标不一致时，匹配仍以经审计的 POI 几何为准，差异单独保留审计信息。

## 原生分类

结果保留 Digimap 的 `group / category / class` 三层原生体系。用于格式检查、拆分和字典连接的分类码按字符串处理，以保留已有前导零；`raw_code` 保留原始值及其类型。

- `group_code` 为原生八位码的前 2 位。
- `category_code` 为原生八位码的前 4 位。
- `class_code` 为完整八位码。
- 格式合法但字典未知的八位码仍保留其前缀并进入审计；格式不合法或空值归为 `UNKNOWN`，同时保留原始值和原因。
- 官方字典的展示名称与代码关联，代码是稳定连接键。第 06 步统计真实源记录频数，第 07 步接入官方字典；频数检查不提前判断官方字典是否收录。

### 第 02 步分类接口与已确认规则

- `classify(raw_code, dictionary, *, include_ev_charging=False)` 每次处理一个分类值；测试字典以完整八位字符串为键，本步只检查键是否存在，不读取正式标签文件。
- 原始值单独保存在 `raw_code`。非缺失值通过 `str()` 转换后检查是否完整匹配 `[0-9]{8}`；不去除首尾空格、不补零、不猜测或修复编码。整数 `10540832` 转为合法的八位码；整数 `1010006` 转为七位码后仍属格式异常；浮点数 `10540832.0` 不会被修复为整数码。
- 本步普通 Python 标量接口将 `None`、浮点 `NaN` 和空字符串视为空值；纯空白字符串属于格式异常，字符串 `"nan"`、`"None"` 不作为缺失值猜测。真实文件的其他缺失值表示须在后续输入接入时明确处理。
- 输出为 `ClassificationResult`，包含 `raw_code`、`group_code`、`category_code`、`class_code`、`classification_status`、`include_in_count` 和 `audit_reasons`。状态分别为 `known`（字典已知）、`unknown_dictionary`（格式合法但字典未知）、`missing`（空值）、`malformed`（格式异常）。
- 空值和格式异常的三层代码均为 `UNKNOWN`，`include_in_count=True`，审计原因分别为 `missing_code` 和 `malformed_code`。合法未知码保留三层代码，审计原因是 `unknown_dictionary_code`，不会仅因字典未知被排除。
- `include_ev_charging` 必须为布尔值。仅字符串转换后精确等于 `10540832` 的码受该开关影响：默认 `include_in_count=False`，增加 `charging_facility_excluded` 审计；打开时设为 `True`。设施判断不依赖字典收录情况，分类状态与设施排除原因分别保留。
- `include_in_count` 只表示通过本步分类规则，最终仍须满足后续空间范围、业务筛选和清洗条件。本步不决定最终 group 汇总是否展开 `UNKNOWN`，也不执行空间计算或正式统计。

## 输入清洗、覆盖和可观测性

- 缺失 `ref_no`、无效或空几何、非点几何、重复 `ref_no` 的冲突行明确排除；不做名称或位置模糊合并。重复清单必须跨完整源数据生成，不能只在单个批次内判断。
- 低位置精度、几何与属性坐标差异和未知分类是审计原因，不自动等同于排除；每项处理动作要可追溯。
- 第 09 步判定目标 PUA 研究范围；第 10 步以全部 E/S/W LAD 的 union 作为 GB 保守覆盖代理，NI 单独保留，并结合订单范围判断覆盖。目标 PUA 边界表达研究选择，GB/订单边界表达数据覆盖，二者分别记录，不能互相替代。站点状态至少区分 `selected`、`query_eligible` 和按半径判断的 `coverage_complete_proxy`。
- 可查询且没有匹配的站点记为可观测的 0。不可观测站点记为缺失，不补零。边缘站点可以保留观测值，但覆盖不完整时可解释的完整覆盖统计留空。
- 完整覆盖要求站点可查询、位于 GB 代理内，并且到 GB 边界和订单边界的余量都至少等于当前半径；数值条件使用 `>=`。
- 数据覆盖的操作性定义沿用第 10 步 GB 与订单范围的交集，保留 `proxy` 标识及来源限制；须先核实订单与当前交付的对应、CRS 和单位，不能从 POI 点集极值推断。仅因圆跨越 PUA 边界，不降低完整覆盖状态。行政边界与订单代理不证明交付内每个真实设施均已收录。

## 分母、零值和汇总

- 总数为 0 时，分类份额为空，不把 0 当作有意义的组成分母。
- `group` 层为可观测站点保留零单元；`category` 和 `class` 层使用正计数稀疏表。稀疏缺行只有在站点可观测时才能解释为 0；不可观测站点不是 0。
- 主要组成均值只使用完整覆盖代理范围且总数大于 0 的站点，站点等权。计数分布保留完整范围内的零站点。
- 汇总同时区分全部可观测站点和完整覆盖站点，并明确各自的站点数、匹配次数和唯一 `ref_no` 数。站点等权份额与合并匹配份额是不同指标，不能互换。

## 目标产物的行含义

### 第 03 步手算原型

用户已指示按第 03 步方案实现；本步采用以下样本修订和内存接口：

以下保留当时已验收原型的真实接口。2026-10-11 的范围修订已取代其中 POI 必须 `in_pua=True` 的分析口径；当前 `src/spatial.py` 的原型仍有这一判断，本轮未修改该原型。第 10 步的站点/覆盖准备不调用该匹配原型；后续正式匹配接入时须显式替换旧资格条件并更新相关测试，不能沿用 `outside_pua` 排除或通过伪造 `in_pua=True` 绕过。

- 样本 B 的 S3 坐标从路线中的 `(0, 0)` 调整为 `(100, 500)`，与 S1 成为不同坐标的分析实体，避免违反同坐标站点合并口径。S1、S2、S4 及所有 POI 沿用样本 B 的坐标。覆盖标志仍是人工测试条件，不代表真实几何覆盖已验证。
- `match(stations, pois, dictionary, *, include_ev_charging=False)` 输入为字典记录的可迭代对象。站点包含字符串 `station_key`、`xy`、布尔 `selected` 与 `query_eligible`；POI 包含字符串 `ref_no`、`xy`、`raw_code`，以及显式布尔 `in_pua`、`passes_business_filter`、`passes_cleaning`。这些资格是已准备样本的前提，不由原型读取真实数据判断。
- 输入站点必须已经按实体合并，POI 的 `ref_no` 必须已经唯一。重复站点键、同坐标的已选择可查询站点、重复 `ref_no` 都会报错；本步不代替后续真实站点合并或重复 POI 清洗。
- `match()` 复用 `classify()` 和 `naive_neighbors()`，仅为已选择且可查询站点生成 1000 m 内唯一匹配对；返回 `matches`、所有输入 POI 的 `poi_audit` 及实际 `include_ev_charging`。审计分别保留分类纳入标志、分类原因、综合 `eligible_for_matching` 与 `exclusion_reasons`，设施开关不能绕过 PUA、业务或清洗资格。
- `summarize(stations, matches, *, group_codes)` 要求每个已选择站点提供 `coverage_complete_proxy={500: bool, 1000: bool}`；覆盖完整以可查询为前提，但不由匹配数或另一半径的状态推导。重复匹配对、不可观测站点的匹配、越界距离和遗漏匹配 group 的展开清单会报错，避免静默丢失或重复计数。
- group 展开域显式输入，基础样本为 `01`、`99`，设施开启样本增加 `10`；异常码用例显式包含 `UNKNOWN`，不据此固定正式 group 汇总的默认展开规则。category/class 只保留正计数。
- `summarize()` 返回内存列表 `station_radius_totals`、`composition`、`radius_summary`、`group_summary`。总数表只包含已选择站点；`observed_count` 区分可查询零值与不可查询 `None`，`interpretable_count` 仅在当前半径完整覆盖时保留。组成表仅包含可观测站点，`share` 在总数为 0 时为 `None`；覆盖不完整站点仍保留观测组成。
- 摘要的 `scope` 分为 `all_observable` 和 `complete_coverage`，主统计使用后者。各自记录站点数、正总数站点数、零站点数、匹配次数、唯一 POI 数及包含零站点的计数分布；group 摘要分别保存正总数站点等权份额与合并匹配份额，以及各自分母。

### 后续正式文件

这些是后续步骤的目标语义；第 00 步不生成它们。

- `matches.parquet`：一行代表一个唯一的 `(station_key, ref_no)` 站点—POI 对。这里的 `station_key` 是目标 PUA 内、合并同坐标源记录后的可查询分析站点键，`ref_no` 是处于数据覆盖区域、通过后续业务筛选和清洗且距站点不超过 1000 m 的 POI 记录；POI 可位于目标 PUA 外。保存站点标识、源 POI 标识、分类代码和 `distance_m`。单行距离支持两个累计半径，不为两个半径重复存储同一对。
- `station_radius_totals.csv`：一行代表一个已选择站点记录与一个半径，包含可观测/覆盖状态和该半径内总匹配数；不可观测不伪造为 0，覆盖不完整的观测值仍可保留但不进入完整覆盖可解释统计。
- `composition.parquet`：一行代表一个站点、半径、分类层级和代码的数量及份额；group 保留可观测零单元，category/class 按正计数稀疏表示。
- `radius_summary.csv`：一行代表一个半径及明确分母下的总体匹配、唯一 POI、站点组成和覆盖摘要。
- `group_summary.csv`：一行代表一个半径和一个 group 代码，分别记录站点等权组成与合并匹配份额所需的分母。

## 数据和运行边界

真实数据在新项目内独立存放，配套来源说明和 `data/source_manifest.json`；原始数据与运行产物不进 Git。用户已确认当前环境就是服务器，代码和测试可在当前新项目检出目录执行；不能把未经来源核实的已有文件或旧项目历史结果当作本轮真实分析输入。

文档阅读和编辑在当前工作区进行；代码、测试和真实分析默认在服务器新项目执行。服务器项目目录在数据盘 `~/Work/analysis_Digimap_POI_v2/` 下自行创建，运行产物写入其 `outputs/<run-id>/`，不写入代码检出目录。每次运行使用新的 `run-id`，并在后续步骤记录输入、配置、代码和产物校验信息。

当前服务器主机为 `3018893001`，代码检出目录是 `/mnt/data/work/mazhichuan/.offloaded-from-system/20261007-full-scan/Projects/analysis_Digimap_POI_v2`。用户在第 06 步进一步说明：个人工作目录使用大写 `~/Work`，位于 18T 数据盘 `/mnt/data/work/<用户名>`，用于个人数据集、项目和大文件，按用户说明仅本人可访问。本机已核对 `~/Work` 与历史小写 `~/work` 均链接到 `/mnt/data/work/mazhichuan`；从本步起统一使用 `~/Work`，实际项目数据根仍为 `/mnt/data/work/mazhichuan/analysis_Digimap_POI_v2/`，无需移动数据或改变权限。代码、测试和分析直接在当前服务器执行，无需另行 SSH；使用 `python -B` 避免在代码检出目录生成字节码缓存。

### 第 04 步路径与配置接口

用户已授权按第 04 步方案实现。路线中“所有路径从 `run.py` 所在根解析”的示例按上述数据盘约定调整为两个独立根；不改变统计口径。

- 代码根由 `run.py` 的实际位置确定，数据根由程序按 `~/Work/analysis_Digimap_POI_v2/` 定位。二者不能相等或互相包含，且不由 YAML 或命令行指定。第 04 步最初采用小写 `~/work`，第 06 步按用户补充的目录约定统一为大写；本机两者实际指向同处。
- `project_path(relative_path, *, root, field="path")` 接收原始相对路径字符串和显式绝对根，返回解析后的绝对 `Path`。拒绝空路径、绝对或带盘符的路径、任何 `..` 分量、反斜杠和 NUL 字符；不展开配置路径中的 `~` 或环境变量。
- 先检查原始路径，再解析符号链接并按路径层级检查归属。内部链接可以通过，指向所属根之外的链接、越界的已有父目录及符号链接循环报错；目标尚不存在也必须通过父目录检查。本步不创建输入或输出目录。
- `load_config(config_path="config.yaml")` 从代码根读取相对路径配置；绝对配置文件路径即使位于代码根内也拒绝。使用 PyYAML 6.0.3 的安全加载器并额外拒绝重复键；配置须为映射，缺少必填项、未知字段或错误类型明确失败。
- `validate_config(config)` 返回有效参数和解析后路径，不修改输入配置。`radii_m` 须为有限正数、严格递增且唯一，并且等于现有的 `[500, 1000]`；不自动排序、去重或转换字符串。`classification_mode` 只接受 `native`，`include_ev_charging` 必须是布尔值，示例默认 `false`。
- `poi_batch_size` 和 `station_batch_size` 必须是正整数，布尔值不视为整数；示例默认分别为 `10000`、`1000`。批次参数供后续处理调整内存与速度，本步不声称已验证分批性能或结果不变性。设施开关会改变分类纳入对象，仍须满足既定的后续空间与清洗条件。
- `paths` 包含 `raw_dir`、`source_manifest`、`cities_dir`、`geography_dir`、`output_dir`，均相对数据根。解析后的目录分别限制在 `data/raw/`、`cities/`、`data/raw/geography/` 和 `outputs/` 内，可以选择子目录；geography 还须位于所选 raw 目录内。来源清单位置固定为 `data/source_manifest.json`。
- 输出与项目根、配置中的输入路径及整个保留输入区域不能重叠，检查祖先/子目录关系和符号链接解析后的真实位置。每次实际运行继续使用独立的 `outputs/<run-id>/`；完整运行目录管理与防覆盖调度留待后续步骤。
- 最小命令是 `python -B run.py --check-config --config config.yaml`，成功输出 JSON 中的 `status=config_valid`、`inputs_verified=false`、有效参数和解析路径，退出码 0；失败指出字段与原因，退出码 2。只读取配置并检查路径，不创建目录、不调用匹配或统计，也不支持完整阶段调度。
- 真实输入存在性、内容和来源完整性本步不验证；固定离线七参数 pipeline 仍未确认，留到第 08 步核实后再补配置。合成测试、日志与验证输出位于服务器数据根的新 `outputs/<run-id>/` 中，不进入代码检出目录或 Git。

### 第 05 步只读输入与来源完整性接口

用户已授权按第 05 步方案实现以下接口与必要验证。合成测试通过和真实输入核验通过分别记录，不因实现接口就声称真实副本完整。

- `open_source(relative_path, *, data_root)` 提供只读二进制上下文，路径复用第 04 步检查；声明位置及符号链接解析后的路径都须位于数据根的 `data/raw/` 或 `cities/`。只打开普通文件，不接受输出目录或来源清单作为原始数据，不提供写模式、不修改权限。目录、FIFO 等非普通文件明确拒绝。
- `sha256(relative_path, *, data_root, chunk_size=1048576)` 对完整原始字节分块计算 SHA-256，返回小写 64 位十六进制摘要。块大小须为正整数，不接受布尔值；不解码、不转换换行、不解压或把整份大文件载入内存。核验同时累计实际字节数；检测到读取期间文件大小、身份或修改状态变化时失败，不将不稳定读取记作通过。
- `verify_sources(manifest_path="data/source_manifest.json", *, data_root, required_paths)` 只读取该数据根的来源清单和声明副本，返回可序列化的报告；失败抛出含 `.report` 的 `SourceVerificationError`。函数不生成/修复来源记录，不写报告文件，由调用方写到本次独立 `outputs/<run-id>/`。
- `required_paths` 是调用方根据本次运行需求独立确认的非空列表或元组，不能从来源清单反推。按规范化相对路径对账；重复项及解析到相同路径的别名重复均拒绝。必需项必须全部出现在清单中，所有清单声明的文件（含非必需条目）也都需要通过核验。
- 来源清单顶层为 `schema_version: 1` 和 `files` 列表；每个条目须含 `path`、`size_bytes`、`sha256`、`source_location`、`copied_at`、`source_sha256_before_copy`、`source_sha256_after_copy`，可附加说明字段。大小为非负整数，摘要为 64 位十六进制（大小写等价），时间为带时区的 ISO 8601；拒绝重复 JSON 键、非法结构、缺证据字段及重复路径/别名。
- 每文件通过条件为实际字节数等于声明大小，且当前副本摘要、清单副本摘要、复制前源摘要、复制后源摘要四者相同。复制前后摘要和复制时间必须据实记录；`source_location` 只作为追溯文本，本次运行不打开外部源位置或旧项目。补写现在的摘要不能证明过去的复制过程。
- 报告保留必需列表、声明数、完成稳定全字节读取数 `checked_count`、通过数 `passed_count`，以及每文件的预期/实际大小与摘要、复制记录、通过状态和失败原因。缺清单、缺必需列表、清单漏项、缺文件、读失败、大小/摘要/复制证据不一致均失败；尽可能汇总有效清单中各文件的问题。
- 通过仅说明核验时当前副本与所给来源记录在字节层面一致，不证明内容正确、来源真实性或研究输入范围完整，也不是未来所有读取的锁定保证。字段、CRS、行数、几何、字典解析及清洗留到后续步骤；本步不改变 `--check-config`，不实现阶段调度。
- 真实输入清单、来源说明建议 `data/SOURCES.md`、运行报告和合成临时文件均不提交 Git；测试和实际核验继续在当前服务器按数据盘约定执行。

### 第 05 步真实输入来源确认

- 用户于 2026-10-10 验收第 05 步，并确认当前代码检出目录 `data/` 中，只有 `charging` 是加工过的数据，其余均为直接下载数据。现有 22 个文件中，`data/raw/charging/UK_OCM_stations_with_connector_summary.csv` 记为已加工的用户提供输入，其余 21 个文件按用户确认记为直接下载；不根据文件名另行推断加工状态。`data/raw/` 在本项目表示保留输入快照，不意味着 charging 未经上游加工。
- 本次准备来源是当前新项目的 `data/`，不是旧项目目录；现有文件保留原位、不改写。`data/raw/` 按原相对路径复制到数据盘项目，`data/cities/` 复制到数据盘项目的 `cities/`，保持已验收的路径接口。
- 第 05 步以全部 22 个现有文件（含配套说明和许可材料）作为本次完整快照的必需范围，在生成来源清单前独立固定。这不表示后续分析读取全部说明材料，也不替代后续研究输入范围和内容检查。
- 来源清单和 `data/SOURCES.md` 记录用户的来源确认、实际本地来源位置和本次新复制过程。复制前源、目标副本、复制后源分别进行全字节 SHA-256 测量；这些时间和摘要不充当原始下载或先前复制的历史证据。原始下载网址、时间、发布方摘要及 charging 加工步骤和上游输入谱系尚未提供，不猜测或补造。实际核验结果和证据路径见 `PROGRESS.md`。

### 第 06 步输入结构与全源质量审计

用户于 2026-10-10 明确“正式验收”第 06 步；本节接口与审计口径已获验收。验收范围为本步实现和必要验证，不包含后续字典连接、站点合并或分析。

用户已授权按方案实现本步及必要验证。以下是本步采用的接口与审计语义，实际运行结果和用户验收状态另见 `PROGRESS.md`。

- `poi_batches(relative_path, *, data_root, layer=None, batch_size=10000, fields=None, limit=None)` 使用 SQLite 只读连接和游标逐批读取原始属性及 GPKG 几何字节；保留源表的整数要素主键为 `source_fid`，按该键排序，不使用批内序号替换。多图层时必须明确选层；批次和局部上限必须为正整数且非布尔值。存在未纳入快照的 SQLite sidecar 时拒绝读取。
- 本次真实 POI 输入为 `data/raw/digimap/poi_6476197.gpkg`，图层 `Points of Interest 2026_09`，源主键 `id`，几何列 `geom`。字段映射为 `ref_no`、`pointx_class`、`feature_easting`、`feature_northing`、`pos_accuracy`。CRS 按 WKT 与 EPSG:27700 等价性及米制单位检查，不只比较名称或 ID。源声明几何类型为 `GEOMETRY`，实际类型须逐行审计。
- `inspect_inputs(poi_path, station_path, *, data_root, output_dir, layer=None, poi_batch_size=10000, station_batch_size=1000, sample_limit=None, poi_fields=None, station_fields=None, accuracy_labels=None, progress=None)` 写入新的 `outputs/` 子目录并返回 JSON 可序列化报告；已有输出目录拒绝覆盖。调用方先用第 05 步独立必需列表核验来源；函数另外检查所读两个输入的前后完整摘要与状态，防止把变动中的输入记为完成。
- `sample_limit` 非空时只检查有限记录，报告为 `scope=sample`；局部重复文件带 `sample_` 前缀，即使样本上限大于源行数，也不发布完整重复清单。全源模式以源表 `COUNT(*)`、扫描行数、唯一源 FID 数及频数守恒对账。CSV 数量按逻辑记录核对，含引号内换行不增加记录数。
- 每行保留原始值与类型。`ref_no` 的 NULL、空串及非有限数记为缺失，纯空白另记异常；字符串 `nan`/`NULL` 不猜作缺失。非缺失键以类型与原值精确比较，不修剪、补零或自动合并整数/文本值。跨全部批次的磁盘索引累计所有 FID，重复键清单及明细包含首次出现的行；分别报告重复键数、涉及行数、多余行数。本次真实 `ref_no` 声明为 INTEGER；若后续使用不同类型的源，须沿用或显式修订这项口径。
- 分类频数以全部原始源行为分母，含重复、无效几何、缺失分类及设施码 `10540832`，不代表清洗或 PUA 筛选后的分析计数。严格沿用第 02 步转换与八位 ASCII 数字规则；合法码取前 2/4/8 位，缺失/异常归 `UNKNOWN` 并保留各自格式状态。三层分别加总到源行数，官方字典状态为 `not_checked`，不以空字典将所有合法码标为未知。
- 几何分别统计 NULL、空、无效、非点、解码失败、非有限二维点坐标及正常 Point；不修复几何或删除源行。对合法 GPKG 头检查版本、包络长度、空标志和 CRS，逐行保存质量原因；异常几何仍计入 ID 与分类审计。
- 有效非空 Point 的二维几何坐标与有限属性坐标可比较时，计算 `dx=x_geometry-feature_easting`、`dy=y_geometry-feature_northing`、`delta_m=hypot(dx,dy)`。保存每条可比较记录、比较分母、零/非零数、均值、最大值及最大值对应 FID；不可比较留空并记录原因。任何非零差异作审计，不设置自动排除阈值，不替换几何坐标。
- 精度频数保留全部原始值。新项目 `POSITIONAL_ACCURACY_LOOKUP.txt` 实测列出 1=地址或位置、2=邻近地址或位置、3=地址/位置内的道路、4=地理邻域；调用方显式传入核实后的描述，缺失与未列出的代码分别审计。此表不提供米级误差数值，不据此推断精度阈值或自动排除任何档位。
- 站点输入为 `data/raw/charging/UK_OCM_stations_with_connector_summary.csv`，关键字段 `StationID`、`Latitude`、`Longitude`。保留 ID 字符串及原值，按 CSV 逻辑记录序号追溯；检查缺失、坐标解析/有限性、经纬度范围、重复 ID、同 ID 多位置和同坐标不同 ID。只记录精确坐标相等的记录组，本步不执行合并；实际合并仍须遵循用户已确认的站点实体口径。
- 报告 `status=completed` 只表示所声明范围的审计完成；`full_source_complete`、`duplicate_list_complete` 仅在全源对账通过时为真，质量问题另设标志。失败保存部分报告并抛出携带 `.report` 的 `InputInspectionError`；后续使用重复清单必须先检查这两个完成标志及输入摘要。
- 产物包括 `input_inspection.json`、有限样本 JSONL、原始分类与三层频数、精度频数、全源重复键/明细、POI 与站点问题明细、逐点坐标差异及磁盘审计索引。索引、日志和全部产物留在数据盘运行目录。点集外接矩形仅为极值范围，不作为覆盖边界。
- 本步不进行官方字典连接、真实站点合并、POI 清洗、PUA 判断、坐标转换、覆盖计算或匹配；不新增完整阶段 CLI。`--check-config` 仍只检查配置，数据根写法按上述用户补充统一。

### 第 07 步官方字典与分类标签

用户于 2026-10-10 明确“正式验收”第 07 步；本节接口、字典连接与标签口径已获验收。验收限于本步实现和必要验证，未扩展到后续筛选、坐标转换或空间分析。

用户已授权按第 07 步方案实现。本节固定本步采用的接口和规则，测试与真实结果另见 `PROGRESS.md`；实现和测试通过不表示用户已验收。

- 官方输入为数据根 `data/raw/digimap/docs/` 下的 `POI_GROUPS.txt`、`POI_CATEGORIES.txt`、`POI_CLASSIFICATIONS.txt`。实测格式为 UTF-8（读取支持 BOM）、竖线分隔、CSV 双引号规则。编码与表头不猜测，错误明确失败。
- `native_dictionary(group_path, category_path, class_path, *, data_root)` 返回 `NativeDictionary`，其 `groups / categories / classes` 分别以完整 2/4/8 位字符串为键；`classes` 可直接传给已验收的 `classify()`。原始 `ID`、局部编号、父级外键、源路径与逻辑记录号保留。三个表的 ID 分别要求唯一；不把 ID 当分类码。
- group 表字段为 `ID / Group Number / Group Description`；category 表另有 `Category Number / Category Description / Group Number Foreign Key`；class 表为 `ID / Class Number / Classification Description / Category Number Foreign Key`。group/category 编号为两位 ASCII 数字，class 局部编号为四位，外键为两位；原始编号已经定宽，读取时不修剪、补零或改写。
- 类别编号 `Category Number` 是 class 文件的父级引用目标，必须全表唯一；不能在不同 group 下重复该编号后猜测 class 的父级。class 局部编号可以在不同 category 下区分，但同一父级下的完整代码必须唯一。沿外键生成 `category_code=Group Number+Category Number`、`class_code=category_code+Class Number`；空键、重复键、重复完整代码、缺失父级和错误字段均失败。
- `merge_classification_frequencies(dictionary, frequencies)` 对官方完整 class 代码和全部源代码做并集合并；状态为 `matched / dictionary_only / source_only`。字典独有项源频数为 0；源独有合法码保留完整码和前缀。缺失/异常码以 `UNKNOWN` 汇总，官方匹配记 `not_applicable`，原始值/类型/格式原因仍在独立审计表中；本步不决定第 15 步 UNKNOWN 的展开域。
- `load_inspection_frequencies()` 要求第 06 步报告 `scope=full_source`、状态及完整性标志通过；核对三份审计产物与前一轮 `verification.json` 中的大小/SHA-256，当前 POI 完整指纹与前一轮读取前后指纹一致，并核对原始格式及三层频数守恒。sample、failed、产物变化或源不一致不能继续使用。
- `source_name_frequencies()` 全源只读分批读取 `pointx_class / groupname / categoryname / classname`，不读取单个 POI 的 `name` 作为类别名称，不解码几何。磁盘 SQLite 索引保留 `(level, complete_code, raw_name)` 频数，NULL 与字符串原值分别保存。名称必须为文本或 NULL，非文本类型明确失败；代码仍沿用既有严格分类规则。
- `category_labels(dictionary, level_counts, name_frequencies)` 以 `(level, code)` 为键。名称的 NULL、空串、纯空白视为标签缺失，但原值保留；非空名称不去空格、不改拼写/大小写。优先选择频数最多的非缺失源名称，并列按 Python Unicode 字符串升序选择；源名称缺失时用非缺失官方名称，两者均缺失时显示完整代码并标记 `code_fallback`。输出全部候选频数、同码多名称冲突标志及源/官方名称差异标志，不按名称合并代码。
- group/category/class 的官方匹配分别判断；合法未知 class 可有已知 group/category。字典独有的父级也保留。每层源频数和名称频数分别对账，不把三层相加当作 POI 数。
- `inspect_dictionary(..., data_root, inspection_dir, output_dir, layer, required_paths, batch_size=10000)` 是本步显式调用的读写接口。调用方独立确定必需输入范围，须包括三份字典和 POI；函数读取前后用 `verify_sources()` 核验声明副本，核对输入摘要/状态与前一轮审计产物。输出目录只能是 `outputs/` 内尚不存在的子目录，拒绝覆盖与越界。失败保存报告并抛出带 `.report` 的 `DictionaryValidationError`。
- 产物为官方三层字典、class 并集、逐原始码匹配审计、源名称频数、三层标签、磁盘名称索引及 `dictionary_validation.json`。消费产物须先检查 `status=completed`、`validation_complete=true` 和输入/产物摘要；代码指纹、实际命令及独立复核记录在该运行的验证证据中。全部产物与测试临时文件位于数据盘新的运行目录，不提交 Git。
- 频数是全部原始源记录频数，设施码也保留；字典连接或标签生成不改变 `classify()` 的设施开关和最终资格语义。本步不做站点合并、POI 筛选、PUA 判断、坐标转换、空间匹配或后续完整调度。

## 第 08 步固定坐标转换接口与验证口径

用户于 2026-10-10 授权按方案实现第 08 步，并采纳分别检查投影环节与完整转换的建议；随后明确“正式验收”。本节接口、固定 pipeline、坐标假设及验证口径已获用户验收；验收仅限第 08 步，不扩展到后续覆盖或站点准备。

- `transform_coordinates(longitude, latitude, coordinate_operation)` 接收两列等长、已解析的实数序列，顺序为 longitude、latitude，单位度，返回两个 Python 列表 `(easting, northing)`，单位米。支持迭代器和空批次；保留数量、顺序及重复坐标，不四舍五入、不生成站点键或进行筛选。
- 输入按 WGS84 解释是现有站点来源约定，CSV 没有嵌入坐标基准、历元或高度证据。缺少高度时内部按 WGS84 椭球高 `0 m` 计算，不把该假设当作实测高程或 ODN 高程。
- 拒绝非序列列参数、文本/布尔/非实数值、非有限值、longitude 超出 `[-180,180]`、latitude 超出 `[-90,90]` 和列长度不等。转换使用 `errcheck=True`；PROJ 失败、非有限或不完整输出均使整个批次明确失败，不删行、不自动修复或交换轴。合法角度范围不能检测所有轴交换，也不能证明 GB/PUA 资格。原始 CSV 解析和异常审计沿用第 06 步；后续站点准备须据审计结果显式处理异常，不把坏行传入转换。
- `validate_coordinate_operation()` 只允许下列固定 token 序列，可规范化空白；其他参数、操作、CRS 名称、网格或附加步骤均拒绝。`run.py` 的配置 schema 新增必需 `coordinate_operation` 并返回规范字符串；配置检查不初始化 PROJ 或执行转换。

完整固定 pipeline：

```text
+proj=pipeline
+step +proj=unitconvert +xy_in=deg +xy_out=rad
+step +proj=push +v_3
+step +proj=cart +ellps=WGS84
+step +inv +proj=helmert +x=446.448 +y=-125.157 +z=542.06 +rx=0.1502 +ry=0.247 +rz=0.8421 +s=-20.4894 +convention=position_vector
+step +inv +proj=cart +ellps=airy
+step +proj=pop +v_3
+step +proj=tmerc +lat_0=49 +lon_0=-2 +k=0.9996012717 +x_0=400000 +y_0=-100000 +ellps=airy
```

- 单位和方向：度转弧度，WGS84 转地心 XYZ（米），执行逆 Helmert，转 Airy 经纬度，再投影到 BNG x/y（米）。平移为米、旋转为角秒、尺度为 ppm；旧参考参数是 OSGB36→WGS84 方向，`+inv` 使实际执行方向为 WGS84→OSGB36，不能省略。push/pop 保存/恢复第三维；二维输出丢弃了转换后的高度，逆变换只要求近似往返。
- 不使用自动 CRS operation 选择。运行所需 pipeline 没有网格步骤或在线依赖；必要验证在新进程 `PROJ_NETWORK=OFF`、数据目录仅含 `proj.db`、用户数据目录为空的环境执行。标准 PROJ 数据库和软件仍属于环境依赖；无网格不等于不依赖软件版本。
- 两类绝对参考分别验证：官方附录 C 的 OSGB36 点 `(1°43′04.5177″ E,52°39′27.2531″ N)`，即 `(1.7179215833333334,52.65757030555555)`，验证投影环节到 `(651409.903,313177.270)`；路线原 WGS84 点 `(1.71605199,52.65797860)` 仍用完整 pipeline 验证同一目标，两轴各容许 1 m。上一轮把路线小数值说成官方 DMS 的舍入形式是错误的，此处明确更正；不更改站点基准，不删除生产七参数步骤，不扩大原容差。
- 完整转换另对照四个由独立 OS 公式生成并冻结的参考值，公式仅使用 Python 数学函数，不调用生产函数、PROJ 或同一 pipeline 的逆变换生成预期值。独立公式通过官方附录 C 投影和附录 D 地心坐标示例核对。OS 的正向小角度七参数公式与本 pipeline 对反向参数执行逆操作有毫米级差别，使用 1 m 每轴容差覆盖这种数值差别；不将其算作真实观测误差。
- 正向后逆向的绝对角度容差为每轴 `2e-8°`，不叠加相对容差。逆变换通过仅说明操作近似自洽，必须结合独立绝对参考检查；不能由往返通过单独断言位置正确。
- OS 对该近似七参数方法说明约 3.5 m（95%）的转换限制，适用于 Britain，不能保证其他区域准确。这是方法限制，不是本批站点实测误差；源坐标定位误差、WGS84 实现/历元未知、默认高度和数值舍入另有影响。站点所属 GB/PUA 与分析资格在后续步骤判定；本步合法角度转换成功不构成资格证据。
- 坐标验证的 1 m 容差不进入半径筛选。最终条件仍严格为 `distance_m <= 500` 和 `distance_m <= 1000`，微小越界点排除。
- 本步必要真实验证仅转换数据盘站点 CSV 的前 10 条逻辑记录，保留记录号、StationID、原始经纬度与两种计算的差异。未执行全量站点转换、合并、PUA、覆盖或匹配；证据和日志全部在数据盘新 `outputs/<run-id>/` 中，结果见 `PROGRESS.md`。

参考：[OS 官方坐标指南 V3.6，第 6.6 节及附录 C、D](https://www.ordnancesurvey.co.uk/documents/resources/guide-coordinate-systems-great-britain.pdf)；[PROJ Helmert 方向、约定和参数单位](https://proj.org/en/stable/operations/transformations/helmert.html)；[PROJ pipeline 的单位与逆操作约束](https://proj.org/en/stable/operations/pipeline.html)。这些资料用于设计和独立验证，不在运行时访问。

## 第 09 步范围修订：PUA–LAD 地理对应、城市研究范围构建与充电站筛选

用户于 2026-10-11 先要求修订文档，随后明确授权实现第 09 步及必要验证，并在实现与自动验证通过后回复“验收通过”。本节接口、研究范围及筛选口径已获用户正式验收；测试、真实运行与验收归档见 `PROGRESS.md`。该次验收仅限第 09 步；第 10 步另按用户指令实现，并于 2026-10-11 获用户单独正式验收。

### 输入与来源

- 实际读取位置沿用数据盘项目 `~/Work/analysis_Digimap_POI_v2/`：PUA 定义位于 `cities/`，LAD 行政边界位于 `data/raw/geography/`，站点为 `data/raw/charging/UK_OCM_stations_with_connector_summary.csv`；先按第 05 步核验独立必需文件范围、来源清单和字节摘要。运行时不读取代码检出目录中的原始准备文件或旧项目数据。
- 定义主表为 `cities/PUA_LA_mapping_Centre_for_Cities_2022.xlsx` 的 `GB_mapping`，`pua_name/local_authority` 定义成员关系，保留工作表、源行及全部原字段。它没有 LAD 代码；`cities/PUA_LA_mapping_GB.csv` 用于逐字段、含重复次数的独立文件对账。`PUA_summary` 独立展开目标名称及预期成员数。`cities/Data tool 2026 dataset.xlsx` 是指标工作簿，`Population.City` 只辅助核对名称，不作为 LAD 映射来源。
- 用户最新回复明确选择**全部 GB 62 个 PUA，不含 Belfast**，覆盖此前“指定子集”的回复。目标清单作为独立研究输入保存，不能从成功匹配或有站点的城市反推；将来使用子集须显式传入新的清单。
- LAD 主表为 `data/raw/geography/LAD_MAY_2025_UK_BGC_V2.gpkg` 的同名图层，代码/名称为 `LAD25CD/LAD25NM`，源键为 `FID`、几何为 `SHAPE`。实查 361 个有效非空 2D MultiPolygon，WKT 等价 EPSG:27700、轴单位米；配套已登记 `lad25_bgc_item.json` 确认 May 2025、BGC 20 m generalised、按平均高潮线裁切，运行时不重投影或修复这些边界。
- 站点原始 ID、经纬度与 CSV 逻辑记录号保留。第 06 步全源审计和第 08 步坐标接口可复用；复用审计前先确认输入与产物摘要、完整状态对应本次文件。charging 仍为用户确认的加工数据，研究范围筛选不补造其上游谱系。

### 地理对应、研究范围与筛选

1. 核实 PUA 标识、名称、成员 LAD、定义版本，以及 LAD 几何表的代码、名称、边界版本和 CRS。优先按稳定 LAD 代码连接，保留原值和源记录位置；名称用于核对，不能未经确认做模糊匹配或自动修复代码。若源定义仅有名称，先建立有依据、可审阅的名称到代码对应。
2. 审计缺失键、重复关系、代码未匹配、同码名称差异及一个 LAD 对应多个 PUA 的情况。PUA 定义和 LAD 边界跨年份时逐项说明兼容性；行政区合并、拆分或更名须有可追溯的对应依据，不能只因名称相近或年份较新就替换。目标 PUA 有未解决的成员缺失或冲突时，不得按残缺边界宣告研究范围构建成功。
3. 核实定义确实采用完整 LAD 成员后，按 PUA 合并已核实成员的 Polygon/MultiPolygon：`G_pua = union(G_lad)`，再对显式目标集合 `T` 构建 `G_study = union(G_pua for pua in T)`。内部 LAD 分界消除，外部轮廓、孔洞和多部件保留；不使用凸包、包围矩形、填孔或额外缓冲替代定义。空/无效几何及未知 CRS 明确报错，不静默修复。如果定义只包含某 LAD 的一部分，先补充对应依据，不能直接使用整块 LAD。
4. 将几何与站点统一到已核实的米制坐标系，站点转换复用第 08 步固定离线 pipeline。LAD 源 CRS 未核实前不直接指定 EPSG:27700；若确需边界坐标转换，在实施方案中明确操作和验证。缺失、无效或转换失败的站点坐标保留审计，范围状态为无法判定，不标成范围外，也不自动入选。
5. 按本次“按方案实现”采用 `covers`：外边界和孔洞边界纳入，孔洞内部排除，微小越界排除，无容差或缓冲。保存全部 LAD/PUA 命中及目标 PUA 命中，城市唯一归属另记；不按文件顺序挑选城市。每行还将 PUA union 命中与各成员 LAD 命中对照，避免 union/index 结果不一致；同一源行不会因多重归属重复写入候选表。
6. 使用 `in_target_pua=True/False/None` 表达范围内、范围外、无法判定。CSV 中分别写 `True`、`False`、空字段；缺失、无效、超出经纬度范围、转换失败及字段数异常分别审计。只有明确为 True 的行进入地理候选表；缺失或冲突 ID 不在本步改变地理判断。配置/PROJ 环境故障整体失败，只有确认运行环境正常后的单点转换失败才记为无法判定。此标志不等于最终 `selected`、`query_eligible` 或覆盖完整；实体合并与 ID 冲突处理留在第 10 步。

由 LAD union 构建的是与所选 PUA–LAD 定义及边界版本对应的研究范围，不据此声称等同于更精细的连续建成区轮廓。generalised LAD 边界、跨版本衔接和站点七参数转换都有位置或语义限制，报告须保留这些限制；本步不改 500/1000 m 的距离判据。

### 跨版本对应与限制

154 条 GB 成员关系均有明确对应，使用 154 个不同 LAD。名称不做 trim、模糊匹配或自动纠正；运行输入 `crosswalk` 保存源名称、当前代码/名称、历史代码和依据。名称相同的行也经 ONS 历史代码及跨版本表复核。本轮 8 项名称对应为：

|源名称|LAD25 名称|LAD25 代码|
|---|---|---|
|Aberdeen|Aberdeen City|S12000033|
|Bournemouth Christchurch and Poole|Bournemouth, Christchurch and Poole|E06000058|
|City of Bristol|Bristol, City of|E06000023|
|Dundee|Dundee City|S12000042|
|Edinburgh|City of Edinburgh|S12000036|
|Glasgow|Glasgow City|S12000049|
|Kingston upon Hull|Kingston upon Hull, City of|E06000010|
|Castlepoint|Castle Point|E07000069|

另有 Barnsley `E08000016→E08000038`、Sheffield `E08000019→E08000039` 两项代码变化，由 ONS LAD24→LAD25 官方对应表确认。采用的是 **2022 PUA 成员身份与完整 May-2025 LAD 几何**，不声称重建 2022 历史边界或证明两年几何相同。原 PUA PDF 将 Bournemouth 成员明确列为 Dorset 和 Bournemouth Christchurch and Poole，故包含完整 Dorset，不凭城市名称缩小它。

独立设计/核对依据为 [CfC 原始 PUA PDF](https://www.centreforcities.org/wp-content/uploads/2022/08/2022-PUA-Table.pdf)、[ONS LAD22 名称代码](https://www.arcgis.com/home/item.html?id=42af123c4663466496dafb4c8fcb0c82)、[ONS LAD24→LAD25 对应表](https://www.arcgis.com/home/item.html?id=40d74f80a6cb49f68fdd9fa53efbb1dd)。下载 URL、摘要和复核副本保存在本次数据盘输出中，作为已审阅 crosswalk 的依据；正式运行仍只读已有 manifest 登记输入，不运行时联网或以网络文件替换已登记来源。原始文件的用户来源说明仍保留，不据工作簿内部笔记补造下载/转录历史。

### 接口、产物与行含义

`src/data.py` 提供只读 `read_pua_definitions()`、`read_lad_geometries()`、`station_rows()` 和 `load_station_inspection()`；后者要求已完成全源审计、当前站点摘要一致，并验证上游报告/问题表与验证文件中的大小和摘要。`src/geography.py` 分离 `resolve_pua_lad()`、`build_pua_scope()`、`select_station_rows()` 与文件驱动 `prepare_pua_scope()`；XLSX 依赖固定为 `openpyxl==3.1.5`。

驱动显式接收目标、主表/工作表、图层/字段、crosswalk、整 LAD 与版本复核说明、上游审计目录、固定转换和独立必需文件列表。必须在新的 `outputs/` 子目录运行；来源或上游改变、缺目标/成员、身份冲突、无效几何及验证不一致均产生失败报告。完全重复的关系保留审计、union 只使用一次；本实现遇到任何未解决的输入定义关系即失败，防止伪造完整的全部 PUA 归属列表。

|产物|含义|
|---|---|
|`scope_definition.json`|展开后的目标清单、版本、crosswalk 与依据、字段、谓词、CRS、pipeline、必需来源和输入摘要|
|`pua_lad_mapping.csv`、`pua_lad_issues.csv`|每条成员关系、原字段、连接代码/FID、来源行、处理状态及对应依据；重复/冲突/名称差异/代码变化可追溯|
|`pua_geometries.csv`、`study_area.wkb`|每个 PUA 的成员与十六进制 WKB；联合边界为二进制 WKB。CRS 单独存于表及范围定义，WKB 本身不带 CRS；大型 WKB 的 CSV 回读临时按实际长度提高字段上限并恢复原设置|
|`station_pua_audit.csv`|每条站点 CSV 逻辑记录一行；原始 ID/坐标、转换结果、LAD/PUA 候选、地理三态及原因，覆盖所有源行|
|`stations_in_target_pua.csv`|只包含明确通过地理筛选的源记录，每个源行一次；尚非按实体合并的正式站点表|
|`pua_scope_report.json`|输入与目标名单、映射/几何完整性、保留/排除/无法判定数、按 PUA 的归属统计和验证状态；多重归属单列，不把各 PUA 计数简单相加当作全局站点数|

全部产物保存到数据盘新的 `outputs/<run-id>/`，不覆盖源文件或先前审计。站点 CSV 的多值及原始字段列为 JSON，`raw_fields` 与 `raw_values` 完整保留输入（包括多余字段），`prior_station_issues` 保存全源问题。本步保留同坐标各源行，使第 10 步不会因只看到地理候选子集而漏掉 ID 冲突。失败报告附近的部分文件不可作为成功产物消费；`status=completed` 仅表示本步运行/自动验证通过，用户验收另记。

### 验证要求与相邻步骤交接

- 先用手工可算的相邻矩形、带孔/多部件几何以及映射缺失/重复/跨版本案例，验证 PUA 成员连接、union、内部 LAD 分界、外部与边界点、多重归属及无效坐标。边界案例的具体预期按最终确认的空间谓词确定。
- 真实检查应覆盖目标名单的全部 PUA/LAD 连接和站点源记录；有限展示与全量报告分开。验收要求目标成员没有未解释的缺失，几何与定义对账，且 `源站点行数 = 范围内行数 + 范围外行数 + 无法判定行数`，候选表行数等于范围内行数，源行键唯一。没有站点的有效目标 PUA 保留为零，不从目标名单中删除。
- 核验前后输入完整性；城市数、LAD 数、站点纳入数与排除原因须真实测量，实际值见进度及本轮报告。第 06 步历史站点计数只在当前指纹一致时用于额外对账，不代替本步独立重数与筛选。
- 原第 09 步的 GB/NI 覆盖几何、距 GB 边界距离和订单余量保留为第 10 步职责，相关内部/外部/边界/孔洞手算检查一并移交；第 09 步不因扩展研究范围任务而取消覆盖约束。第 10 步先按全源审计处理站点实体和冲突，再为目标 PUA 内实体确定最终状态。GB 代理仍使用全部 E/S/W LAD，不能只用目标 PUA 的 LAD。
- 第 11/13 步的交接按 2026-10-11 第 10 步方案轮的新指令修订：第 09 步目标研究范围仅用于筛选 EVCS；POI 按第 10 步的数据覆盖定义和后续业务/清洗规则准备，再按与可查询站点的距离筛选邻域。POI 无需与站点同属 LAD/PUA，也无需位于任一目标 PUA 内。
- PUA 研究范围不是数据覆盖边界。靠近 PUA 边缘时，站点 buffer 保持完整的 500/1000 m 累计圆，POI 可来自圆内有数据覆盖的 PUA 外区域；完整覆盖仅依 GB/订单代理判断，不新增 PUA 边界距离或改变组成分母。第 09 步已验收的站点筛选结果不受这项 POI 范围修订影响。

目标、主辅来源、连接字段、版本使用规则、边界谓词及多重归属规则已按上文确定。仍有研究解释限制：20 m generalised LAD 和近似七参数转换不能证明边界附近站点的真实位置；本步不做定位误差敏感性分析，也不把几何命中等同于数据覆盖完整。

## 第 10 步站点实体、查询资格与覆盖（用户已正式验收）

2026-10-11，用户先要求方案并修订 POI 空间范围，随后明确“按刚才的方案实现第 10 步，只完成本步及必要验证”。据此采用方案中的 ID 规范化及异常组整体处理建议，并实现以下接口。219 项测试、真实样本/全源准备和 119 项独立核验通过后，用户明确回复“验收通过”，本节接口及口径已获正式验收；实际数量、证据及提交标识见 `PROGRESS.md`。验收归档仅核对代码与产物摘要，没有重跑测试或真实分析，也不授权第 11 步。

- 目标 EVCS 沿用第 09 步全部 GB 62 个 PUA、covers 及全部归属记录。全源站点用于身份检查；范围外源行仍参与重复 ID 和位置冲突判断。按有效坐标合并后保存源行映射，不能以地理筛选消除全源冲突。
- ID 仅去首尾空白并保留原值、前导零，不作数字等价转换；规范化后重新进行全源重复/冲突检查，文本 `nan` 不猜作空值。按解析后的有效 WGS84 坐标精确相等合并，正负零相同，不舍入或作近邻合并；无法定位的记录逐行保留且不可查询。
- 沿用路线的缺失/重复 ID 不可查询规则，包括同 ID 同位置重复。同坐标且不同、各自唯一的 ID 合并为一个实体，该合并本身不阻止查询；含缺失、重复或位置冲突 ID 的整个坐标组不可查询。同 ID 跨位置不串联合并；同坐标组的投影位置和上游归属必须一致，否则失败。
- 分别输出 `selected`、`query_eligible` 和按半径的 `coverage_complete_proxy`。全量模式的 selected 表示实体在目标 PUA 内；可查询还需坐标与身份合格、GB 代理 covers 且在已核实订单范围内。NI、订单外及无效坐标/身份不可查询；固定种子样本仅从已选择且主状态为 `within_GB_proxy` 的实体抽取，先按稳定键排序。样本仍检查完整源身份，保留全部实体与审计；未抽中者 selected/query_eligible 为 False，原 `in_target_pua` 保留，原因记录 `not_sampled`。
- `G_GB = union(全部 E/S/W LAD)`，NI 的 N 前缀 LAD 单独合并；保留孔洞、多部件并验证 CRS/几何，不只使用目标 PUA 的 154 个 LAD。以 `C = G_GB ∩ R_order` 作为数据覆盖代理，同时保存 GB/NI 几何、订单定义、CRS、来源和限制。若订单材料不支持唯一且有依据的范围，不能宣告真实覆盖准备成功；多块范围不能擅自合并成扩大覆盖的外接矩形。当前读取接口仅支持一个产品目录、一个交付 chunk、两个相同的目录/文件 Extent；不同结构须显式扩展并验证。
- 订单为已核实 BNG 米制矩形时，`order_clearance_m = min(x-minx, maxx-x, y-miny, maxy-y)`。GB 距离使用点到 union 完整边界的最短距离，含孔洞环；外部点的正距离不构成内部资格。

```text
coverage_complete_proxy(s, r) = query_eligible(s)
                               and G_GB.covers(s)
                               and gb_boundary_distance_m(s) >= r
                               and order_clearance_m(s) >= r
r in {500, 1000}
```

相切满足 `>=`，1000 m 完整必须蕴含 500 m 完整。POI 的后续空间条件是 `C.covers(p)` 且 `distance_m(s,p) <= r`，再叠加业务筛选和清洗；没有 POI 的 PUA 资格条件。圆形 buffer 用于表达欧氏距离范围，最终精确判定不用近似多边形代替距离，也不加入转换误差容差。

第 10 步仅生成站点、源行审计、覆盖代理及站点×半径基础表，不填 POI 数量。覆盖不完整的可查询站点在后续保留观测值，完整覆盖统计留空；不可查询为缺失，可查询零匹配才为 0。合成案例已验证跨 PUA 的点仍在覆盖/半径内、圆跨出 PUA 不降低覆盖，以及覆盖外点即使在 buffer 内也不具备覆盖资格；真实 POI 清洗与匹配留待第 11—14 步。

### 已实现接口与输出

- `coverage_geometries(lads, *, crs, order_bounds)` 校验唯一 E/S/W/N LAD 代码、有效 2D Polygon/MultiPolygon 和 EPSG:27700 米制 CRS，输出 GB/NI/order/data_coverage 几何及成员列表。无 N 源行时 NI 显式为空；GB 不能为空。数据覆盖交集可为空，但不能把退化线/点当成覆盖面。
- `boundary_distances(xy, geometry)` 使用完整 union 边界的直线段 STRtree 精确求最近距离，包含孔洞环，不使用近似圆多边形。None 位置对应 None 距离；外部点的正距离不代替 covers 资格。
- `prepare_stations(rows, coverage, *, target_puas, sample_size=None, random_seed=None)` 是全源内存接口；`rows` 来自严格读取的第 09 步审计。稳定键为 `loc_` 加规范化 WGS84 浮点十六进制串的 SHA-256；未解析记录为 `unresolved_<12位源行号>`。键不依赖 ID、输入遍历顺序或抽样。所有源行/原 ID 对齐保存，源行还列出相同规范 ID 的全部逻辑记录号。
- 主状态优先级为位置异常、缺失 ID、同 ID 位置冲突、重复 ID、目标范围未知/范围外、NI、GB 外、订单外、GB 边界、within_GB_proxy；`spatial_status` 独立保存空间状态，`query_blockers`/`audit_reasons` 保留全部原因，布尔资格由条件独立计算。样本未抽中实体另标 not_sampled。目标 PUA 与 GB 源不一致、导致入选位置在 GB 外时明确失败，不扩张研究范围。
- `station_radius_base(stations)` 仅为 selected 实体生成 500/1000 m 各一行，含覆盖标志、距离/余量与全部不完整原因，无 POI 数量列。
- `load_station_scope()` 核对第 09 步通过证据、报告、必要产物大小/摘要、源指纹、pipeline、目标/CRS；逐行与当前完整 CSV 原字段对照，复用固定 pipeline 重投影并复核研究范围，候选表须恰等于全审计 True 行。再与第 06 步全源问题对账，拒绝失败/部分上游产物。
- `read_order_extent()` 严格核对交付目录/文件、产品、格式、两个 Extent、矩形顺序及面积，读取当前 POI GPKG 元数据验证 CRS/单位；订单 CRS 的跨材料依据必须作为 `review` 保存，不从 POI 极值推断。
- `prepare_station_files(station_path, lad_path, order_path, poi_path, *, data_root, output_dir, scope_dir, inspection_dir, coordinate_operation, required_paths, order_review, poi_layer, sample_size=None, random_seed=None)` 是本步显式文件驱动；只接受数据根内新 `outputs/` 目录，不覆盖已有目录。先后核验来源清单全部副本与上游证据，失败保存 `station_preparation_report.json` 并抛出 `StationPreparationError`；附近部分文件不能消费。

产物为 `stations.parquet`、`station_audit.csv`、`station_radius_base.parquet`、`gb_proxy.wkb`、`ni_proxy.wkb`、`data_coverage_proxy.wkb`、`coverage_definition.json` 和 `station_preparation_report.json`。实体表保留范围外/未解析记录供审计，基础表仅有 selected 实体；一个实体的多 PUA 归属不会扩张基础行数。Parquet 使用显式、含空值和列表的 schema，依赖固定为 `pyarrow==23.0.1`；列表子字段固定 `element` 以保持标准 Parquet 回读一致。CSV 保留 JSON 多值列、原始字段和逻辑行号；WKB 的 CRS 配套保存在覆盖定义。写出后逐项回读验证，报告保存源行/实体守恒、全部目标 PUA（含零值）、状态及覆盖数量。

### 本次真实订单与覆盖依据

`contents_order_3022550.txt` 的目录 `poi_6476197`、文件 `poi_6476197/poi_6476197.gpkg` 均给出 `0.0, 0.0 - 700000.0, 1300000.0`，产品 Points of Interest、September 2026、GeoPackage，面积 910000 平方公里。目录原文本身没有 CRS 标签；当前同名交付图层 `Points of Interest 2026_09` 的 GPKG CRS 为 EPSG:27700、单位米。`POI_TECHSPEC_v_4_3.pdf` 说明 GB 产品与 National Grid Easting/Northing、米制分辨率，`POI_PRODUCTINFO_v_4_3.pdf` 指定 Coverage is Great Britain；订单 citation 标记 Tiles: GB。共同据此采用上述 BNG 米制矩形，并将原文行号、文件摘要、元数据及跨材料解释保存，不声称 Extent 行单独声明了 CRS。

真实 LAD 为 350 个 E/S/W 和 11 个 N，分别 union；GB 与订单交集用于后续 POI 准备。LAD 为概化行政/海岸边界，七参数转换仍有位置误差，代理并不证明每个现实 POI 均被收录。本步未做 QGIS/实地位置复核、敏感性分析或真实 POI 邻域计数。

## 第 00 步的未决问题

以下问题不在第 00 步擅自决定，须在相关步骤前明确并记录：

1. 旧项目 `/home/mazhichuan/Projects/analysis_Digimap_POI` 仅作只读参考；旧 `config.yaml` 和源码不能直接照搬。第 04 步未填入未经验证的转换字符串；第 08 步已独立实现及验证固定 pipeline，详见上节，用户已正式验收。站点 WGS84 基准、历元和高度的原始证据仍未提供。
2. 第 09 步目标已明确为全部 GB 62 个 PUA，主表、字段、跨版本对应、CRS、covers 和全部归属保留规则见上节。第 10 步已实现 GB/NI 代理、边界距离及订单覆盖，通过自动验证并获用户正式验收；覆盖代理及来源限制继续适用。
3. 后续 POI 业务筛选的字段、类别清单和排除原因需要在 POI 清洗步骤前明确；在此之前不能声称所有有效点都会被计数。
4. `UNKNOWN` 是否在最终 group 汇总中作为展开代码，需要在接入真实字典和第 15 步统计前根据旧实现与用户决定固定。
5. 第 16 步的独立验距应以匹配使用的几何坐标为准，还是同时复现旧实现基于 `feature_easting/northing` 的检查，需要先讨论几何/属性坐标不一致时的取舍。
6. 服务器执行地点和数据盘输出目录已在第 02 步确认。第 05 步在用户确认来源后，已为当前新项目的 22 个文件建立数据盘独立副本、来源说明、来源清单和本次复制证据，并通过全字节完整性核验；先前缺副本与来源确认的状态保留为历史记录。原始下载元数据及 charging 加工谱系尚未提供；字段、版本、坐标系和内容正确性仍须在后续对应步骤核实，不能由哈希一致推出。

## 用户已确认的口径修订

- 同坐标不同 `StationID` 是数据错误，合并为一个分析站点，并在审计中保留所有源记录和 ID。
- 暂不加入城市等级。2026-10-11 第 10 步方案轮修订为：EVCS 限于目标 PUA 内；POI 可位于目标 PUA 外，按站点 buffer、数据覆盖区域及后续业务/清洗条件纳入，取代此前 POI 也限于目标 PUA 的要求。
- PUA 边界使用新项目 `cities/` 与 `data/raw/geography/` 的联合数据；旧项目 `/home/mazhichuan/Projects/analysis_Digimap_POI` 只读参考，不能照搬。
- 数据盘项目根固定为 `~/Work/analysis_Digimap_POI_v2/`，输出按 `outputs/<run-id>/` 分目录保存；第 00 步未创建真实数据目录，第 05 步在用户确认来源后准备独立副本。第 06 步用户补充个人工作目录为大写 `~/Work`；现有小写别名指向同处，物理输入位置不变。
- POI 需要在后续步骤筛选，不能把源数据中所有 POI 默认视为研究对象。
- 第 02 步保留原始分类值，另将非缺失值转换成字符串检查；不去空格、不补零、不修复编码，空值与格式异常分别审计。
- 第 02 步空值和格式异常均归为 `UNKNOWN`，`include_in_count=True`；该标志仅表示通过分类规则，最终仍须满足后续空间和清洗条件。
- 当前环境就是指定服务器，可以直接在新项目执行测试；运行输出仍放在数据盘的独立 `outputs/<run-id>/` 中。
- 第 03 步按已说明方案实现：样本 S3 使用 `(100, 500)`，原型采用显式 group 展开清单；真实输入准备、PUA 判断与覆盖几何计算仍在后续对应步骤验证。
- 第 04 步按已说明方案实现：代码根与数据根分别约束，使用 YAML 读取配置、固定 500/1000 m 和原生分类模式，检查设施开关、正整数批次、符号链接越界及输出重叠；本步只提供配置检查入口。
- 第 05 步按已说明方案实现：独立必需文件列表与来源清单对账，只读分块核验大小及当前/复制前后摘要；不通过修改真实清单或补造复制记录掩盖不一致，真实输入状态与合成测试结果分别报告。
- 第 05 步验收时，用户确认当前 `data/` 中 charging 为加工数据，其余为直接下载数据；该确认用于区分来源性质，哈希只证明本次复制与当前副本的字节一致性。
- 2026-10-11，第 09 步改为“PUA–LAD 地理对应、城市研究范围构建与充电站筛选”，先修订 Markdown，随后明确授权按方案实现本步及必要验证。最新回复选择全部 GB 62 个 PUA；采用 covers 和保留全部归属，范围纳入仍须经过后续实体清洗与覆盖判断。
- 2026-10-11，用户按方案授权第 10 步及必要验证；采用 ID 去首尾空白、精确同坐标合并、异常身份组整体不可查询和独立资格/覆盖状态。只有 EVCS 要求位于目标 PUA 内，后续 POI 按 buffer 与数据覆盖范围纳入。用户在本步自动验证通过后明确回复“验收通过”，第 10 步已正式验收，第 11 步及后续未开始。

这些未决问题不改变本轮已记录的基础口径，也不授权提前实现后续步骤。
