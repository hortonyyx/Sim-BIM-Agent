# 国产 Flash 读图员粗测与收工交接

2026-10-19，用户在前次收工后授权先测一张平面、一张立面，明确加入最新 DeepSeek Flash，并要求优先完成 Go/Paratera 已有候选。本轮已全部终止，未组装、未跑整案、未启动 textured mesh 或生成服务。结论：DeepSeek 与 Qwen 北立面通过本张图核对；平面没有获得可替代 6 Sol 的成功结果。新增 Go 候选 MiMo 的协议已进入图片/工具循环，但交付被传输中断，能力尚未评定。

## 范围、版本与参数

- 原图：sm25 `1f_view.png` 与 `North_view.png`；每模型各一个独立平面/立面任务。平面/立面收到各自图种、角色包及共用整楼包围盒原点；输入目录仍有六张原件，读图节点按分配只读本图。
- 冻结 domain113。原三组及 GLM Go 使用 `8ab9a952`、runtime24；MiMo 初次使用 `fb1b5c4f`、runtime25，兼容修正后使用 `2c4ff322`、runtime26。domain、墙厚、网格、图纸内容、缓存参数均未修改。每个运行的源文件/配置/输入前后哈希一致。
- 每图保护预算：900 秒、32 模型请求、128 工具调用、32,000 输出 tokens、0 自动重试；不是能力上限测试。各独立节点并行，最初六节点同批发起，未降低 runtime 默认8并发。MiMo兼容修正后仅各重启一次新case ID。
- Qwen：Paratera `Qwen3.8-Flash`，thinking=true、temperature=0.7，无未支持的effort；DeepSeek官方 `deepseek-flash/high`；GLM原订阅平面high/立面medium，原入口失败后明确改 Go `glm-5.3-flash/high`；MiMo Go `mimo-v2.6-flash`服务默认思考，无温度/effort覆盖。
- 每次启动前说明角色、模型、预算及范围。GT/旧答案不进入工作模型；只有终止后才进行数值比较和主助手看原图、净图、回叠。无运行中人工提示、修稿、补对象；本次不是另设审阅者的完整独立审阅。

## 主要结果

时间是启动器端到端时间，含节点初始化和收尾；tokens仅列供应商实报输入+输出，包含缓存输入。`已知 + 未知`不能当作完整用量。

| 模型/入口 | 图 | 交付与质量 | 时间 | 请求/响应/工具 | 实报 tokens |
|---|---|---|---:|---:|---:|
| Qwen3.8-Flash / Paratera | F1 | 工具参数JSON不合法；无有效稿，质量未评 | 864.622s | 6/6/10 | 417,329 |
| Qwen3.8-Flash / Paratera | North | 正式提交；本张数值及实际图通过 | 408.455s | 11/11/20 | 448,368 |
| DeepSeek V4.1 Flash / 官方 | F1 | 正式提交；实质质量失败 | 403.340s | 19/19/27 | 1,414,238 |
| DeepSeek V4.1 Flash / 官方 | North | 正式提交；本张数值及实际图通过 | 172.350s | 8/8/19 | 307,235 |
| GLM-5.3-Flash / Go | F1 | 第3请求传输中断；无有效稿，质量未评 | 76.344s | 3/2/4 | 已知30,273 + 1请求未知 |
| GLM-5.3-Flash / Go | North | 正式提交；楼层归属错误，未通过 | 170.729s | 11/11/19 | 450,486 |
| MiMo-v2.6-Flash / Go，修正后 | F1 | 第2请求传输中断；无有效稿，质量未评 | 107.178s | 2/1/2 | 已知12,073 + 1请求未知 |
| MiMo-v2.6-Flash / Go，修正后 | North | 第3请求传输中断；无有效稿，质量未评 | 77.255s | 3/2/4 | 已知20,995 + 1请求未知 |

失败前置尝试同样保留：GLM原订阅F1首请求HTTP429/1113、North首请求HTTP400/1210（分别21.454s、20.807s）；MiMo初版两图首请求HTTP400（21.208s、20.718s）。均无工作模型响应、无工具执行、usage未知。GLM North的400具体参数原因未证实。GLM Go及MiMo修正后的失败链均为 `httpx.RemoteProtocolError → httpcore.RemoteProtocolError`，没有HTTP状态或服务端usage；无法进一步定位代理/服务端的具体责任。

MiMo另做2次明确的单请求协议诊断：原请求重放400、按官方格式改参数后46.913s读取超时，均无usage，不计作有效读图或免费请求。粗测12节点合计67请求/60响应/105工具，已知 **3,100,997 tokens**，另7请求usage缺失；含诊断共 **69请求、9请求usage未知**。从首节点发起到最后节点终止约20分10秒，含MiMo接入修正间隔，不是单图读图耗时。最初六节点批次868.632秒。

## 质量证据

### DeepSeek F1：不能通过

