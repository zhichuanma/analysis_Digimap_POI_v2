# Digimap POI 分析重写

> **第 12 步当前状态（2026-10-12）：** 已实现可复用的 `build_neighbor_index()` 和 `exact_neighbors()`；cKDTree 生成候选，`math.hypot` 复算并严格按 `distance <= radius` 纳入。302 项测试（新增 23、回归 279）及 1,079 项独立核对通过，213 次构造查询无漏点/多点、最大距离差 0 m。用户已回复“验收通过”，现为**用户已正式验收**；第 13 步及后续未开始。运行证据、验收归档及 Git 提交标识见 PROGRESS。

> **第 11 步最新状态（2026-10-12）：** 默认接口已接入 [v3 规则](config/poi_research_rules_v3.csv) 及 [绑定配置](config/poi_research_rules_v3.yaml)，按官方 Group 汇总。602 类为 **585/17/0**；24 项最新人工决定全部执行，`02030047` include。[8 项空功能组](config/poi_research_rules_v3_pending_groups.csv) 均正常纳入，原分组仅供审计。279 项测试、真实前 100 条及 3,640 项独立核对通过，用户于 2026-10-12 明确回复“验收通过”，现为**用户已正式验收**。验收归档及 Git 提交标识见 PROGRESS；原规则与历史交付未覆盖，全源清洗未运行；第 12 步当前状态见上文。

> 下文 v2 的数量、理由和待确认记录为历史；如与上文冲突，以 v3 及最新 SPEC/PROGRESS 为准。

项目按 `REWRITE_GUIDE.md` 逐步推进，分析口径见 `SPEC.md`，实际验证与验收状态见 `PROGRESS.md`。

第 00—12 步已获用户验收。第 09 步构建全部 GB 62 个 PUA（不含 Belfast），154 条成员关系连接 154 个 LAD；27,006 条站点源记录中，地理纳入 18,740、范围外 8,266、无法判定 0。第 10 步于 2026-10-11 获用户正式验收：全源合并为 26,918 个实体，其中目标 PUA 内 18,685 个且均可查询；500/1000 m 完整覆盖分别为 16,889/15,060 个。219 项测试与 119 项独立核验通过，证据和验收归档见 `PROGRESS.md`；这些不是 POI 计数。第 11 步的 v3 和官方 Group 实现、限定验证已于 2026-10-12 获用户正式验收。第 12 步查询实现及必要验证已于同日获正式验收，第 13 步及后续未开始。

第 10 步消费第 09 步结果，使用全源审计处理站点实体合并与 ID 冲突，实现 GB/NI 代理、边界距离和订单范围覆盖。进入目标 PUA 只表示通过地理筛选，后续仍需满足可查询和覆盖条件。按用户已确认的范围，PUA 仅限制 EVCS；POI 可以来自目标 PUA 外，只要在站点 500/1000 m 累计 buffer 内、处于数据覆盖区域并通过后续筛选。第 11 步消费第 10 步覆盖定义，不按 PUA 裁剪 POI；后续候选准备应使用按最新口径调整并验证后的清洗资格。订单 BNG 矩形为 `(0,0,700000,1300000)`，跨材料 CRS 依据及覆盖限制见 `SPEC.md`。已验收的第 03 步匹配原型仍有旧 `in_pua` 条件，正式匹配接入时须迁移；第 10 步不调用该原型，没有运行真实 POI 清洗/匹配。

主要组成采用 POI 官方 Group：`01/02/03/04/05/06/07/09/10` 共 9 组，保留 Category/Class 原生码及名称。当前严格按 v3 的完整 Class 码执行纳入，不重新审查或以类别关键词推翻人工决定。具体活动、生产和服务设施原则上纳入，纯地理/工程标记和充电设施排除；不要求公众开放或有人值守。最新 24 项人工决定优先于旧理由，原始字段、顺序、前导零及空值均保留。

[v2 审查报告](config/poi_research_rules_v2_review.md) 的 7 个纳入边界和 19 项功能组问题为历史记录；7 个边界均已按 v3 决定解决。v3 的 8 个空功能组包括 WiFi 热点，现可通过 loader 和文件写入，`functional_group_pending` 仅作审计，不改为 review。表外未知、缺失或异常分类码沿用基础资格独立判断、运行时 review、暂不进入研究主表的接口；这与 v3 表内 review=0 分别统计。

