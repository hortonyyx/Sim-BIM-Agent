# Mesh 串行简单几何迭代后单次复测

用户授权“好，跑一次吧”后，以原三输入、GPT-6.1 Sol/high和原预算冷启动一次。正式交付candidate_09，运行55分51秒、命令58分04秒；较上轮29分41秒慢88.20%，15分钟目标未达。0人工改稿/补提示，未重跑、改产品或发布桌面。串行仍为主方向；本轮没有设计新角色或改变并发。

## 冻结范围与终态

| 项目 | 结果 |
|---|---|
| 运行代码 | `9398a15667cd9524a2c27b32ff7abea2e09371b3`，domain-v128-20261020 / runtime-v34-20261019 |
| 模型 / 入口 | single_model，chatgpt-subscription，gpt-6.1-sol，high |
| 原预算 | 48请求、240工具、每次输出预留32000、64候选；无整案时间/token硬截止，1次模型重试；image history 3、reasoning history all |
| 可选worker | 原配置上限8保持；本轮实际0 worker，全部请求由coordinator完成 |
| 正式状态 | controller exit 0；receipt completed；finalization delivered、completion_valid=true；一次finish_bim |
| 候选选择 | delivery、completion、selection一致指向candidate_09 |
| 时间 | runtime 3351.134秒；命令3484.060秒；独立数值评价166.915秒另计 |
| 调用 | 45请求、44完整响应、109工具；一次transport_error按原配置重试 |
| 用量 | 已收到usage的44响应合计8,311,620 tokens：input 8,257,813 / output 53,807；失败请求usage未知，全案用量不完整，不记为0；订阅无货币费用估算 |
| 完整性 | 原3输入哈希不变，7交付产物哈希均匹配；代码与启动版本一致 |
| 最终模型 | 179空间、1525边界、225非窗开口、46窗、225连接；无未构建开口/不支持项 |

运行于2026-10-20 15:25:20至16:23:24（新加坡时间）。输入仍为原building.glb、building.json和23f.png，尺寸及SHA与上轮保持。新配置仅改batch_id和输出目录，未扩大输入或预算。配置/输入/doctor检查在0模型调用时通过，独立就绪检查完成后才启动。没有终止其他会话。

本轮1153个3秒资源样本全部有效：最低可用提交内存17.73GiB、最低可用物理内存11.66GiB、运行子进程采样私有内存峰值3.80GiB。这是采样值而非精确峰值；没有MemoryError/inf。网络错误发生在等待响应头阶段，自身约6.312秒，重试间隔约3.4秒，不能解释整体耗时上升。

## 时间与过程

| 口径 | first | repaired | scale（上轮） | serial（本轮） |
|---|---:|---:|---:|---:|
| runtime秒 | 1723.628 | 2351.838 | 1780.578 | 3351.134 |
| 全案请求 / 工具 | 27 / 93 | 44 / 125 | 43 / 96 | 45 / 109 |
| 父模型请求 | 27 | 31 | 37 | 45 |
| 父模型时间秒 | 1439.281 | 1210.597 | 1380.205 | 2808.469 |
| 父本地工具时间秒（扣worker等待） | 203.198 | 840.591 | 184.745 | 328.579 |
| 父worker等待秒 | 9.211 | 155.877 | 69.128 | 0 |
| 父其他时间秒 | 71.938 | 144.773 | 146.500 | 214.086 |

上轮37个父请求加6个worker请求，本轮45个父请求；不能只用全案43→45判断主线往返基本未变。模型累计时间增加1428.264秒，占runtime净增1570.557秒的90.94%。本地工具也增加143.834秒。时间分桶用于定位观测到的消耗，不将一次样本的模型响应波动全部归因于harness。

首个源码几何有效候选为candidate_04，runtime第2161.317秒（36分01秒）；上轮第946.507秒（15分47秒）。candidate_01–03虽然生成文件，仍为severe/source_geometry_ready=false，不能记作有效候选。candidate_04–06通过后，较大的空间划分调整又使candidate_07–08失败，candidate_09恢复通过并正式交付。

新primitive入口实际被使用：draft_002–006包含6处单矩形primitive和2个local_baseline，但同时显式指定modelling.frames，不能用本轮证明自动框架继承路径。draft_007起改用line表达。最终primitive数量为0，仍保留8个旧rect声明并展开为108个轴对齐矩形空间；112个四顶点空间都是矩形，其中3个电梯及RoofL共4个为旋转矩形。因此“最终未用新primitive字段”不等于“没有采用简单几何”。

纯line新增约束确实触发roofShared两次、EF一次共享端点拒绝，draft_015的retail/retailUpper四个路径环也受0.159m位移检查；最终179个prism全部覆盖，not_assessed=[]。补齐旧豁免提高了检查覆盖，也产生了新的修稿需求，不能把旧版跳过检查后的速度直接当同等约束比较。

