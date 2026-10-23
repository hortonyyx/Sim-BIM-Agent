# Mesh 夜间迭代与验收

本轮按用户夜间授权继续交付档位之外的 harness，所有新增真实测试仅 GPT-6.1 Sol/high。分工保持探索，09:00（Asia/Singapore）汇报后讨论。当前恢复根为 `D:/Sim-BIM-Agent/cases/mesh-harness-overnight-20261024/`，先看 `handoff.json`、`PROGRESS.md` 和各子目录实际 controller/seal，避免重复派发。本线程定时续跑为 automation `mesh-harness`。

## 利园完整验收与独立质量复核已结束

冻结 `dd8768f3`、runtime42/domain141，原三个盲输入和原scope保持；48模型/240工具、无额外墙钟或token上限、worker8、Sol/high及32k本地输出预留。旧候选、GT、评价不进入模型。实际输出 `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-overnight-20261024-001`。

原生 runtime 用时 **1339.250秒（22分19秒）**，外部准备至退出1361.086秒。主控24请求、worker3请求，全27次实际请求已核Sol/high；62工具（60+2），全树3,050,309实报tokens。C04显式finish，finalization.delivery、7个绑定交付文件hash及只读前后不变检查通过。首机器有效C03工具返回11:27.22，之后10:52.03；末稿C04返回19:02.91，至终态3:16.34。文件mtime、build_timings和工具返回分别记录，不混成一个首稿时间。

独立Codex视觉审阅支持当前Lite BIM用途的主体外形、主要窗带和核心组织；318开口无宿主问题，7连续核心无中间foreign盖板，23占用层×3lift/2stair的115条接入具备潜在连通。AIR/PIPE无门不扣分。上部复杂窗型、屋顶设备和非23F内部仍有明示简化/推断，不称测绘或竣工还原。32条floor记录包含7核心支撑，并非32物理层；public_names序号让F23图标题显示F30，仅展示混淆，未改源身份。

固定2304射线中四案共同有效1122：新稿5/10/30cm内34.76%/81.37%/95.01%，中位误差5.94cm。两两新/前稿共同1155中，30cm为94.11%对94.46%，不能挑分母说全面改善。详细数值、覆盖、独立视觉及边界分别在外部 `evaluation/lee/NUMERIC-AND-MECHANISM-REVIEW.md`、`INDEPENDENT-VISUAL-REVIEW.md`。

与前轮runtime41/domain138的27分37.97秒相比，本样本少318.72秒（5分18.72秒），实际请求33→27；仍比旧runtime36/domain132的20分40.70秒慢98.55秒。代码、模型自主轨迹与服务时点都不同，不将单次差值归为某个特性的严格因果收益。本次完整验收**尚不含**下面的新证据投影改动。

封印及终态审计：外部 `lee-acceptance/`；固定原frame的9视角射线/宿主/核心检查与独立视觉在 `evaluation/lee/`。模型申报质量、机器合法性及独立质量分别记录。方法中的 `binding.is_delivered_candidate` 只是从completion取候选；本轮正式交付依据另有terminal-audit，不将该描述字段当验收门。

字段聚合、failed_batch引用局修、image_view_ids预取和worker量测摘要四项在本轮均未触发，不能把用时改善归因于它们。declared-host提示已出现且随后补丁成功，但不单独作因果断言。C01/C02唯一severe是floor_coverage毫米级缺口；进一步确定性重放发现**原始parametric_plan完整覆盖，0.1m supporting-line规整后产生真实裂口**。源验证在正确拒绝规整结果，不放宽阈值。根因及最小拓扑一致修复在隔离树调查，尚未合入。

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

## 新版真实Sol小验收