当前分别保存 `base_eligible` 和 `analysis_include`（基础合格且 Class include）。基础清洗检查有效几何、有效且无全源冲突的原类型 ID、GB∩订单覆盖及设施开关。`10540832` 在两种开关状态下均不进入研究主表，同址的商店、PayPoint、ATM、酒店、停车场等仍按各自记录判定。功能组不再作为纳入门槛，计数摘要为 `native_group_counts`，官方九组保留零计数。

入口为 `src.data.load_poi_cleaning_context()`、`clean_poi_batch()` 和有限文件驱动 `prepare_poi_sample()`；独立分类模块为 `src/poi_research.py`。文件驱动只允许 `limit <= 100`，不接受全源调用，不执行邻域查询。context 要求完整且封存的第 06/07/09/10 步证据、当前原始来源指纹及规则绑定；参数完整列表见 SPEC。每批不修改输入，分别返回基础合格、研究主表、每源行一次的审计及摘要，不作坐标去重。

第 11 步实施证据在数据盘 `outputs/step11-v3-implementation-20261012T131717Z/`，真实样本产物在 `first100/`：基础及研究 Parquet、完整 JSONL 审计、Group 摘要、规则覆盖、CSV/YAML 原字节快照、验证报告和 manifest。Parquet schema 2 保留原 ID/分类的可逆 type/value、全部原字段、原始 GPKG 几何、计算 Point WKB/x/y、原生三层码及官方名称、研究决定和理由、两层资格、功能组与待分组标志；metadata 保存 EPSG:27700、规则版本和 `composition_mode=native_group`。发布前回读所有样本行和 schema，核对数量/组总数，再核验输入/规则；失败产物不能消费。

第 11 步实施时 279 项测试通过（本步 60、既有回归 219），3,640 项独立核对通过。12 条样本基础默认保留/排除 4/8，设施开启 5/7，研究主表两种开关均 3 条；602 类构造样本研究纳入 585，8 项空组全部纳入；真实前 100 条基础/研究均保留 100，全部为 Class `09490695`（NEW VEHICLES）、Group `09`（RETAIL），与预期一致。真实样本仅覆盖一个类别，不代表全源或城市组成；其他类别边界由构造样本验证。未运行全源清洗、跨城市偏差或 QGIS/实地复核；同机构/车站多入口跨 ID 归并尚需记录关联证据，未来匹配还须迁移第 03 步旧 POI `in_pua` 条件。用户已于 2026-10-12 正式验收，归档与提交标识见 PROGRESS。

旧 v1 证据 `outputs/step11-20261011T145744Z-ef431140/`（272 项测试、2,111 项独立核对）和 v2/v3 表格核验均原样保留，不计作本轮新运行结果。

第 10 步的显式入口为 `src.data.prepare_station_files()`，尚未接入完整 CLI。`src.spatial.prepare_stations()`/`station_radius_base()` 负责实体与资格，`src.geography.coverage_geometries()`/`boundary_distances()` 负责覆盖计算。文件入口要求完整源、已封存的第 06/09 步证据、固定 pipeline、独立必需来源列表及有出处的订单 review；先核对输入和上游，全部产物写入新的数据盘输出目录，失败文件不能继续消费。Parquet 依赖固定为 `pyarrow==23.0.1`。

成功目录包含 `stations.parquet`、逐源行 `station_audit.csv`、`station_radius_base.parquet`、GB/NI/数据覆盖 WKB、`coverage_definition.json` 和 `station_preparation_report.json`。WKB 必须与定义中的 CRS 一起读取。实体表保留范围外实体用于审计，基础表仅包含 selected 实体；字段含空值、列表和全部原因。完整覆盖标志以半径为单位，基础表没有 POI 数量。当前证据位于数据根 `outputs/step10-20261011T112022Z-dddcfc70/`，其中 `latest_full.json`、`latest_sample.json` 标明成功目录；复现使用保存的 request 和新的 output_dir，不能覆盖这些证据。

