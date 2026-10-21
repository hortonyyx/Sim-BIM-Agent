# 幸福中心第一批：Astra 与最新 DeepSeek Flash

用户已明确暂不做 Opus，先并行运行 Astra 和最新 DeepSeek Flash，各一次独立冷启动，详细保留评测材料。第二次重复、Kimi、Opus、失败重跑及线路回退均不由本批自动触发。正式进程与终态以外部批次 `status.json` 和各 run 的原生 `receipt.json` 为准；配置封存不等于已经发出模型请求。

## 统一运行条件

| 项目 | Astra r1 | DeepSeek Flash r1 |
|---|---|---|
| 路由 / 调用 ID | chatgpt-subscription / gpt-6-astra | deepseek / deepseek-flash |
| 次高推理档 | xhigh | high |
| 最新型号证据 | 当前账户目录 GPT-6-Astra | 官方 2026-10-17 更新为 DeepSeek-V4.1-Flash-1017；实时目录显示 DeepSeek-V4.1-Flash |
| 输出配置 | 128000，仅本地预留，订阅线路不发原生输出 cap | 393216，服务原生最大 max_tokens，包含思考和可见输出 |
| 独立输出目录 | runs/energy-plaza-astra-r1-20261021-001 | runs/energy-plaza-deepseek-flash-r1-20261021-001 |

共同采用 `single_model`，同一中文 scope、原始 mesh / 5–12F 辅助图 / 来源和用户观察 JSON，三个输入哈希保持。BIM 为主要交付，平面与回叠可作过程证据；不要求固定操作路径或空间数量。参考 candidate_06 与其他模型产物不进入工作模型输入。

每案共享 80 次模型请求 / 400 次工具，`tokens=null`，不单独限制思考额度。5400 秒按总经过时间保护，包括网络故障和重连；既有一次普通重试、2 秒 backoff、累计 180 秒重连沿用。可选同模型只读 worker 上限 8、候选上限 64、图片历史 3 批、推理历史 all；上下文容量自动。没有人为统一 32k 等输出上限，各服务原生限制仍存在，同名 effort 不等于等量计算。

## 启动前修正与检查

只读审核发现旧 `seconds=5400` 同时设置墙钟截止和累计请求秒上限，并将 worker 单次完整请求自动切成 `5400/(8+1)=600` 秒。直接运行会偏离已确认的墙钟口径。因此启动前新增显式 `limits.time_budget_mode="wall"` / `--time-budget-mode wall`；旧默认 `wall_and_requests` 保留。

wall 模式继续用 5400 秒限制准备、请求、工具、worker、重连和恢复的总经过时间，但不因并发请求秒相加提前耗尽预算，也不自动给 worker 切 600 秒。模型自己明确设置的子任务时间仍有效，且受父任务剩余时间约束。每个真实请求的原始耗时和累计结算记录保持；旧缺字段配置的序列化与恢复兼容。

最终登记 **runtime39/domain136**（后者包含入口与 worker 接线），register / verify 通过；两案共享同一提交，最终 commit、完整配置、输入与 controller 哈希由批次 `seal.json` 固定。运行中不改产品。

离线检查及独立审核记录保存在批次 audit；新墙钟场景覆盖并发累计 5600 秒而墙钟 700 秒、真正墙钟截止、显式 worker 时间、配置 CLI 传播及恢复。独立审阅未发现实质缺陷或启动阻断项。

| 检查范围 | 结果 | 边界 |
|---|---|---|
| 新墙钟定向场景 | 12 通过，5.94 秒 | 纯离线，与下面回归有重叠，不累加 |
| 初轮六模块 | 144 通过 / 7 失败，33.81 秒 | 4 项基线断言；1 项本机读取编码；2 项尚未登记版本 |
| UTF-8 回归 | 134 通过 / 4 项已知断言排除，30.40 秒 | 编码项复验通过；与初轮重叠 |
| 登记后完整入口、恢复与拒绝改变计时模式 | 2 通过，75.43 秒 | default / wall 各一，真实本地 MCP、scripted 模型，0 远程模型调用 |
| 独立审阅、版本与配置回读 | 通过 | 无实质缺陷；新版本注册保留所有旧记录 |

4 项既有断言涉及 StopIteration 错误包装、完成记录缺失时的恢复状态、两项上下文耗尽状态名；在独立进程加载 HEAD 旧 loop 后相同失败，未在主树覆盖旧代码。本批不以修改断言掩盖失败。原始初轮工具输出未完整重定向，长错误输出曾被截断；`wall-time-validation/validation-summary.json` 明确这一证据边界，登记后入口的完整输出另存 stdout.log。

本轮目录核验只读，未用额外模型推理小测替代正式样本。DeepSeek 最新别名是服务端可更新别名，不冒称锁定了不可变权重；保留当日目录及实际响应 model 字段。

## 资源与可追溯记录

用户单独授权结束已查明的 12 个 Codex 持有的 BIM MCP 服务；逐一校验 PID、父进程、命令与创建时间后处理，事前和结果清单保留。未改系统代理、TUN 或分页文件。Windows 自动分页文件同期自行缩小，内存余量使用实际启动采样，不根据释放量推算。

批次准入要求可用提交空间至少 10 GiB，每次 spawn 前至少 8 GiB。10 GiB 是参考此前两个同类无 worker 运行的采样私有内存峰值之和约 7.51 GiB，加启动余量的实验选择，不是产品限制或最坏情况保证。两案共享本机，资源争用是效率解释的混杂因素；3 秒记录系统提交内存/物理余量及两棵进程树的私有内存/工作集，不声称覆盖 CPU/GPU 性能。

批次目录：`D:/Sim-BIM-Agent/cases/energy-plaza-mixed-20261021/audit/batch-01-astra-flash-20261021`。

- `config.*.r1.json`、`shared-task-scope.txt`、`model-catalog-and-effort-evidence.json`、`seal.json`：模型、条件、版本和输入封存。
- `controller.py`、`launch*.claim`、`status.json`、`state.*.json`、`stdout.*.log`、`stderr.*.log`：每案独立启动、时间、状态和退出码。只启动各一次；一个启动失败仍监控已启动兄弟案。
- `resource-samples.jsonl`：运行期间 3 秒采样。控制器不因临时内存下降杀模型或改变 worker 权限。
- `result.*.json`、`files.*.json`：原生 receipt 摘要、终态文件哈希索引、输入后验。控制器退出码不等于模型正式交付，也不等于质量通过。
- 各 run 的 `events.jsonl`、blobs、checkpoint、journal、versions、frozen、全部候选 BIM/viewer 和 worker 记录：可观测请求、响应、工具调用/结果、图片、失败与恢复过程原件。凭据只通过既有受保护来源读取，audit 不保存 key/token。

协议另存 `audit/comparison-protocol.v0.3.json`，v0.1/v0.2 不覆盖。质量/效率/稳定性的细则可继续讨论；分清预先声明与事后探索，统一重评全部保留样本。效率保留原始墙钟，之后仅扣有证据的网络阻塞区间并集，不能直接用 recovery_seconds 或全部失败请求时间作扣除，也不能扣掉并行生产性工作。当前不预判质量，不制定结果导向的阈值。

下一步核对本批原生交付与独立 BIM 质量，再与用户定下一批；不自动扩跑。桌面产品未更新。
