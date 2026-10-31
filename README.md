# Sim-BIM Agent

将建筑图纸和带贴图的三维网格转换为轻量建筑信息模型。

Sim-BIM Agent 结合模型的视觉理解与建筑建模工具，生成包含楼层、空间、边界、门窗和连接关系的 **Sim-BIM**，并提供可在浏览器中打开的三维查看器。

**当前版本：Windows · PNG 图纸 / GLB 网格 · JSON 模型 + HTML 查看器**

[下载最新版本](https://github.com/hortonyyx/Sim-BIM-Agent/releases/latest) · [版本记录](https://github.com/hortonyyx/Sim-BIM-Agent/releases) · [反馈问题](https://github.com/hortonyyx/Sim-BIM-Agent/issues)

## 可以做什么

- **从图纸建模**：结合平面图与立面图，组织建筑空间、边界、门窗及连接关系。
- **从三维网格重建**：读取带贴图的建筑 GLB，结合辅助图纸和文字说明，还原外形、楼层关系和外窗，并补充内部空间。
- **查看与保留结果**：在 HTML 查看器中检查三维模型，保存结构化 JSON，供后续修改和处理。
- **记录建模依据**：结果保留假设、冲突和未解决事项，便于检查模型对资料的理解。

## 安装与快速体验

准备 Windows、Python 3.12 和 uv，并确保终端可以运行 `uv`。

1. 在 [最新版本](https://github.com/hortonyyx/Sim-BIM-Agent/releases/latest) 的附件中下载 `Sim-BIM-Agent-*.zip`。
2. 解压到一个独立目录，在该目录打开 PowerShell。
3. 安装并检查环境：

```powershell
./setup.ps1
./Start.cmd doctor
```

首次安装需要联网下载依赖。看到 `"status": "ready"` 后即可继续。

可以先运行一个不需要模型账号的本地示例：

```powershell
./Start.cmd demo --output D:/Sim-BIM-Data/demo-001
```

示例会生成一个两房间模型，并返回 `source_model` 和 `viewer` 路径。用浏览器打开 `viewer` 对应的 HTML 即可查看。再次体验时换一个新的输出目录，例如 `demo-002`。

## 准备建筑资料

| 输入 | 支持内容 | 使用模板 |
|---|---|---|
| 建筑图纸 | 同一建筑的平面、立面 PNG 图片 | [config.example.json](config.example.json) |
| 三维网格 | 几何与贴图包含在文件内的 GLB；可附辅助 PNG 和建筑说明 | [config.mesh.example.json](config.mesh.example.json) |

建议把资料、配置和结果放在独立的数据目录，便于更换程序版本：

```text
D:/Sim-BIM-Data/
├─ inputs/
│  └─ building-a/
│     ├─ drawings/        平面、立面 PNG
│     ├─ building.glb     三维网格
│     ├─ auxiliary/       可选的辅助 PNG
│     └─ building.json    可选的建筑说明
├─ config/
│  ├─ drawings.json
│  ├─ mesh.json
│  └─ deepseek.env
└─ runs/
```

根据输入复制一份模板到 `config` 目录，分别命名为 `drawings.json` 或 `mesh.json`，并修改以下字段：

| 字段 | 填写方式 |
|---|---|
| `input_root` | 输入根目录，例如 `D:/Sim-BIM-Data/inputs` |
| `run_root` | 结果目录，例如 `D:/Sim-BIM-Data/runs` |
| `cases` 中的 `input` | 相对输入根目录的路径：图纸为 `building-a/drawings`，网格为 `building-a/building.glb` |
| `cases` 中的 `output` | 本次任务的结果目录名；每次新建任务使用新名字 |
| `cases` 中的 `scope` | 在任务说明中补充建筑用途、已知尺寸、材料含义和建模要求 |

图纸模式读取指定目录直接包含的 PNG，文件名应便于辨认楼层或立面方向。当前图纸入口不读取 PDF、JPEG 或子目录中的图片。

网格模式中，`auxiliary_images` 和 `building_input` 分别填写辅助图片目录与建筑说明文件的相对路径。**只有 GLB 时，删除这两个可选字段即可。** 有辅助材料时，按上面的目录示例分别填写 `building-a/auxiliary` 和 `building-a/building.json`。

建筑说明使用 UTF-8 JSON，例如：

```json
{
  "task_brief": "重建办公楼，优先保留外形、楼层关系和外窗。",
  "confirmed_facts": ["所附平面图对应第五层"],
  "user_conjectures": [],
  "unknowns": ["其余楼层的内部布局未知"]
}
```

## 配置模型

真实建模需要联网，并使用你自己的模型账号或 API 凭据。任务所需的图片和资料会发送到所选模型服务，API 用量由服务商按你的账户计费。

### DeepSeek Flash

以下以 `mesh.json` 为例。在自己的 `D:/Sim-BIM-Data/config/deepseek.env` 中填写：

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY
```

在 `mesh.json` 的 `cases` 对应任务中替换或补充这些字段，其余参数先保留模板默认值：

```json
{
  "provider": "deepseek",
  "model": "deepseek-flash",
  "credentials_file": "D:/Sim-BIM-Data/config/deepseek.env",
  "reasoning_effort": "high"
}
```

将 `YOUR_DEEPSEEK_API_KEY` 换成自己的密钥。凭据文件保存在自己的数据目录，不要上传到仓库或附在问题反馈中。

### 使用默认模型配置

图纸和网格模板默认使用 ChatGPT 账号授权。在程序目录执行：

```powershell
./Start.cmd auth login
./Start.cmd auth status
./Start.cmd auth models
```

可用模型以账号返回的列表为准。图纸模板为多个角色分别配置模型；调整型号时，需要同时修改顶层模型字段和 `roles` 中的相应设置。

使用 OpenCode Go 的用户可参考 [配置示例](config.opencode-go.example.json)。

## 开始建模

在程序目录先检查配置，再启动任务。以下任务名沿用模板默认的 `case_id`；如已修改，请同步替换命令中的名称。

**图纸任务：**

```powershell
./Start.cmd check D:/Sim-BIM-Data/config/drawings.json
./Start.cmd run D:/Sim-BIM-Data/config/drawings.json --case my-building
```

**网格任务：**

```powershell
./Start.cmd check D:/Sim-BIM-Data/config/mesh.json
./Start.cmd run D:/Sim-BIM-Data/config/mesh.json --case my-textured-building
```

`check` 检查配置与输入文件，不发起模型请求。`run` 开始实际建模；请求次数、工具次数和时间上限可在配置的 `limits` 中设置。

同一版本内中断后，可以保留原配置和输入，追加 `--resume` 继续：

```powershell
./Start.cmd run D:/Sim-BIM-Data/config/mesh.json --case my-textured-building --resume
```

## 查看结果

结果保存在 `run_root` 下本次任务的 `output` 目录中。完成后按照程序返回的交付路径打开结果：

| 文件 | 用途 |
|---|---|
| `source_model.json` | Sim-BIM 源模型：楼层、空间、边界、门窗、连接及建模说明 |
| `viewer.html` | 对应模型的三维查看器，可直接用浏览器打开 |
| `delivery.json` / `delivery.html` | 本次正式交付的记录与展示入口 |

**JSON 保存建筑模型，HTML 提供可视化。** 运行过程中可能产生多个候选版本，请以最终交付记录选中的结果为准。

## 当前范围

Sim-BIM Agent 仍在迭代中，当前通过命令与配置文件使用，支持 PNG 图纸和 GLB 网格输入，交付范围为 Sim-BIM 与 HTML 查看器。CAD、实景照片、其他模型格式及图形化编辑尚未开放。

生成质量受资料完整度和所选模型影响。使用结果前，请结合原始资料检查尺寸、空间划分、门窗位置和连接关系，尤其留意输入无法确定的内部布局。

## 更新与反馈

更新时下载新版 ZIP，解压到新目录并运行 `setup.ps1`，继续使用自己的输入、配置和结果目录。进行中的任务请在原版本中完成或恢复。

遇到问题可在 [Issues](https://github.com/hortonyyx/Sim-BIM-Agent/issues) 中提供版本号、输入类型、所用模型、复现步骤和错误信息；分享资料前请移除密钥及不便公开的内容。

查看器使用 Three.js 与 OrbitControls，许可信息见 [第三方说明](scripts/tool_scripts/vendor/README.md)。