第 09 步通过 `src/geography.py` 的 `prepare_pua_scope()` 显式调用，尚未串入完整 CLI。它要求目标清单、映射工作表/字段、已审阅 crosswalk 与版本依据、完整第 06 步审计、固定坐标转换和独立必需来源列表。主表为 `GB_mapping`，CSV 核对定义，指标工作簿只辅助核对城市名称；PUA 使用完整 May-2025 LAD 的 union，不声称是 2022 历史边界或精细建成区轮廓。8 项名称差异、Barnsley/Sheffield 两项代码变化及依据详见 `SPEC.md`。

成功目录包含 `scope_definition.json`、映射与问题表、`pua_geometries.csv`、`study_area.wkb`、全源 `station_pua_audit.csv`、候选 `stations_in_target_pua.csv` 和报告。空间判据为 covers；多重命中保留全部候选归属，但每个源行只写一次。CSV 三态字段为 `True`/`False`/空字段，原始字段、坐标、归属列表及全源问题使用 JSON 列。WKB 的 CRS 单独保存为 EPSG:27700；读取大型十六进制 WKB 时注意 CSV 字段长度上限。

运行前检查来源和上游摘要，禁止覆盖既有目录；必须先确认报告 `status=completed` 且自动验证通过，失败目录中的部分文件不可消费。本步依赖新增 `openpyxl==3.1.5`，本次安装仅在数据盘运行目录内；其他环境须按 `requirements.txt` 安装依赖。重跑使用新的运行目录，显式调用示例：

```python
import json
from run import DATA_ROOT, load_config
from src.geography import prepare_pua_scope

# 已核实的目标与对应依据；改变研究选择时先更新并复核这一输入。
request = json.loads((DATA_ROOT / "outputs/<已复核run-id>/reviewed_request.json").read_text())
report = prepare_pua_scope(
    **request, data_root=DATA_ROOT, output_dir="outputs/<新的run-id>",
    coordinate_operation=load_config()["coordinate_operation"],
)
```

第 04 步提供路径和配置检查。服务器使用 Python 3.13.2、PyYAML 6.0.3；本步依赖见 `requirements.txt`。

代码根由 `run.py` 的实际位置确定。`--config` 只接受相对此根的路径，默认 `config.yaml`，与当前 shell 工作目录无关。数据根按 SPEC 固定为 `~/Work/analysis_Digimap_POI_v2/`，当前解析到 `/mnt/data/work/mazhichuan/analysis_Digimap_POI_v2/`，不接受 YAML 或命令行更换根目录。按用户说明，个人工作目录 `~/Work` 位于 18T 硬盘，实际路径为 `/mnt/data/work/<用户名>`，供存放数据集、项目和大文件，只有本人可访问；可通过 `cd ~/Work` 进入。历史小写 `~/work` 在本机指向同处，本步未移动数据、更改权限或验证其他账号的访问权限。

在代码目录检查默认配置：

```bash
python -B run.py --check-config --config config.yaml
```

在其他目录可使用同一 `run.py` 的绝对路径启动，`--config` 仍使用相对路径：

```bash
python -B /mnt/data/work/mazhichuan/.offloaded-from-system/20261007-full-scan/Projects/analysis_Digimap_POI_v2/run.py --check-config --config config.yaml
```

成功时输出 JSON、退出码 0；失败时在 stderr 指出字段和原因、退出码 2。`status=config_valid` 仅表示配置有效，`inputs_verified=false` 表示尚未验证真实输入；本命令不创建目录或调度分析。

配置中的数据路径均相对数据根，原始数据放在 `data/raw/`，来源清单为 `data/source_manifest.json`，PUA 来源为 `cities/` 与 `data/raw/geography/`。允许在指定目录内选择子目录，geography 必须仍在所选 raw 目录内；来源清单位置固定。输出必须在 `outputs/` 下且与全部输入区域分离；每次实际运行使用独立的 `outputs/<run-id>/`。

绝对配置路径、任何 `..` 分量、符号链接越界及输入/输出重叠会报错。配置路径不展开 `~` 或环境变量，使用 POSIX `/` 分隔符。配置检查本身不核验真实文件；第 05 步通过下述独立接口核验存在性、哈希和来源清单，数据内容检查留到后续步骤。