原图西侧凹进部分是室外，输出新增 `hall-west` 及外墙，把两扇西窗移到虚构外边界；可见的西入口、东入口未记录。工作模型把外门自行解释为“out of scope”，没有收到这种排除授权。自审文字声称没有漏造对象，与实际净图/回叠相矛盾。

- 空间：参考14、产物15、匹配14；多出室外填成的室内空间。
- 房间边界5/10/30cm分档：`5 / 3 / 6 / 0`，匹配空间最大0.22m；这个数字不包含对多出空间的豁免。
- 开口：参考31，产物31（16窗/15门），仅匹配29；宿主匹配26，门连通匹配13/16。总数相同不能掩盖漏项和错宿主。
- 匹配开口沿墙误差分档 `10 / 6 / 6 / 7`，最大1.922m；垂墙分档 `11 / 0 / 16 / 2`，最大5.136m。完整原始发现保留，未放宽门窗容差。
- 25/25墙厚已填，19段inferred、6段observed；没有独立逐墙厚度GT，不能称测厚准确。

查看：[原图](D:/Sim-BIM-Agent/cases/sm25-gpt-20261012/input/1f_view.png)、[厚度净图](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/deepseek-flash/evaluation-f1-01/reader_view_2.png)、[实际回叠](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/deepseek-flash/evaluation-f1-01/reader_view_1.png)、[完整评价](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/deepseek-flash/evaluation-f1-01/quality.json)。

### 北立面：两张通过，一张楼层错误

| 产物 | 8窗/楼层/轮廓 | 实际回叠水平最大误差 | 窗竖向最大误差 | 标高最大误差 | 本张判断 |
|---|---|---:|---:|---:|---|
| DeepSeek | 8/8，0/3.6/7.2m正确，轮廓完整 | 1.04cm | 1.28cm | 1.36cm | 通过 |
| Qwen | 8/8，必需标高正确，轮廓完整 | 0.86cm | 1.28cm | 1.36cm | 通过 |
| GLM Go | 窗8/8，但3.6m线写成floor_id=F1 | 2.59cm | 1.21cm | 3.6m（楼层语义错误） | 不通过 |

DeepSeek和Qwen的实际回叠全部窗/标高/轮廓落在≤5cm档。GLM采用世界坐标的窗定位为6个≤5cm、2个5–10cm；实际回叠8个≤5cm，但不能用窗位置正确覆盖楼层归属错误。Qwen的ground和F1_level都在0m，是同高度两个语义记录，并未造出额外楼层；净图标签略重叠，记录为显示小问题。

查看：[DeepSeek净图](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/deepseek-flash/evaluation/north-01/reader_elevation.png) / [回叠](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/deepseek-flash/evaluation/north-01/reader_overlay.png)；[Qwen净图](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/qwen-flash/evaluation/north-01/reader_elevation.png) / [回叠](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/qwen-flash/evaluation/north-01/reader_overlay.png)；[GLM净图](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/glm-go/evaluation/north-01/reader_elevation.png) / [回叠](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/glm-go/evaluation/north-01/reader_overlay.png)。

Qwen F1最后一响应正常返回 `finish_reason=tool_calls`，其中`trial_plan_bim`参数8,884字符，在第5,721字符JSON解析失败；不是网络未收到响应，也没有被标为输出截断。最后一响应实报42,834输出tokens（含38,522思考tokens），请求cap为32,000；服务端cap对思考的实际语义未验证，不据此猜测终止原因。没有尝试人工补JSON或继续建模。GLM/MiMo无有效稿的节点不运行几何验收，不能按0误差或模型能力失败记分。

## 效率、缓存与候选取舍

| 已结束节点 | 模型等待 | 端到端占比 | 输入缓存命中 | 主要行为 |
|---|---:|---:|---:|---|
| Qwen F1（无稿） | 839.952s | 97.1% | 71.6% | 首稿前9次观察，最后工具格式失败 |
| Qwen North | 382.283s | 93.6% | 82.4% | 14次像素剖面、1次试稿 |
| DeepSeek F1 | 366.594s | 90.9% | 95.9% | 5次trial调用、2次提交；未发现室外误封闭 |
| DeepSeek North | 146.174s | 84.8% | 88.2% | 11次像素剖面、1次试稿 |
| GLM Go North | 146.597s | 85.9% | 80.8% | 3次试稿、2次提交，仍有标高错层 |

命中率按供应商`cached/input`计算，缓存参数未动。当前主要损耗是模型观察/思考/返工和无法自审出实质错误；本地工具实际执行仅0.8–4.6秒。高缓存不代表低总tokens或正确。不同供应商token口径及思考策略不同，这不是严格同参数模型排名；Go是订阅用量，不能将未知费用写成0。

候选决定：

