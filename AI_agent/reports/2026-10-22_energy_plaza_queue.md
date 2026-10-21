# 幸福中心：补齐 Paratera 接线并继续排队

用户于 2026-10-22 授权继续独立冷启动测试，最多同时两个整案，非 GPT 优先；Opus 5.5 暂停，由 Qwen 旗舰替代。Kimi K3、Qwen 明确指定 Paratera，并坚持原厂次高档。最新追加指令将 Astra 第二次留待以后，不进入本轮队列；原八次计划中本轮安排七次。原始输入、任务 scope、BIM 主要交付、80 请求 / 400 工具共享预算、无总 token 限额、90 分钟总经过时间保护、原自动重连和 worker 权限保持。

本记录位于隔离接线分支；原主库 `beb8ee01` 的运行不受修改。准确接线提交、版本、准入和启动事实以后续外部 `seal.json`、`status.json`、原生 receipt 为准。

接线版已登记 **runtime40/domain137**。runtime 修改仅涉及 provider 参数、模型资料和已声明共享窗口的上下文预留；domain 新号仅因隔离检出中既有 `runtime_mesh_continuation.py` 的 CRLF→LF 字节差异，业务源码无变化。旧版本记录逐项保持不变。前三次仍按 runtime39/domain136 报告，不能宣称七次字节版本完全相同。

定向验证：provider 契约与相邻图片续接 16 项通过；新增共享窗口及 UTF-8 边界 18 项通过（范围有重叠，不加总成总测试数）。一次六模块回归 171 通过 / 6 失败，其中四项以旧 HEAD 原文重放确认既有，一项 Windows 默认读文件编码以 UTF-8 复验通过，一项未登记版本哈希在登记后单独复验通过。根审与交叉审发现的 Kimi cap 元数据、旧默认 profile 恢复形状问题已修复并定向验证；无剩余启动阻断。真实接线仅上述四次微型请求，质量评估未开展。

## 已有运行与人工反馈

- Astra 第一轮：原生完成并正式选择 candidate_05，原生耗时 1810.265 秒；主模型及 worker 共 26 请求 / 58 工具。
- DeepSeek Flash 第一轮：原生完成并正式选择 candidate_02，原生耗时 879.881 秒；33 请求 / 58 工具。
- Flash 第二轮：2026-10-22 00:24:34（Asia/Singapore）从原冻结主库独立启动，原生 653.026 秒后完成并正式选择 candidate_02，26 请求 / 48 工具。输出 `D:/Sim-BIM-Agent/runs/energy-plaza-deepseek-flash-r2-20261022-001`；配置、输入与候选未人工修改。

用户观看 Flash 第一轮交付后反馈：“我大概看了下，还算合预期，比之前做利苑那个好多了，至少看起来像合理的建筑”。这是初步人工视觉印象，不等同于分维度评分、独立几何验收或受控跨案例比较。原文保存于评价侧，不进入后续模型输入。

## 接线核验与边界

Paratera 鉴权目录返回准确 ID `Kimi-K3`、`Qwen3.8-Max`。其公共文档没有型号级 effort 映射或输出上限表；原厂次高档分别为 high、medium。用户明确选择按原厂档位补接线，不改成统一 high。

两型号分别完成两次微型图片—工具—回执—回答预检，共四次请求，全部 HTTP 200。没有读取正式案例或 BIM，不计正式样本。请求档位、原始响应、usage、参数和哈希在外部 audit 保存。服务接受参数不能证明内部档位实现或满窗口容量。

- Kimi：原厂 `max_completion_tokens` 最大 1,048,576、默认 131,072，输入与输出共享 1M 上下文；原厂固定 temperature 1.0。实际路由已接受 high 和原厂最大输出参数。完整顶层 `reasoning_content` 保留；返回包装只重复相同内容，无独有签名，不能无差别转发未知字段。
- Qwen：medium 是原厂独立次高档；high 是最高 xhigh 的别名。采用已验证的 `max_tokens=131072`、temperature 0.6；此字段约束最终回答，不能写成思考与回答合计上限。没有另加 thinking budget。
- 原冻结 Paratera formatter 错误地一律拒绝 medium，并将 Kimi 使用普通输出字段；隔离修复限制在已核实型号。
- Kimi 的配置上限等于整个共享窗口，不能把上限全部静态预留后再加输入而拒绝一切请求，也不能虚称本地只预留 128k 就覆盖线上 1M。每次按实际输入估算和共享窗口剩余空间形成实际输出参数，预算、请求字节、哈希与记录保持一致。131072 只用于上下文压缩规划，不作为固定生成上限；输入估算仍具有已记录的不确定性。

原厂来源：<https://platform.kimi.ai/docs/guide/kimi-k3-quickstart>、<https://platform.kimi.ai/docs/introduction>、<https://docs.qwencloud.com/api-reference/chat/openai-chat>。Paratera 原始页面与读取结果保存在 audit。

## 队列、资源与 TPS

剩余主队列顺序为 Kimi 第一次、Qwen 第一次、Kimi 第二次、Qwen 第二次；Astra 第二次明确暂不启动。Flash 第二轮单独运行，但占用同一个最多两案并行限制。队列只在空位与内存门槛满足后补发，所有单案独立 claim，失败保留、不自动冷启动替换；Popen 结果未知时停止后续准入并继续观察已知运行。按 PID 与创建时间追踪已观察的后代；未被观察即脱离的进程仍属监控边界。

用户追加授权今后自动释放本机闲置 BIM MCP 服务，并说明没有其他本机 Codex 会话。已核实并停止此次新出现的 8 个服务，前后证据保存；不因此终止被测模型进程。保持原 10/8 GiB 准入门槛，不修改系统代理或分页文件。

新增评价侧只读 TPS 提取器，覆盖主模型和 worker：每请求用实报 `completion_tokens` / `output_tokens` 除以原生 `adapter_send_seconds`，汇总采用总输出除以对应请求秒之和。失败、未知 usage、缺失时长单列。它包括服务器排队、prefill、推理、网络与传输，且 tokenizer 与思考计数口径可能不同；不是纯 decode TPS、整案吞吐或扣网后效率。当前没有首末 token 时间，TTFT 和 decode TPS 保持未知，HTTP headers 不能替代首 token。

首批回填端到端输出 TPS：Astra 29.79（26 请求、2 个任务）、Flash 239.36（33 请求、1 个任务）。原生交付与独立质量分别评价；两次重复及同机并行不足以给总体稳定性或独立模型速度排名。

## 证据与后续

主审计目录：`D:/Sim-BIM-Agent/cases/energy-plaza-mixed-20261021/audit/queued-evaluation-20261022`。其中 `phase-flash-r2` 已终止；主队列是否启动必须读取实际 status，不能以本文的排队计划推断。

包含配置、原始 scope、接线预检、模型目录、授权和人工反馈、清理记录、外部 controller、TPS 提取器与回填、进程树/资源采样、后续 seal 和原生结果索引。第一批记录仍保存在 `batch-01-astra-flash-20261021`，旧协议不覆盖。

用户明确本轮结束先只汇总交付和运行记录，暂不做质量评估，次日再讨论评测体系。质量细则后续确定后再统一评价保留样本；效率扣时只减有证据的网络阻塞区间并集，不扣并行生产性工作。用户取消高频聊天唤醒，保留单次收尾闹钟；后台 controller 负责原始采样与队列，不靠反复模型轮询推进。完成后给交付、耗时、请求工具、实报 token、TPS、失败重试及 BIM/日志链接的汇总并暂停闹钟，不归档聊天。