- `receipt-acceptance/`：冻结54d1255e/runtime42/domain142，真实Sol主控+worker于85.589秒完成（另共享准备1.214秒），6真实请求/4工具、全树39,979 tokens；其中parent23,952、worker16,027，controller.reported_tokens仅父级。33协议检查及16独审证据检查全真，两条worker自然语言与实际valid flags/3.1899948120124404m一致。显式委派、同次取图和完整量测、再取完整答案的guided流程成功，不证明自主调度。原416文件读前后hash不变。离线5通过/14.56秒、38断言与第一次解析失败历史另存。
- `delegation-experiment/`：同版两题四腿全部终态，123.81秒、6真实Sol/high请求、28,030实报tokens，另14 synthetic parent请求与12正式工具。四腿均19协议项及任务合同通过；实际图片字节、native新坐标提示、max_answer_repairs=0外部同臂控制均验证。末腿后显式原seal核验两次通过。独立视觉与提示审阅已通过，见 `evaluation/facade/`；合同与视觉两种证据分别保留。旧版两次离线smoke保留，新版离线1通过/40.93秒、57检查全真，0真实provider。

| 题目/供图 | 子任务真实请求 | 实报tokens | worker秒 | 每腿完整秒（含准备/审计） |
|---|---:|---:|---:|---:|
| 招牌/预取 | 1 | 4,712 | 17.99 | 32.60 |
| 招牌/按需 | 2 | 9,769 | 22.16 | 32.78 |
| 窗带/按需 | 2 | 9,260 | 16.54 | 28.03 |
| 窗带/预取 | 1 | 4,289 | 13.93 | 27.77 |

预取少一次请求且输入用量下降，**本样本端到端几乎持平**；不同题目、单次样本与服务时点不能推稳定提速。订阅费用未知。父级是脚本、核验由Codex在外部进行，未计开发/协调推理费用，不能声称完整分工已经降本。

实验输入与参考分别冻结，only task PNG和必要原GLB进入worker。正式schema/来源、任务字段合同、视觉事实可用性三层分别评价。已有图和固定ROI由评审侧预先给定，不能称为自主寻找视图。

## 历史归因更正及成本边界

原ROI四腿全部7次真实请求的高优先级说明要求“cite ... and box_pixels”，与题面禁坐标/notes要求null冲突。Qwen off输出不满足题目合同是事实，但该样本不能作为纯模型遵从能力失败的证据；其他三腿没输出坐标也不证明提示没有影响。原run全文件hash保持，追加审计见 `delegation-design/prompt-audit/`，不改旧答案。

`delegation-design`早期方案写“人工参考/人工核验”不准确，实际由独立Codex子agent建立视觉参考，主控另看图核对，没有用户人工标注。原盲参考数值和封印保持；`reference-provenance-correction.json`（v1，SHA256 `d2f32042d01d1d9ae049090719732851c055c09baac8daba571370673c6fc7a8`）追加更正，新run seal必须纳入。Codex开发/审阅/协调推理开销未测，记unknown，不能填0或声称已计完整主控/人工成本。真实worker、脚本parent、准备和独立核验的费用边界分别说明。

## 直接量测回执已集成并通过真实窄验收

量测源码 `af7186e6`（隔离原提交 `e0db9299`）已合入，登记 **runtime42/domain143**。完整数字和三角面证据仍留在 raw/journal 与内容寻址报告，模型默认收到保留全部数值、方向、有效性及 caveat 的摘要；需要细部时使用已有 report JSON pointer。缺少历史 PNG 不妨碍已有数值读取，缺图明确标注；子任务完整报告仍限定自身目录。没有修改量测算法、候选、worker canonical 或停止条件。

86项唯一实现定向检查通过；独立 actual-MCP/native smoke 5通过/6.14秒、32检查全真，另报告目录写失败反例1通过/6.16秒，无阻断。不是真实模型结果。独审与源码hash见外部 `direct-measurement-acceptance/INDEPENDENT-PRODUCT-REVIEW.md`。

六条旧回执用正式 hook 反事实重放：shown字符125,103→49,082，其中也包含JSON紧凑排版收益；统一排版后净减少41.21%，按原请求出现次数加权少335,238字符。六条完整报告/分页/pointer均等值，原证据不变；补的snapshot从终态原文件重建，不能倒称历史调用时已验真。不是token/费用/耗时实测。见外部 `analysis/FORMAL-HISTORY-MEASUREMENT-PROJECTION.md`。