1. **6 Sol保留主线。** 上一轮F1 283.349s/97,852tokens、North 66.749s/27,705tokens且质量通过，是既有harness系列样本，不是本轮同步重跑；本轮没有证据证明这些Flash候选在质量约束下更快。
2. **DeepSeek优先保留为立面候选。** 单图约2分52秒且通过；平面实质失败，不能整包替换读图员，更不能推出整案能力。
3. **MiMo是Go里值得补测的新低档候选，现已补入。** 原生图片与工具能力有官方依据，修正后原始图片输入取得响应；工具返回图片已进入后续请求，但该请求传输中断，尚未完成图片回传后的响应闭环，也未验证建筑质量。当前停止重试，不把外部中断当作模型不能读图。
4. Qwen立面可作后备，但本次慢且平面格式失败；GLM有可用几何感知，当前错层与连接失败尚未满足替代条件。先不为了它们堆提示词、JSON补丁或扩大测试。
5. 豆包Seed2 Lite/Step3.7 Flash未出现在已配置Go/Paratera目录，也无专用凭据，未调用。HY3、LongCat-2.0在当前Go元数据入口只支持文本，排除直接读图；更高档的既有候选暂不扩测。

当日只读目录：Go 37型号、Paratera 83型号、DeepSeek `deepseek-flash`/`deepseek-v4-pro`，均HTTP200。官方[Go型号和协议](https://dev.opencode.ai/v2/docs/console/go/)、[MiMo原生图片及请求格式](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/multimodal-understanding/image-understanding)、[MiMo思考参数](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/other/deep-thinking)、[DeepSeek型号说明](https://api-docs.deepseek.com/quick_start/pricing/)。DeepSeek当日官方将`deepseek-flash`对应为V4.1-Flash-1017；远端别名不等于独立权重证明。MiMo公开MOPD修复并不证明Go当前部署恰为该权重。

## 本轮必要接线与离线检查

只增加MiMo Go准入/profile及标准协议映射，不修改domain。runtime25初次使用现有Chat字段，首请求400；runtime26按官方格式使用`max_completion_tokens`，不传采样参数，去掉本地图片`detail`提示，保留原图字节并对消息投影登记来源。实际发送字节、源引用、图像哈希和usage继续可核对。一次诊断同时规范两个字段，未单变量证实原400究竟由哪一项触发。

| 检查 | 结果 | 时间/范围 |
|---|---|---|
| 初始MiMo准入Go定向检查 | 29通过 | 5.21s，0供应商调用 |
| 规范参数后的Go检查 | 初次28通过/1失败；补入带detail的真实图片fixture后29通过 | 最终5.33s；首次失败是原fixture不含待验证hint，不是放松断言 |
| 共用runtime相邻模块 | 61通过/3失败 | 12.25s，0供应商调用 |
| 同3失败在未修改基线8ab9a952重跑 | 同样3失败 | 1.29s，61未选；证实是既有问题，未扩修 |
| 版本登记/封印 | 测试树runtime26通过；最终主库runtime27/domain113通过，changed_modes=[] | 未关闭哈希，未改变指导/工具能力指纹 |

既有3失败：`StopIteration`被异步边界转换成`RuntimeError`而测试期待原异常；两项旧context-limit终态断言期待旧状态，实际为`context_recovery_required`。保留记录，不能声称全套测试全绿。初始启动器路径保护拒绝和一次评价脚本参数审计兼容修正均在本地完成，没有因此额外发出模型请求或改几何基准。固定桌面仍runtime24/domain110，本轮未发布。

合入主库时，Git按已有`eol=lf`将`model_profiles.json`从测试树405个CRLF转换为LF，首次主库封印核验因此拒绝。已确认JSON及规范化字节完全相同；旧runtime25/26记录不修改，主库实际LF字节另登记runtime27并核验通过。没有据此复跑工作模型。

## 证据与恢复入口

- 批次运行根：`D:/Sim-BIM-Agent/runs/cn-reader-smoke-20261019`，保留全部尝试，不覆盖旧case。
- 缓存根：`D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019`；包含配置、目录响应、前后哈希、启动日志、逐请求/工具审计、独立几何评价、JUnit。
- [汇总及完整用量边界](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/batch-audit-final.json)、[主助手图像核对及文件哈希](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/visual-review.json)、[Qwen格式失败证据](D:/Sim-BIM-Agent/cache/cn-reader-smoke-20261019/qwen-flash/f1-01-response-format-audit.json)。评价文件中的`pending_independent_inspection`是数值程序写入的原始状态，不追改；图像结论另存`visual-review.json`。
- MiMo的原运行脚本指向临时工作树；清理后如需恢复测试时字节，从`2c4ff322`恢复隔离工作树，并按前置回执恢复`model_profiles.json`的CRLF。获准新测优先使用封印已通过的当前主库及新case ID，不原地覆盖旧启动回执。其余运行源码为`8ab9a952`。大证据留D盘，不声称已备份到Git远端。
- 读图员及国产替代测试到此收工，不追加全图、全案或重试。下一轮按用户计划盘点并搭建textured mesh harness；既有入口、照片原型边界和待讨论细度见[上一轮收工交接](2026-10-18_closeout_model_candidates_and_mesh.md)。
