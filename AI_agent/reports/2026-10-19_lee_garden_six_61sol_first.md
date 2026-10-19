# 利園六期：6.1 Sol 首轮完整混合输入测试

2026-10-19。用户明确授权用已准备的原 textured mesh、23/F 平面和中文说明，执行一次 GPT-6.1 Sol 完整测试。正式运行已自主交付 `candidate_04`，源 BIM、查看页和当前整栋范围声明有效。运行 1723.628 秒（28分44秒），整条命令含启动/退出 1767.218 秒（29分27秒）；超过本案5–10分钟、常规建筑15分钟的速度目标。没有人工补提示、改候选或额外模型复跑。

## 范围与冻结配置

- 代码 `3e4aa188d20b5489ce35ebbd07cdf56f3b5cc4d7`，**domain-v121-20261019 / runtime-v31-20261019**。本轮不改程序、不重新登记版本、不发布桌面；固定桌面仍runtime24/domain110。
- 普通 `single_model`，`chatgpt-subscription / gpt-6.1-sol / high`，每响应最多32000tokens；可选同模型只读worker最多8个，父子共享48模型请求/240工具，无整案时间/token硬截止。保留原重试配置1，无模型/provider回退。
- 仅从准备配置派生模型名、独立输出目录和批次标签；原6 Sol配置保留。预检通过，真实启动授权单独记录，未把预检本身当用户授权。
- 模型只收到准备好的三个文件：原`building.glb`、`auxiliary/23f.png`、`building.json`。3份输入运行后哈希均不变，详见准备报告和`postrun-integrity.json`。审计侧照片、既有观察、人工分析与GT没有作为答案输入。
- 建筑单体、办公为主且含原低层商业，公开高度82.6m。23/F是唯一有直接平面依据的楼层，无标尺；可租面积不是外轮廓标定依据，其他层复用、楼层编号及屋顶用途必须区分推断。

## 实际运行与交付

| 检查范围 | 结果 | 耗时/调用及边界 |
|---|---|---|
| 原配置派生、正式预检、输入完整性 | 通过 | 0模型；3输入哈希保持 |
| 正式6.1 Sol整案 | completed，exit 0 | 27模型请求、93工具；正式1723.628秒，整条命令1767.218秒 |
| 最终交付 | candidate_04，completion_valid=true，delivered | 1次finish；整栋范围是绑定当前源/回叠的模型声明，不是独立保真认证 |
| 真实只读worker | 2个均未启动推理 | 总token预算不足；0模型/0工具；主模型自行继续完成 |
| 最终源几何 | validation pass | 184空间、1564边界、252开口（88窗/164门）、164连接；unbuilt/unsupported均0 |
| 独立固定九视角数值验收 | 9/9完成，1238/2304有效配对 | 主评估产物区间85.454秒，不含启动/初始哈希；0模型；有效样本92.08%深度差≤30cm，非整栋保真通过 |
| 独立实际viewer与原件视觉验收 | 主体/主要正面窗带/23F主关系首轮可用 | 完整垂直交通拓扑不具备；屋顶/基座局部精度与HUD楼层语义保留问题 |

运行：`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001`。实际交付 [delivery.html](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001/bim/delivery.html)、[viewer.html](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001/bim/candidate_04/viewer.html)、[source_model.json](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001/bim/candidate_04/source_model.json)。源规范哈希 `d599bfd1ff0fde44cf3abf9505595d12ab38f7534ae676ae9e8fe4c6eb48546d`。

模型将主体解释为2个商业层段加21个办公层段，另建5个连续核心和7个屋顶/设备高度范围。源中35个floor groups不是35个实际楼层。23/F暂对应72.4–75.6m，3.2m重复层高、其他层核心复用及实际编号仍是有标注的假设，未因完整交付转成事实。

88个window对象表示源中的窗带/窗组（84个办公窗带及转角组、4个商业组），不是88块玻璃或88扇实际单窗；细窗梃没有逐根建模。

27/27响应完整，0截断、0重试、0provider失败或缺失usage。供应商实报输入4,244,102、输出59,075，合计**4,303,177tokens**；缓存输入38,912，推理计数21,277已包含在输出中，不重复相加。订阅路径费用未知，不能把null当免费或估成API账单。93次工具执行92成功/1异常，另有业务拒绝。完整口径见`run-statistics.json`。