`radii_m` 固定为 `[500, 1000]`，`classification_mode` 固定为 `native`；`include_ev_charging` 必须是布尔值，示例默认 `false`。`poi_batch_size=10000` 和 `station_batch_size=1000` 是可调整的正整数性能参数；本步不运行分批分析。缺字段、未知字段或重复 YAML 键均报错，不自动修复配置。

实现验证在服务器代码根执行。先在数据盘分配新的运行目录，让日志与临时文件都留在该目录下：

```bash
step04_run_dir=$(mktemp -d "$HOME/Work/analysis_Digimap_POI_v2/outputs/step04-XXXXXXXX")
TMPDIR="$step04_run_dir" python -B -m unittest discover -s tests -v > "$step04_run_dir/unittest.log" 2>&1
```

测试只使用合成文件与配置；没有指定 `TMPDIR` 时，配置测试也会在数据根的 `outputs/` 下创建隔离的临时目录。代码检出目录只保存代码和文档，真实输入、运行产物及 Python 缓存不提交 Git。

第 05 步提供文件完整性核验：`src.data.open_source()` 只读打开项目内普通文件，`sha256()` 默认每块读取 1 MiB 的原始字节，`verify_sources()` 对账必需文件并检查全部声明副本。仅使用 Python 标准库，来源位置只是追溯文本，不会被运行时打开。

来源清单位于数据根下 `data/source_manifest.json`，建议同时准备 `data/SOURCES.md`，据实记录授权来源、版本、文件用途和复制过程。下面仅展示一个 **合成三字节文件 `abc`** 的完整条目，不可用于代替真实数据的复制记录：

```json
{
  "schema_version": 1,
  "files": [
    {
      "path": "data/raw/abc.bin",
      "size_bytes": 3,
      "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
      "source_location": "synthetic-fixture:abc",
      "copied_at": "2026-10-10T00:00:00+00:00",
      "source_sha256_before_copy": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
      "source_sha256_after_copy": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    }
  ]
}
```

顶层字段为 `schema_version` 和 `files`。条目必须包含示例中的七个字段，允许附加说明字段；`size_bytes` 是非负整数，摘要是 64 位十六进制字符串，`copied_at` 使用带时区的 ISO 8601 时间。拒绝重复 JSON 键、重复路径和解析到同一路径的别名声明；摘要大小写等价，报告统一小写。

调用方须另行声明非空的 `required_paths` 文件列表，不能从清单条目反推。以下调用只针对上面的合成示例：

```python
from run import DATA_ROOT
from src.data import SourceVerificationError, verify_sources

required_paths = ["data/raw/abc.bin"]  # 真实运行须换成独立确认的全部必需文件。
try:
    report = verify_sources(data_root=DATA_ROOT, required_paths=required_paths)
except SourceVerificationError as error:
    report = error.report
    raise
```

输入必须位于该数据根的 `data/raw/` 或 `cities/` 内，符号链接解析后也须留在输入区域；输出目录、来源清单自身、外部文件和非普通文件不能充当数据输入。清单本身只能从 `data/source_manifest.json` 读取。函数不创建目录、不改权限、不修复清单，也不提供写模式。

核验要求必需文件全部被声明，且每个声明文件的实测字节数等于清单大小，当前副本摘要等于清单副本摘要和复制前后源摘要。分块读取会覆盖全部字节；检测到读取期间文件状态变化时拒绝通过。`checked_count` 为完整读取并取得稳定摘要的文件数，`passed_count` 为大小、摘要与复制证据全部通过的文件数。即使声明文件均通过，清单漏了必需项时总状态仍为 `failed`。

成功返回 `status=passed` 的报告；失败抛出 `SourceVerificationError`，其 `.report` 保存全局及逐文件原因，均可序列化为 JSON。由调用方将报告、必需列表和测试日志存到新的 `outputs/<run-id>/`，不能写回原始输入目录。完整 CLI 调度留到后续步骤，`--check-config` 仍不核验真实文件。