诊断读取负担上升：read_candidate_items从上轮5次/21681字符增至23次/114564字符，其中11次report、12次cells。role_evidence缺少role字段从draft_001就存在，直到draft_004才暴露，draft_005修齐；阶段内聚合并没有消除跨阶段逐个发现问题。此证据支持继续改善反馈组织与一次修改范围，不意味着应对无效中间几何强行运行所有下游检查。

20次构建/修订返回的业务结果如下。这20次在runtime层都记succeeded，不等于BIM构建有效；另有1次read pointer的工具运行失败，单列而不混入业务拒绝。

| 业务结果 | 次数 |
|---|---:|
| source_geometry_ready | 4 |
| 建筑规整拒绝 | 8（位移3、接触1、宿主歧义1、共享line端点3） |
| schema错误 | 2 |
| source硬约束 | 1 |
| overlap severe候选 | 5 |

首次build返回758.400秒，上轮419.912秒；首稿那次模型响应232.453秒，上轮176.516秒。candidate_06→07为跨层stair/core较大调整，空间从223变179，floor对象28变29，随后c07/c08出现overlap；从c06到c09共587.777秒，其中c07到c09修补207.799秒。连续核心最终正确，但这一结构判断做得晚，增加了后续返工。

已收到的8,311,620 tokens较上轮5,188,057增加60.21%，失败请求用量仍未知。797张累计请求图片包含781张有响应请求图片及16张失败尝试图片，是重复传输计数而非独立图像数量。更长上下文和更多父模型往返与耗时上升同时出现；没有同版本重复试验或消融，不能把相关性当作全部因果。

过程审计前后完整run文件清单及6137文件SHA保持，另保护原3输入、配置及readiness共5文件。全案实际尝试仅本次，没有worker请求或人工修补。

## 独立数值检查

沿用冻结评价脚本和相同九视角、每图16×16、合计2304射线；直接使用模型保存的mesh_frame，未拟合或修正。serial自选yaw=-46.4°，scale为-46.6°，均零平移，因此差异包含执行器自选坐标系变化。每次的有效样本集合不同，另报告共同样本。

| 轮次 | 有效样本 | ≤5cm | ≤10cm | ≤30cm | 平均绝对深度差 |
|---|---:|---:|---:|---:|---:|
| first | 1238 | 61.712% | 76.414% | 92.084% | 0.235295m |
| repaired | 1154 | 89.428% | 92.374% | 94.281% | 0.184181m |
| scale | 1227 | 33.007% | 77.099% | 94.051% | 0.258243m |
| serial | 1201 | 58.035% | 86.928% | 94.172% | 0.240831m |

scale与serial共同1190射线：≤5/10/30cm从34.034%/77.479%/93.866%变为57.647%/86.807%/94.118%，均差0.263326→0.242611m；874条改善、233条变差、83条不变。共同主体1063条均差8.980→7.176cm，≤10cm从80.715%到90.875%。共同屋顶127条均差1.71579→1.67265m，>30cm由51到48条，仍有大偏差。

四轮共同1121射线的均差first/repaired/scale/serial分别为0.225144/0.170356/0.268431/0.247317m；本轮优于scale，但没有回到repaired的固定射线一致性。不能单凭5cm比例判Lite成败，也不能把深度指标当成整楼质量排序。

本轮有效1201/2304（52.13%）；neither_hit 1019、source_no_hit 37、edge_sample 47。相较scale，source_no_hit仍37（其中屋顶28）；edge从21增到47，37条paired转edge、11条edge转paired造成有效净减26。无命中和边界样本不记作零误差；有效比例不是外表面积覆盖率，重复视角不是独立构件样本。

九视角均成功，0评价失败、0模型调用。源BIM经公共kernel独立执行重建，validation/precision通过；179空间、1525边界、225非窗开口和46窗ID及结构均保持。重建规范SHA为`1dd80444889b26f19b594708ccbdd022617f50c8cf2cff5ae6ff50d87ac6a6ad`，与保存源不同；逐顶层仅generation和source_model_sha256变化，评价provenance不同，其余相等。源码重建是独立执行，不是独立验证算法。

数值评价196个受保护文件前后哈希一致：4评价脚本、38基线证据、3输入、140源码依赖、10终态/交付依据、1协议。这不是整个run树的文件数量。

5/10/30cm是保存源BIM与原mesh的同视线深度差，不是窗端点误差；透明贡献、遮挡、轮廓边缘和纹理开口有方法边界。z=75.523m仅为沿用的原mesh诊断分区。未知内部没有GT；23F图无尺寸标尺，2897sqft为可租面积，不作为外轮廓定标。

## 实际图与空间语义

