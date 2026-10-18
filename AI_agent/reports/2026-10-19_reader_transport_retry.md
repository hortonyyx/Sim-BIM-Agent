# 读图中断复测、串行与路由核验

## 范围与状态

用户在上一轮收工后要求重做中断节点，随后明确“一次一次用；Go还不行就走Paratera”。本次仅重测sm25首层平面和北立面：GLM平面、MiMo平面/立面。先前Qwen格式失败、DeepSeek平面质量失败、GLM北立面错层不扩测；无整案、人工修稿或GT输入。

首次重做的三节点已结束；收到串行指令后，所有新节点按时间顺序执行，单个节点结束才启动下一个。GLM串行Go仍断开后转同名Paratera；当日Paratera目录HTTP200、83型号，有`GLM-5.3-Flash`，没有MiMo，不将其他型号冒充同模型换线。

所有节点已终止，0正式提交、0有效草稿。本轮复测结束，不追加重试；读图主线仍保留6 Sol，下一轮按原计划转textured mesh。不能用这轮传输失败判定GLM/MiMo的建筑识读上限。

每节点900秒、32请求、128工具、32k输出、0自动重试。GLM平面high；MiMo使用服务默认思考/采样。输入固定`D:/Sim-BIM-Agent/cases/sm25-gpt-20261012/input`，明确plan_reader或elevation_reader任务及统一坐标。缓存、代理配置、domain113指导和几何契约未改。产品runtime默认8并发不变，本次实验按用户要求串行。

## 完整结果

请求/响应/工具为实际记录数；tokens仅列已收到usage部分，“+未知1”表示另有1请求用量未收到。耗时包括节点启动、模型等待和工具执行，不剔除服务延迟。

| 阶段、模型与case | 版本 | 耗时 | 请求/响应/工具 | 已知tokens | 结果 |
|---|---|---:|---:|---:|---|
| 初次并发 GLM Go F1-02 | runtime27/domain113 | 134.423s | 7/6/6 | 122,214 +未知1 | 第7请求无HTTP响应断开 |
| 初次并发 MiMo Go F1-03 | runtime27/domain113 | 75.280s | 2/1/2 | 11,348 +未知1 | 第2请求无HTTP响应断开 |
| 初次并发 MiMo Go North-03 | runtime27/domain113 | 66.674s | 2/2/2 | 26,450，完整 | 第2响应后的本地呈现审计故障 |
| 串行 GLM Go F1-03 | runtime27/domain113 | 225.682s | 4/3/8 | 66,419 +未知1 | 第4请求无HTTP响应断开 |
| 串行 GLM Paratera F1-01 | runtime28/domain113 | 16.421s | 1/0/0 | 未知1 | 首请求HTTP400 |
| 串行 GLM Paratera F1-02 | runtime28/domain113 | 508.250s | 2/1/2 | 13,507 +未知1 | 第2请求450秒超时 |
| 串行 MiMo Go F1-04 | runtime28/domain113 | 105.267s | 2/1/2 | 11,288 +未知1 | 第2请求等待53.730秒后断开 |
| 串行 MiMo Go North-04 | runtime28/domain113 | 75.486s | 1/0/0 | 未知1 | 首请求等待61.506秒后断开 |
| Paratera原请求诊断，非读图节点 | 与F1-01相同wire | 60.251s | 1/1/0 | 14,592，完整 | HTTP200；返回工具未执行 |

共8节点及1诊断，**22请求、15模型响应、22工具，已知265,818tokens，另7请求usage未知**。这只是本轮新增量，不覆盖上一轮粗测。供应商实际费用未知。串行节点的前后时间区间逐项验证无重叠；诊断在两个Paratera正式节点之间单独执行。

GLM串行Go做了8次图像观察；Paratera有效首轮与MiMo平面均只到输入/分块查看。所有节点都未调用trial/submit，未产出可验收净图或回叠。空间、开口宿主、门连通、墙厚及5/10/30cm误差分档均**未评**，不填0误差，也不人工补稿。MiMo本地修复已离线验证，但修后两次真实运行均在下一响应到达前断开，不能称修后真实读图闭环或建筑质量通过。

## 中断判断