来源摘要必须来自真实准备过程，不能拿当前副本的摘要补造历史记录。缺少副本、独立必需列表或来源证据时，真实输入核验保持未完成。哈希一致只说明核验时的字节与所给记录一致，不证明数据内容正确、来源记录可信或后续读取期间永不变化；字段、CRS、几何及真实数据质量在后续步骤验证。

第 05 步验收时，用户确认当前检出目录 `data/` 中 charging 是加工数据，其余均为直接下载数据。已将这 22 个文件按原始字节独立复制到约定的数据盘项目：`data/raw/` 保持相对路径，`data/cities/` 对应目标 `cities/`。来源性质、本次复制摘要与时间记录在数据盘的 `data/source_manifest.json` 和 `data/SOURCES.md`；独立文件列表及 22 项通过的核验报告位置见 `PROGRESS.md`。这些记录不补造原始下载历史或 charging 的加工谱系。

第 06 步提供 `src.data.poi_batches()` 与 `inspect_inputs()`。前者以只读 SQLite 游标返回原始记录字典的批次，保留真实要素主键为 `source_fid`，几何保持 GPKG 字节供逐行审计。后者先核对图层、字段与 EPSG:27700 米制 CRS，再检查原始记录质量、跨批重复 ID、分类及位置精度频数、几何/属性坐标差异和站点关键字段。依赖增加 Shapely 2.1.2、pyproj 3.7.2；不进行坐标转换。

实际输入使用图层 `Points of Interest 2026_09`、源 FID 列 `id`、几何列 `geom`、分类列 `pointx_class`、精度列 `pos_accuracy`；站点字段为 `StationID`、`Latitude`、`Longitude`。文件格式与字段映射记录在报告中。调用前先按第 05 步独立必需列表核验来源，再在新的运行目录中分别执行有限样本与全源审计。例如，以下片段需由调用方传入本次新建的 `run_relative_dir`，以及根据输入精度说明核实的 `accuracy_labels`：

```python
from src.data import inspect_inputs

arguments = dict(
    poi_path="data/raw/digimap/poi_6476197.gpkg",
    station_path="data/raw/charging/UK_OCM_stations_with_connector_summary.csv",
    data_root=DATA_ROOT,
    layer="Points of Interest 2026_09",
    poi_batch_size=10000,
    station_batch_size=1000,
    accuracy_labels=accuracy_labels,
)
sample = inspect_inputs(**arguments, output_dir=f"{run_relative_dir}/sample", sample_limit=20)
# 先展示 sample 的有限记录及报告，再执行全源审计。
full = inspect_inputs(**arguments, output_dir=f"{run_relative_dir}/full")
```

`output_dir` 相对数据根，必须为 `outputs/` 下尚不存在的子目录；函数拒绝覆盖旧报告。省略 `accuracy_labels` 时只统计原始精度值，不判断是否为未知精度码；真实运行从新项目 `POSITIONAL_ACCURACY_LOOKUP.txt` 读取 1—4 四档描述。该表没有米级误差阈值，本步不按精度档位删除记录。

`input_inspection.json` 是使用产物前的状态入口。`status=completed` 表示声明范围内的审计完成，质量问题另由 `quality_issues_present` 表示；后续消费全源重复清单必须同时检查 `full_source_complete=true`、`duplicate_list_complete=true` 及输入摘要。失败抛出 `InputInspectionError`，其 `.report` 和落盘报告均保留原因；不能使用失败报告旁的部分文件充当完整产物。所有目录应保持与这份报告一起使用。

`sample_limit` 表示按原始 FID 顺序检查前若干条，报告 `scope=sample`，局部重复清单文件名带 `sample_` 前缀。样本不能代表全局频数；即使样本上限大于行数，完成标志仍不会升级为全源。全源输出也保存最多 20 条可追溯样本，样本 JSONL 每条均标明 `scope=sample`。

主要审计文件如下：