独立检查按本轮实际服务层和核心ID绑定，没有把旧115门数当作固定答案。23个服务层的69个电梯门、46个楼梯门全部满足真实双侧宿主、各自宿主面内包含、接触关系、唯一connection及本层Z。5个连续核心的水平边界仅在0和75.6m，其他空间水平面也未穿入核心内部。225个连接状态均为unknown，屋顶4门是推断外门，不能当作已恢复楼梯到屋顶的连通证据。

三电梯实际为2.1×3.5、2.1×3.5、2.0×3.5m、四角均为90°的旋转矩形，边方向沿-24.6°，两楼梯也是矩形。但矩形化不等于位置和比例准确：保存23F回叠可见井道前边伸入原图公共前室，门线也随之前移。这属于局部空间还原问题，应保留整改，不被几何门禁/连接合法性通过掩盖；不从无尺度图换算真实米制偏差。

固定两侧屋顶实际回叠中主要阶梯体量大致对应；前场HVAC、栏杆、细墙、竖片按声明省略，许可的几何简化与23F前室侵占分开评价。屋顶仍有米级残差，不能称精确复原。顶视ray[862,337]约3m回退位于轮廓临界：source点[8.740621,-2.506543]仅越出RoofR的y=-2.5前缘约6.5mm，使命中从78.6m转为75.6m；该单点不足以证明漏掉整块主体体量，也不据此消除其他屋顶误差。

本轮不重复viewer按钮/剖切交互测试，viewer实现未改；不将此前交互结果当成本轮新验收。完整楼层编号、暗面外窗、屋顶设备细节和其他层真实内部仍不能由本案输入确认。

视觉/语义独评保护原run 6137文件及3输入，共6140文件、780803303字节，前后无新增/缺失/改变。实际检查原23F、最终23F回叠、重建O21平面及原mesh/最终屋顶与整栋固定视角，0模型调用。O21对应的重建图片为floor_029.png；图头F25为内部高度组名，不能直接当作实际25层。

## 证据与入口

- [最终BIM](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001/bim/candidate_09/viewer.html)、[正式交付页](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001/bim/delivery.html)
- [终态校验](D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-serial-20261020/terminal-integrity.json)、[controller](D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-serial-20261020/controller.json)、[资源采样摘要](D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-serial-20261020/resource-summary.json)
- [独立过程审计](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/process-comparison/ANALYSIS.md)、[核验事实](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/process-comparison/verified-facts.json)、[完整run保护](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/process-comparison/protection-check.json)
- [数值判读](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/results-01/independent_numeric_verdict.json)、[完整四轮对照](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/results-01/comparison_baselines.json)、[冻结评价协议](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/numeric_protocol.json)
- [数值执行与保护](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/numeric_execution_result.json)、[源重建](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/results-01/source_recheck.json)
- [23F核心区原图/最终回叠](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/visual-review/23f_core_comparison.png)、[独立整栋回叠](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/results-01/overlays/view_04_az135_el15.png)、[顶视回叠](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/results-01/overlays/view_09_az000_el90.png)
- [独立视觉/语义报告](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/visual-review/REVIEW.md)、[真实门/核心检查](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/visual-review/semantic_audit.json)、[O21平面](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/results-01/source_plans/floor_029.png)、[屋顶两侧对照](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-serial-20261020-001-evaluation/visual-review/roof_side_comparison.png)

source规范SHA为`616ba6f45a6a66f9c4a37eb23d99d52231c74311bfc8ff069a936d871c88a332`，source文件SHA为`791660f89bba4cbae70ab0f385748597b404912174cb9350169e02a3851f0092`，二者口径不同。events SHA为`1b153d6d28a616441141227a4eb46320cddb82391ed28f59bc55bdffa55ad80c`。

## 后续边界

本轮只完成授权的一次真实测试、独立验收和文档记录。提速未成立；基本形状及关系表达仍是用户确认的方向，不因本例变慢自动引入角色体系。下一轮建议优先讨论：

1. 首稿先稳定主体、共享墙和贯通核心关系，减少有效后大改；矩形化同时校验比例、前室与门位，不把“直角正确”当空间关系正确。
2. 将可独立检查的格式/引用问题尽早集中返回，几何错误反馈明确同一修改影响的宿主和相邻空间；保留依赖阻塞边界。
3. 减少重复读取整份report/cells与长历史传输，优先当前错误及相关几何的紧凑反馈。按证据检查参数表达为何从primitive切换，不强制单一表示。
4. 先用相同质量目标验证最小串行改动；需要并发时，再共同讨论问题拆分、消费时机和角色，不预先固定体系。

上述为基于本次证据的建议，尚未实施；未自动修模型、改预算、追加追问或启动下一案。本次仅文档变更，保存原run、全部独评和输入；桌面保持原安装。报告经过程、数值、视觉/语义三方独立复核，修正“全部有效射线”被误称为主体及“矩形内角90°”的歧义。文档diff检查通过，没有为文档变更重跑产品测试。收尾C盘45.28GiB、D盘115.48GiB；没有创建工作树或待清理服务。