模型请求累计1439.281秒（约24分钟，占runtime83.5%），工具含子等待212.409秒，其他71.938秒。最长第21请求264.125秒，中位请求25秒。工具分布：观察35、查询25、构建/修订5、审计26、交付1、委派1；次数本身不证明冗余，下一步应结合实际对象变化审视观察、修订和上下文成本。首次可用candidate_02在启动后861.725秒（14分22秒）才保存，随后又检查/重建/修订至最终交付。

`delivery.json.generation_status=in_progress`是finish时点的handoff快照；最终receipt已校验并复用该handoff，`completion_valid=true`、`finalization.status=delivered`。查看页按`handoff_saved`显示“已选定并保存 BIM”，并非仍在生成。不能只取这一快照字段判断最终状态。

## 角色行为、失败与恢复

1. 调度员尝试把平面对应和楼层/立面观察交给两个只读worker，分别只给7000/6500总tokens，但子任务继承32000输出预留。公共预算检查在发送请求前拒绝，两子均`child_token_budget_exhausted`、0请求/0工具；不是provider失败。主模型随后直接完成观察、量测、建模及检查。当前未验证真实worker并行带来收益。
2. 两次构建未通过：首次23/F顶部与屋顶机房产生0.1m宽接触条带，被`source_hard_constraints`拒绝；第二次candidate_01为`severe`诊断稿，`G:door:left_access`缺完整两侧宿主，`source.opening_unbuilt`且`saved_candidate=null`。模型修为candidate_02后成功。四个目录不等于四次成功保存，真正保存成功的新稿共3个（02/03/04）；业务拒绝与runtime工具执行成功分别记录。
3. `inspect_parametric_plan(candidate_02)`入参合法，但修订稿没有`parametric_plan.json`，工具直接读取引发`FileNotFoundError → ToolError`。这属于工具适用性处理缺口；模型继续工作，后续对参数直建的candidate_03调用成功，不能称原缺陷已修。
4. 模型重新建立candidate_03，再修订为candidate_04，保存观察/推断/简化和未解事项，并自行完成finish。过程中没有开发代理修改源几何、换图、补答案或放宽检查。

## 独立质量与未覆盖

评估使用实际candidate_04源、冻结原mesh和原`mesh_frame`（yaw=-46.6°、平移0），9个预先固定相机×16²图像网格，不拟合模型。所有9图成功；2304射线中1238有效（53.73%），1017双背景、20原mesh有命中而源无、2反向、27边缘样本。有效率是网格采样口径，不是面积覆盖率；本次稀疏点未落到透明无效区域，不等于透明细部已完整核验。

| 同视线绝对深度残差 | 有效样本数 | 有效样本占比 |
|---|---:|---:|
| ≤5cm | 764 | 61.71% |
| >5–10cm | 182 | 14.70% |
| >10–30cm | 194 | 15.67% |
| >30cm | 98 | 7.92% |

累计≤10cm为76.41%、≤30cm为92.08%；中位1.56cm、均值23.53cm、最大16.714m，保留大值，不截尾美化。后验按原mesh命中高度解释（不替换总分）：主体8.4–75m有994有效样本、均值4.44cm，12点略超30cm（最大30.275cm）；基座98点中35点>30cm，上部146点中51点>30cm。最大深度差对应斜视射线从原屋顶z79.926m命中到源23/F顶75.6m，涉及第一命中表面变化，不能把16.7m称为欧氏位置偏移，也不能当零误差忽略。

全部98个超30cm样本另做命中三角、材质、源宿主和视角绑定：屋顶51个中45个为OPAQUE，不能统归栏杆/透明细节；16.714m对应原设备侧面到源屋面的换配，10.878m对应原屋顶斜面体量到后方源墙。基座最大−2.800m对应商铺退进被源平墙跨过，+2.688m对应原柱/外凸首面换配，均是应保留的局部几何差异。主体12点均落于同一L2井壁/同一视角，射线差约−30.11至−30.28cm，折算法向投影约11cm，属于局部墙位偏置及斜视放大，不是全塔错位。见评估目录`outlier_audit.json`及`outlier_contact.png`。

评估另在独立目录重建保存的proposal：184空间、1564边界、164门、88窗均0增删改，楼层、宿主、接触和连接一致。公共precision检查通过；130个正交空间检查了轴向规则，54个非正交空间的轴向最小净宽未评。这里复用了公共kernel，是独立执行检查，不是第二套独立几何算法。原输入、receipt、候选及交付受保护文件哈希保持，额外模型0。

