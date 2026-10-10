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

绝对配置路径、任何 `..` 分量、符号链接越界及输入/输出重叠会报错。配置路径不展开 `~` 或环境变量，使用 POSIX `/` 分隔符。真实文件存在性、内容、哈希和来源清单完整性在后续步骤核实。

`radii_m` 固定为 `[500, 1000]`，`classification_mode` 固定为 `native`；`include_ev_charging` 必须是布尔值，示例默认 `false`。`poi_batch_size=10000` 和 `station_batch_size=1000` 是可调整的正整数性能参数；本步不运行分批分析。缺字段、未知字段或重复 YAML 键均报错，不自动修复配置。

实现验证在服务器代码根执行。先在数据盘分配新的运行目录，让日志与临时文件都留在该目录下：

```bash
step04_run_dir=$(mktemp -d "$HOME/work/analysis_Digimap_POI_v2/outputs/step04-XXXXXXXX")
TMPDIR="$step04_run_dir" python -B -m unittest discover -s tests -v > "$step04_run_dir/unittest.log" 2>&1
```

测试只使用合成文件与配置；没有指定 `TMPDIR` 时，配置测试也会在数据根的 `outputs/` 下创建隔离的临时目录。代码检出目录只保存代码和文档，真实输入、运行产物及 Python 缓存不提交 Git。
