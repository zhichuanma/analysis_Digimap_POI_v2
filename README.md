# Digimap POI 分析重写

项目按 `REWRITE_GUIDE.md` 逐步推进，分析口径见 `SPEC.md`，实际验证与验收状态见 `PROGRESS.md`。

第 04 步提供路径和配置检查。服务器使用 Python 3.13.2、PyYAML 6.0.3；本步依赖见 `requirements.txt`。

代码根由 `run.py` 的实际位置确定。`--config` 只接受相对此根的路径，默认 `config.yaml`，与当前 shell 工作目录无关。数据根按 SPEC 固定为 `~/work/analysis_Digimap_POI_v2/`，当前解析到 `/mnt/data/work/mazhichuan/analysis_Digimap_POI_v2/`，不接受 YAML 或命令行更换根目录。

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
step04_run_dir=$(mktemp -d "$HOME/work/analysis_Digimap_POI_v2/outputs/step04-XXXXXXXX")
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
