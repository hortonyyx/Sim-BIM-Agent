# Mesh 夜间迭代与验收

本轮按用户夜间授权继续交付档位之外的 harness，所有新增真实测试仅 GPT-6.1 Sol/high。分工保持探索，09:00（Asia/Singapore）汇报后讨论。当前恢复根为 `D:/Sim-BIM-Agent/cases/mesh-harness-overnight-20261024/`，先看 `handoff.json`、`PROGRESS.md` 和各子目录实际 controller/seal，避免重复派发。本线程定时续跑为 automation `mesh-harness`。

## 利园完整验收已结束，独立质量仍在复核

冻结 `dd8768f3`、runtime42/domain141，原三个盲输入和原scope保持；48模型/240工具、无额外墙钟或token上限、worker8、Sol/high及32k本地输出预留。旧候选、GT、评价不进入模型。实际输出 `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-overnight-20261024-001`。

原生 runtime 用时 **1339.250秒（22分19秒）**，外部准备至退出1361.086秒。主控24请求、worker3请求，全27次实际请求已核Sol/high。C04显式finish，finalization.delivery、7个绑定交付文件hash及只读前后不变检查通过。候选保存/首次有效时点、返工机制、用量及独立几何/视觉质量正在复核，不能以completed替代质量判断。

与前轮runtime41/domain138的27分37.97秒相比，本样本少318.72秒（5分18.72秒），实际请求33→27；仍比旧runtime36/domain132的20分40.70秒慢98.55秒。代码、模型自主轨迹与服务时点都不同，不将单次差值归为某个特性的严格因果收益。本次完整验收**尚不含**下面的新证据投影改动。

封印及终态审计：外部 `lee-acceptance/`；固定原frame的9视角射线/宿主/核心检查与独立视觉在 `evaluation/lee/`。模型申报质量、机器合法性及独立质量分别记录。方法中的 `binding.is_delivered_candidate` 只是从completion取候选；本轮正式交付依据另有terminal-audit，不将该描述字段当验收门。

## 已集成的新改动

源码提交 `772aa6ca64c1dfad80d2294c87fd3f87415dc92b`，runtime42/domain142；仅mesh worker主控/读图端domain变动。利园终态封印核验后才合入主库。没有改变几何合法性、档位或停止条件。

1. 每个worker答案内，完整相同的图片证据对象只展示一份，逐claim用evidence_ref引用。按整个对象hash去重，原图、ROI、来源快照不同不合并；原始outcome/journal完整保留。
2. read_mesh_worker指定图片或量测selector时，只展示所选材料、完整量测和结果hash/完整答案读取入口，不重复整个答案与无关目录。无selector仍可读取完整答案及唯一证据表，不依赖隐式“已经读过”状态。worker结束不等于质量认可，快照适用性与fidelity边界保持。
3. 辅助裁图坐标说明消除冲突：题目不需要或禁止坐标时允许省略/null；任何实际填写的框仍须采用原图坐标并位于实际供图范围。schema及坐标校验不放松。

| 验证范围 | 结果 |
|---|---|
| 新回执、原projection及ROI定向 | 32通过，23.18秒 |
| worker/evidence相邻 | 88通过，28.82秒 |
| 独立审阅，含4个新反例 | 36通过，26.10秒，无剩余阻断；与上述有重叠，不相加为唯一覆盖数 |
| 旧回执纯内存重放 | Lee取图read 46,831→3,317字符；Energy delegate 28,247→14,190；ROI Sol预取10,681→5,617。原raw/PNG、逐claim证据和结果hash不变，Lee55个有效查询保留 |

独审位于 `D:/Sim-BIM-Agent/cache/mesh-harness-overnight-20261024/receipts-review/`；受审主文件SHA256为 `3854d1355efc1b3bbf0a64cd28341153f706d5391d979684ca02af9c8c3c3b10`。字符减少不等于provider token、费用或整案秒数收益。

## 下一批小验收准备

- `receipt-acceptance/`：一次真实Sol主控+worker窄协议，最多10模型/24工具/600秒，worker4/6/300。明确指示delegate、同次取图和完整量测、再按入口取回默认答案；考查模型能否操作新引用，不是自主调度能力。实际MCP加ScriptedAdapter离线5通过/14.56秒、38断言全真，0真实provider；独审无阻断。已保留首轮外部脚本解析失败及修复。尚未封印/启动真实验收。
- `delegation-experiment/`：两题四腿，立面招牌文字/图标和完整可见窗带行数；预取1请求/0工具、按需2请求/1工具，全部Sol/high，串行实际请求总上限6，14次synthetic零用量脚本请求另列，正式工具共12。两臂收到同字节1x无网格图片。外部进程统一固定max_answer_repairs=0，禁止用修复轮挑选更好样本；这不是生产专用角色能力。旧main的离线链只验证外部脚本，合入后的实质新代码需再验证后封印。尚未启动真实模型。

实验输入与参考分别冻结，only task PNG和必要原GLB进入worker。正式schema/来源、任务字段合同、视觉事实可用性三层分别评价。已有图和固定ROI由评审侧预先给定，不能称为自主寻找视图。

## 历史归因更正及成本边界

原ROI四腿全部7次真实请求的高优先级说明要求“cite ... and box_pixels”，与题面禁坐标/notes要求null冲突。Qwen off输出不满足题目合同是事实，但该样本不能作为纯模型遵从能力失败的证据；其他三腿没输出坐标也不证明提示没有影响。原run全文件hash保持，追加审计见 `delegation-design/prompt-audit/`，不改旧答案。

`delegation-design`早期方案写“人工参考/人工核验”不准确，实际由独立Codex子agent建立视觉参考，主控另看图核对，没有用户人工标注。原盲参考数值和封印保持；`reference-provenance-correction.json`（v1，SHA256 `d2f32042d01d1d9ae049090719732851c055c09baac8daba571370673c6fc7a8`）追加更正，新run seal必须纳入。Codex开发/审阅/协调推理开销未测，记unknown，不能填0或声称已计完整主控/人工成本。真实worker、脚本parent、准备和独立核验的费用边界分别说明。

## 当前未完成

完成利园独立质量/机制分析；冻结新版本并运行两组Sol小验收；据结果决定后续非档位优化。主控直接measure_mesh_pixels回执的摘要/原报告读回仅在分析，未实现。生产异构/异步调度和交付档位仍未实施。未更新桌面；`.playwright-cli/`原有资料保留。
