# 建筑照片 → mesh → BIM：已有原型与后续边界

2026-10-18收工决定：下一轮转向搭建textured mesh harness，先复用已有GLB观察与源BIM能力，当前不启动实现、模型测试或生成服务。具体入口见[收工交接](../reports/2026-10-18_closeout_model_candidates_and_mesh.md)。

以下为2026-10-14保存的照片原型记录。它尚未接入自主harness，其开发代理介入、外部标定和推断边界继续保留。

## 路径与输入性质

```text
原建筑照片
  → 图像清理、遮挡补全、立面语义着色（生成内容）
  → Hunyuan3D 3.1 textured mesh（形状提案）
  → 方向/尺度处理、多视角和截面观察
  → 开发代理介入：规整体量、楼层、核心筒和开口，生成 IFC
  → 独立转换：项目 source_bim_v3 + 原版查看器
```

现有案例是 `photo-figure15-hunyuan-20261014-01`，原材料为论文 Figure 15 中提取的照片面板。试验明确为 `supervised_quality_first`，`uses_autonomous_harness=false`；使用当前图像生成与 Hunyuan3D 3.1，也未实现原研究的 GraphSAGE，因此不是严格论文复现。

实际输入不只有照片：初始尺度采用80m高度假设，后续用了论文给出的16,315㎡建筑面积和0.56窗墙比作外部标定。`experiment.json` 的80m是初始假设；最终采用值应读 `bim/parameters.json` 的统一缩放和层高链，不把初始假设当最终测量。标定条件不是独立精度参照，也不能声称从照片自行恢复了这些数值。

## 已存在的可复用部分

| 环节 | 原型已有内容 | 尚不能外推的能力 |
|---|---|---|
| 输入与生成 | 原图、生成图、提示文本、服务提交与返回记录分别保存 | 补全的遮挡面不是建筑实测证据；生成mesh不是真值 |
| mesh观察 | 保留原始及尺度处理后的GLB，多视角、截面、窗带与拟合记录 | 无独立的真实尺寸、背面或内部精度验证 |
| 监督重建 | 参数化脚本、IFC4、开口宿主关系与显示网格 | 开发代理直接判断和修改，不能算work model独立冷启动 |
| 项目源BIM | 转换为现有source_bim_v3，复用项目平面图、交付清单、原版查看器 | 现有直角建模/修订工具未验证支持该圆塔多边形；可查看不等于全套工具可编辑 |
| 几何检查 | 已存IFC解析/三角化、源空间覆盖、开口映射和浏览器检查 | 未验证真实建筑保真、实际用途/室内布局、EnergyPlus或桌面IFC软件导入 |

已有IFC记录63个空间、513个开口；项目源BIM为21层、44个几何空间、555条开口片段，保持513个原IFC开口的映射及面积。两个格式的计数口径不同，不把片段数差异说成新增或丢失真实门窗。

每层功能开敞区环绕核心筒，因当前源空间使用无孔单环，拆成两个几何单元，共界明确开放；另有两个连续跨层核心空间，避免按层生成虚假井道楼板。21层、尺度、核心筒、门位与室内布置仍含推断。该处理只是本案例的表达，不设为照片输入的默认细度政策。

## 固定证据与入口

原始试验保留在 `D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01`，不迁移、不改原记录，也不复制大网格或案例到产品目录。

| 内容 | 入口 |
|---|---|
| 全流程、图片对照与边界 | [原试验报告](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/report.html) |
| 当前主交付 | [项目原版查看器](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/project-bim/viewer.html) · [源BIM](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/project-bim/source_model.json) |
| 原始/尺度处理mesh | [生成GLB](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/mesh/generated-original.glb) · [尺度处理GLB](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/mesh/input-metric.glb) |
| 输入与外部条件 | [实验范围](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/experiment.json) · [采用参数](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/bim/parameters.json) |
| 监督重建与转换 | [BIM脚本](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/build_bim.py) · [项目转换脚本](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/export_project_bim.py) |
| 已存验收 | [IFC检查](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/review/bim-validation.json) · [源格式转换检查](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/project-bim/conversion_report.json) · [已有浏览器验收记录](D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01/project-bim/final_verification.json) |
| 本轮只读固化 | [Git内证据索引与关键SHA](../reports/2026-10-14_harness_refinement/photo-route-evidence.json) · [原核验记录](D:/Sim-BIM-Agent/cache/harness-refinement-20261014/photo-route-audit.json) |

本轮复核原交付清单65份文件SHA均一致，并记录原始mesh、处理mesh、生成/转换脚本和源BIM等关键文件SHA。仅核验已有文件，未重跑历史服务、重建或浏览器验收。既有最终转换记录的“0模型调用”只指那次格式转换，不代表之前图像/mesh生成零调用或免费。

## 后续讨论范围

1. 交付目标与细度：体量、楼层开敞区、核心筒、房间分别需哪些证据；无平面时不默认编造房间级还原。
2. 输入可信度：分开原照片、生成补全、mesh提案、人工/模型假设和外部标定；冲突时回到原始证据。
3. 尺度与不可见部分：如何表示未知尺度、真实楼层数、背立面和室内；哪些条件需用户补充。
4. 接入边界：候选方法能否复用现有mesh观察与源BIM，而不新增第二套runtime；圆弧/斜边的建模和局部修订支持另评。
5. 验证：用独立参照评外部形状、开口和空间关系；有监督原型、无干预冷启动和服务生成质量分别统计。

生成服务选型、自动调度和统一细度政策本轮均未定案。恢复这条线时先读本页和原试验记录，再明确一个具体问题；不因已有脚本或服务提交记录自动重复调用。
