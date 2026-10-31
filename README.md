# Sim BIM Agent

从建筑图纸或带贴图的三维网格生成轻量 BIM 和 HTML 查看器。一个程序提供两个输入入口，共用源 BIM、几何工具、查看器和自有 runtime；运行不需要 Codex 进程。模型调用需要用户自己的账号授权或 API 凭据。

## 安装与启动

当前交付为 **Windows 源码安装包**，不是独立 EXE。安装前准备 Python 3.12 和 uv，在产品目录内用 PowerShell 执行：

```powershell
./setup.ps1
./Start.cmd doctor
./Start.cmd demo --output D:/Sim-BIM-Data/runs/demo-001
```

`setup.ps1` 按随包的 `uv.lock` 建立本目录的 `.venv`，首次安装可能需要联网下载依赖。不要复制其他目录的虚拟环境。`doctor` 校验发布文件、版本和基础依赖，不调用模型；`demo` 生成明确标记的两房间合成示例及离线查看器，不验证真实建筑识读质量。示例每次使用新的输出目录。

## 两个输入入口

| 入口 | 配置模板 | 输入与运行方式 |
|---|---|---|
| 图纸 | `config.example.json` | PNG 图纸目录；调度员、平面读图员和立面读图员分工 |
| Textured mesh | `config.mesh.example.json` | 自包含、带贴图的 `.glb`；单模型调度员，可附辅助 PNG 和文字声明，可选只读 worker |

将需要的模板复制到产品目录外，例如 `D:/Sim-BIM-Data/config/drawings.json` 或 `mesh.json`，再修改：

- `input_root`：本案输入的专用绝对路径；`input`：其中的图纸目录或 GLB 文件相对路径。
- `run_root`：独立运行输出目录；`output`：本次运行名。新任务使用新名字，续跑保留原配置并加 `--resume`。
- `case_id`：命令的 `--case` 值；`scope`：本次任务目标、材料含义和需要保留的不确定性。
- 模型服务与凭据：按所用模板配置；图纸分工的顶层模型设置须与 `roles.coordinator` 一致。

模板中的 `D:/Sim-BIM-Agent/...` 是示例路径，需换为自己的目录。图纸、个人配置、运行结果和凭据都应放在产品目录及成品 Git 仓库之外。

图纸输入只读取指定目录直接包含的 PNG；当前此入口不接收 JPEG 或 PDF，也不递归查找子目录。`floor_plan_images` 如有填写，须为实际 PNG 的准确文件名。

Mesh 模板的 `auxiliary_images` 指向辅助 PNG 目录，`building_input` 指向 UTF-8 JSON 对象，两者均须在本案 `input_root` 内。**只有 GLB 时，删除这两个可选字段。** 文字声明中的路径不会自动加载其他文件。可用以下声明起步，并按实际证据修改：

```json
{
  "task_brief": "根据原网格重建轻量 BIM，优先外形、楼层关系和外窗。",
  "confirmed_facts": [],
  "user_conjectures": [],
  "unknowns": ["内部房间布局未知"]
}
```

Mesh 模板保留 8 个可选只读 worker 的并发上限；是否派工由调度员决定，worker 沿用本案模型和共享预算，主 BIM 只由调度员修改。删除 `mesh_workers` 字段可由调度员直接工作。模板默认模型、思考强度和预算见 JSON；本发布没有更改这些策略。时间目标、运行预算和建筑质量验收是不同事项。

先预检，再运行所选案例：

```powershell
./Start.cmd check D:/Sim-BIM-Data/config/drawings.json
./Start.cmd run D:/Sim-BIM-Data/config/drawings.json --case my-building
./Start.cmd run D:/Sim-BIM-Data/config/drawings.json --case my-building --resume

./Start.cmd check D:/Sim-BIM-Data/config/mesh.json
./Start.cmd run D:/Sim-BIM-Data/config/mesh.json --case my-textured-building
```

`check` 验证配置和输入文件，不调用模型，也不验证整栋质量或 GLB 是否能完成重建。运行结果位于配置的输出目录；候选不等于正式交付，应检查最终运行回执及交付路径。

## 模型授权

默认模板使用 ChatGPT 订阅：

```powershell
./Start.cmd auth status
./Start.cmd auth login
./Start.cmd auth models
```

授权保存在本机专用存储，更新程序不要求重新登录。其他服务通过配置中的 `credentials_file` 指定外部凭据文件；OpenCode Go 示例为 `config.opencode-go.example.json` 和 `config.opencode-go-haiku.example.json`。不要把密钥填入模板、发布包或 Git。账号连通与模型质量须分别验证。

## 版本与更新

成品仓库存放由开发库生成的发布内容，不在这里进行功能开发。`release-manifest.json` 记录开发源提交、runtime/domain 版本及逐文件哈希；成品仓库自己的提交用于管理发布版本。

安装和隔离验收使用从已发布版本导出的 **无 `.git` 目录**。当前更新器与发布验收器拒绝把 Git 检出目录当作安装包。`.venv` 在安装目录本地建立，不进入 Git 或发布包。

开发维护者在开发库使用 `scripts/release/build_product.py` 构建，经 `scripts/release/verify_product.py` 验证后再同步成品仓库。已有固定安装可由开发库的 `scripts/release/update_product.ps1` 按发布清单更新及回滚；这些维护脚本不属于本安装包。更新前结束当前运行，保留外部输入、配置、输出和授权存储。

离线发布验收覆盖文件身份、隔离导入、合成 BIM/查看器、两个模板的配置路由与输入冻结、mesh 渲染和 runtime 恢复。它不调用真实模型，不代表图纸分工、整栋重建或建筑质量已经通过。本程序的模型自检与独立质量评价也应分开看待。

完整交互编辑界面尚未交付；EnergyPlus 是可选下游，需要另行安装和验收。查看器使用随包附带的 Three.js/OrbitControls，来源及许可见 `scripts/tool_scripts/vendor`。