- 初次重做：Go GLM平面第7请求等待39.227秒后`RemoteProtocolError`；MiMo平面第2请求等待52.871秒后同类失败。串行Go GLM第4请求等待62.497秒后再次失败。均已发完请求体，在等待响应头时被对端断开，没有HTTP状态或usage，远未达到450秒的单请求上限。
- 口径更正：较早进度中的“还剩约14分钟”是节点总预算口径，不能当作单请求超时。实际trace记录每次请求450秒；RoleSession按`limits.seconds / (max_concurrent_readers + 1)`设置该上限，本次900/(1+1)=450秒。没有调整此参数。
- 所以串行不能消除本次Go断开，现有证据不能归因于单纯并发超限。未收到429或明确并发上限错误。实际请求经过本机`127.0.0.1:10809`的verge-mihomo代理；尚不能区分本机代理路径、Go网关及其上游哪处关闭连接。
- Paratera首次正式请求返回HTTP400。一次严格复用已存请求字节的诊断得到HTTP200、60.251秒、14,592tokens；未执行该诊断响应里的工具。这排除了“同一请求必然因参数错误被拒绝”，不能据此确定首轮400根因。随后启动新case正式重跑，未改参数或复用旧答案。
- 正式Paratera重跑第2请求在450.013秒被客户端取消；父级状态为`child_time_budget_exhausted`，事件依次记录`timeout`及时间结算略超过预留。实际节点尚未消耗900秒总预算，原因是单请求上限。该请求尚未形成usage结算，`original_missing_usage_requests`仍为0，但`usage_complete=false`；汇总按请求/响应逐项计入1次未知，不依赖该单字段，也不修改原回执。
- 外部被动观察器只记录HTTP阶段、时间、状态及请求字节hash；不更改请求体、鉴权、代理或重试策略。已结束节点逐请求hash与journal一致，认证值不进入报告。

HTTPX对[远端协议错误](https://www.python-httpx.org/exceptions/)和客户端读超时分别定义；默认会读取[代理环境变量](https://www.python-httpx.org/environment_variables/)。本轮定位来自实际运行日志，官方说明只解释错误类型。

## MiMo本地缺陷及修复

MiMo北立面初次重做收到第二个HTTP200后，本地报`ValueError: tool context projection reference is required`。离线重放两份原始响应稳定复现，未发出真实模型请求。

原因是工具图片的`detail=original`在发送前按MiMo协议去掉，但工具呈现审计还按带hint的旧对象逐项比较，误把格式变化视为图片未回传，要求不存在的上下文裁剪记录。图片字节未变；这是runtime适配缺陷，不属于网络或建筑判断失败。

runtime28在保存工具呈现之前生成该模型可发送的图片格式，保留原始MCP结果、图片哈希及省略的显示hint。请求、呈现与图片证据一致，校验器未放宽。其他路由默认保留hint。代码提交`df8d2e58`，domain仍113，提示/工具能力指纹不变。

| 离线检查 | 结果 | 边界 |
|---|---|---|
| 最小工具图片回传复现 | 修前1失败，修后1通过 | 真实供应商调用0 |
| Go/图片/相邻runtime定向81项 | 初次75通过、6失败；修正新测试字段名并登记版本后6项复验通过 | 最终81项有效通过；初次4项是测试字段写错，2项是封印未登记，不隐藏失败 |
| 两份旧响应重放 | 修前0呈现/2工具，修后2呈现/4工具 | 第三请求被离线适配器主动拒绝，不是完整读图交付 |
| runtime28/domain113封印 | 通过，changed_modes=[] | 无桌面发布、无完整独立质量审阅 |

## 证据入口

- 初次重做：`D:/Sim-BIM-Agent/runs/cn-reader-retry-20261019`；cache同名目录有配置、启动回执、被动传输日志、审计和修前/后离线重放。
- 串行及Paratera：`D:/Sim-BIM-Agent/runs/cn-reader-serial-20261019`；cache同名目录保留配置、目录结果、诊断、逐请求审计与离线JUnit。
- [最终审计](D:/Sim-BIM-Agent/cache/cn-reader-serial-20261019/final-audit.json)、[终态及质量边界](D:/Sim-BIM-Agent/cache/cn-reader-serial-20261019/final-status.json)、[修前/后旧响应重放](D:/Sim-BIM-Agent/cache/cn-reader-retry-20261019/offline-replay-comparison.json)。两个批次快照均已置为terminal；所有节点配置、输入和运行时源码的前后hash一致。
- 修复仅合入开发主库runtime28/domain113；固定桌面仍runtime24/domain110，本轮未发布。大证据保留D盘，不声称已备份到Git远端。未清理旧实验或无关工作树。
- [上一轮粗测](2026-10-19_cn_reader_smoke.md)保留不改；新复测不替代原有失败。模型完成、正式读图提交、独立质量和稳定性分别判断，未知usage不记作零消耗。