冻结 `c543c7a7`/domain143 的普通单parent Sol/high窄验收已完成：37.555865秒（含启动8.723814秒，另准备1.162072秒），3真实请求/2工具/6,563实报tokens。6请求/8工具/300秒上限内，一次量测后按实际报告入口读取 `/queries/0/triangle_surface_evidence`，没有worker、重试或格式修补。27协议项与17独审项全真；最终3.1899948120124404m等于两个实际XYZ的距离，三角面13字段逐值一致，报告/子树hash区分正确，161原run文件hash未变。原seal `7dfebca269e77243de3cd7d6ec1720997d1457d5ee3e7381c04a1e2974771584`及输入完整性已通过，源码冻结解除。证据在外部 `direct-measurement-acceptance/REAL-SEMANTIC-REVIEW.md`；这证明指导性数值协议可用，不是整楼质量、自主调度或用时降幅。规整定位器继续隔离审阅，通过后另登记。

## 规整共享角点诊断已集成，原输入整案待验收

最终源码 `1cc8ce00`（隔离受审 `1e6c14d3`）登记 **runtime42/domain144**。原支持线独立量化仍保持；新增定位在写 adopted/candidate 之前，检查同模板实质共享角点附近是否出现 source 原数值谓词同样拒绝的缺失/越界/重叠，聚合坐标、frame、声明路径与完整报告入口。共线冗余点和现有数值容差内的薄楔不因此拒绝。已有原模板平面谓词需先通过，避免把原始输入缺口归因规整。

该定位限于可由局部模板证明的直棱柱情形：含vertical_sections或任何实际实例显式spanning_space_ids的模板记not_assessed，仍走原完整source/topology门；未用模板不触发。通用source入口保留原跨层身份并向内部定位器传递组成上下文。没有新公开规整策略、自动焊接/填缝、放宽阈值或默认移动锚点。

最终实现150项通过/13.05秒；独立新增17项及3反例共20通过/3.12秒（有重叠，不相加）。旧C01定位4处、C02定位1处，C03完整adopted与历史逐值一致；正式MCP失败前无candidate/adopted副作用，原seed/draft保持，完整audit可通过已有report读回。默认摘要C01为6446B、C02为2445B。独审无阻断，证据在外部 `analysis/REGULARIZATION-INDEPENDENT-REVIEW.md` 和 `regularization-junction-cause.md`。尚未证明模型据此修复更快。

集成只在test_groups新增条目处发生内容冲突，已同时保留两个新模块。期间误对尚未完成的cherry-pick做了一次未发布登记；未用其启动任何模型、未提交/推送，原registry已按登记前副本逐字恢复，再以最终受审源码重新登记。错误尝试仅保留外部审计副本；最终42条runtime、143条历史domain及aliases均逐值不变，新domain144核验通过。

原输入Lee002在 `lee-acceptance-next/` 已离线prepare，单次Sol/high、48模型/240工具、无额外墙钟/token上限、worker8和原scope；将基于最终HEAD新封印后启动。评价在 `evaluation/lee-next/`：原9×16×16射线/四方口径不变，上一轮001另作逐对比较。新run未产任何质量结论。三空间合成修复仅保留备用设计，不为增加次数自动启动。

## 当前未完成

四腿独立语义已复核通过：两次识别Miele/BRICK LANE/Bicycle，两次均为6条完整可见窗带，无越界推断；6个真实native developer提示均已消歧、图像字节一致，原1223文件hash不变。见 `evaluation/facade/INDEPENDENT-VISUAL-AND-PROMPT-REVIEW.md`。

两项隔离实现均已独审并合入；domain143量测真实窄验收通过，domain144下一轮Lee002待最终封印。另在独立 `delegation-boundary/` 离线准备一题自然遮挡的立面裁图，检验可见图标与不完整文字的unknown边界，拟最多一次Sol回答、0子工具、0修格式/重试；截至本记录未启动。参考是独立Codex判断，未由用户人工标注。生产异构/异步调度和交付档位仍未实施。未更新桌面；`.playwright-cli/`原有资料保留。