独立审阅者实际打开viewer、旋转并选择23F所在高度组，核对原件、最终回叠、平面及门连接：主体折角、主要正面窗带和内凹基座入口可接受为首轮轻量重建，23/F主办公区、3部电梯、2处楼梯和3组卫生间的主要关系基本对应。保持以下限制：

- 中间O01–O20与R1没有电梯层门，仅G与23F共6个lift door；模型已披露，但仍不能称完整垂直交通拓扑。各办公层楼梯连接与连续核心存在，不等于所有垂直交通关系齐全。
- 屋顶主要体量存在，设备和部分实体表面仍缺省，差异并非全部只是细栏杆；基座亦有超30cm及漏命中样本。整栋几何精度未获通过结论。
- 源23F在查看器为F28，HUD显示`floors 35`，混淆高度分组与实际楼层。源假设文字已有解释，但界面语义仍待修正。
- 23/F未标尺寸，外轮廓局部与mesh拟采用轮廓并非严格重合；其他层内部是明示模板推断，不是逐层读图结果。暗面开口状态保留未知。

以上是运行结束后的独立评估，没有向工作模型反馈或修改交付。

独立视觉审查约10–15分钟（审查者估计，非模型运行计时），已保存[正式记录](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001-evaluation/visual-review/independent_visual_review.json)。11项关键冻结输入/运行/交付文件前后哈希一致；临时本地viewer服务与浏览页已关闭。独立评估耗时不计入正式运行28分44秒。

直接查看：[主体回叠](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001-evaluation/results-01/overlays/view_08_az315_el15.png)、[23/F源平面](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001/bim/candidate_04/plan_F28.png)、[实际viewer截图](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001-evaluation/visual-review/viewer_overview.png)。查看器F28对应源23F高度范围，界面中的F编号/35分组仍不能当真实楼层编号。

不将同视线depth residual称为三维点到面精度或窗端点准确率；固定图像网格命中比例也不是立面面积覆盖率。23/F无尺寸标尺、其他内部无GT，不能报告全楼房间恢复准确率。屋顶细栏杆/设备、暗背面可能开口、真实楼层编号、全部细部与材料/EnergyPlus不在已证实质量范围内。本轮不另做真实恢复/重复性测试，不由单例交付外推稳定整楼能力。

## 证据与后续

- 准备来源、输入哈希与范围：[首测准备](2026-10-19_lee_garden_six_mixed_ready.md)。
- 派生配置、预检、授权、控制器、完整性和失败诊断：`D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-first-20261019`；模型参数配置在其父目录`run-config.61sol-first.json`。
- 原始请求/工具/恢复点/receipt及实际产物：上述run目录。仅评估产物：同级`lee-garden-six-mixed-61sol-first-20261019-001-evaluation`，不改模型run。
- 终态用量/时序/失败/7件交付哈希核验：审计目录`final-run-summary.json`、`run-statistics.json`。固定9视角、结构重建、异常点明细：评估目录`results-01/summary.json`、`source_recheck.json`、`structure_and_residual_diagnostics.json`、`outlier_audit.json`。
- 独立数值结论：[FINAL_NUMERIC_REVIEW.md](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-first-20261019-001-evaluation/results-01/FINAL_NUMERIC_REVIEW.md)；评估文件哈希清单同目录`evaluation_artifact_hashes.json`。原run与评估侧产物分别保留。
- 本轮没有修改程序；两个已定位的分工/工具缺口及观察、修订成本留作下一步优化输入，不静默修复后替换本轮结果，不自动启动第二轮。
- 评估侧另发现只读统计helper `project_saved_run` 的本地`ReadOnlyStore`缺少`resolve`，与当前request accounting接口不匹配。外部统计脚本改读stop事件锚定且SHA验证的最终receipt，并与27条provider usage求和相符；公共runtime未修。这是运行后的统计入口缺口，不计为模型运行失败。
- Git只保存短报告和状态索引；原始大输出、图片与评估数据在D盘本机，未宣称远端备份。

本轮仅更新4份项目文档，链接与`git diff --check`通过，未因报告修改重跑pytest。没有新工作树，既有Go工作树未动。收尾磁盘剩余C 34.54 GiB、D 119.72 GiB。