|文件|一行的含义|
|---|---|
|`classification_frequencies.csv`|一个原始分类值及类型的源记录频数、格式状态、三层代码；字典状态未检查|
|`native_level_frequencies.csv`|一个层级和代码的全源频数；每层含 UNKNOWN 后独立加总到扫描行数|
|`positional_accuracy_frequencies.csv`|一个原始精度值及类型的频数和已核实描述|
|`duplicate_ref_nos.csv`|一个重复的非缺失原始 ID 键及出现次数|
|`duplicate_ref_rows.csv`|一个重复 ID 的一条源记录，包含首次出现的行、源路径、图层和 FID|
|`poi_quality_issues.csv`|一条 POI 源记录的一项问题；同一记录可有多项|
|`coordinate_differences.csv`|一个可比较点的几何/属性坐标、dx、dy 及米制差异|
|`station_quality_issues.csv`|一条 CSV 逻辑记录的一项关键字段、位置或 ID 冲突问题|

`audit_index.sqlite` 为本次磁盘审计索引；重复检测跨全部批次累计，不将全源几何放进内存。NULL/空串/非有限 ref_no 单列为缺失，其他值保留类型及原值精确比较；不修剪或补零。坐标差异只计算有效非空 Point 与有限属性坐标，无法比较不填 0；点集外接矩形不是数据覆盖边界。

本步频数以全部源行为分母，含重复、几何问题、缺失/异常分类以及充电设施。它们不是 PUA、业务筛选或清洗后的统计。站点同坐标不同 ID 的错误只记录，实际合并留到站点准备步骤；官方字典连接留给第 07 步。运行证据和具体结果见 `PROGRESS.md`，测试通过不表示用户已验收。

第 07 步使用 `native_dictionary()` 构建官方三层字典；返回对象的 `classes` 是以完整八位码为键的映射，可传给 `classify()`。TXT 按实际竖线分隔和 UTF-8 编码读取，完整代码沿外键连接，源 POI 编码不补零或修复。类别局部编号是 class 的外键目标，必须唯一；完整统计键分别是 2/4/8 位代码。

本步的显式调用如下，`run_relative_dir` 须是数据根 `outputs/` 下本次运行目录，`required_paths` 须是独立确定的必需列表：

```python
from run import DATA_ROOT
from src.data import inspect_dictionary

report = inspect_dictionary(
    "data/raw/digimap/docs/POI_GROUPS.txt",
    "data/raw/digimap/docs/POI_CATEGORIES.txt",
    "data/raw/digimap/docs/POI_CLASSIFICATIONS.txt",
    "data/raw/digimap/poi_6476197.gpkg",
    data_root=DATA_ROOT,
    inspection_dir="outputs/step06-20261010T210932Z-8e6f6702/full",
    output_dir=f"{run_relative_dir}/full",
    layer="Points of Interest 2026_09",
    required_paths=required_paths,
    batch_size=10000,
)
```

函数先核对来源清单及第 06 步审计产物的完整状态、摘要和当前 POI 指纹，再全源分批统计三层分类名称，不解码几何。分类频数对官方与源代码做并集合并：字典独有项为零，源独有项保留，缺失/异常原码留在审计中。`category_labels()` 优先选频数最多的非空源分类名称，并列按 Unicode 字符串升序选；再回退官方名称、完整代码。全部候选和冲突标志保留，同名不同码不合并。

|第 07 步文件|一行含义|
|---|---|
|`native_dictionary.csv`|一个官方层级和完整代码、父级、原始局部编号及来源记录|
|`classification_dictionary.csv`|一个 class 并集项的完整代码、官方三层名称、源频数和匹配状态|
|`classification_frequency_audit.csv`|一个原始分类值及类型的频数、格式与官方匹配状态|
|`source_name_frequencies.csv`|一个层级、完整代码与原始源名称的频数，含名称缺失项|
|`category_labels.csv`|一个层级和代码的最终标签、来源、频数、冲突及名称差异标志|

`dictionary_validation.json` 保存输入、前一轮审计产物及本次产物摘要、对账和质量摘要；`name_index.sqlite` 是本次磁盘索引。消费前须检查 `status=completed`、`validation_complete=true`。失败抛出 `DictionaryValidationError` 并保存原因，附近的部分产物不能视作成功结果。调用与验证继续在指定服务器执行，实际结果和命令见 `PROGRESS.md`；本步不新增完整阶段 CLI。
