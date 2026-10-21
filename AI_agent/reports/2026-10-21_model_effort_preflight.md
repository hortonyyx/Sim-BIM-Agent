# 幸福中心测试前：Opus 与 Kimi 次高档接入核验

用户本轮只授权接通 Opus、确认其与 Kimi 的次高档；整案仍不启动，第一批顺序之后再定。正式意向仍为 Astra、Opus 5.5、DeepSeek Flash、Kimi K3，各两次独立冷启动。

## Opus：次高档确认，服务调用仍被拒绝

- 精确 ID 为 `anthropic/claude-opus-5.5`。Anthropic 官方文档及 OpenRouter 当日 `/api/v1/models` 均支持 `max / xhigh / high / medium / low`，因此目标为 **xhigh**。原生参数是 `output_config.effort=xhigh`，OpenRouter Chat 参数是 `reasoning.effort=xhigh`，不另设 reasoning token 预算。目录披露 context 1,000,000 / output 128,000；未做容量实测。
- 当前 key 的只读鉴权查询 HTTP 200。新增三次无建筑输入小请求，均明确发送 xhigh：限定 Anthropic 的 SG 出口 1.234 秒 / US 出口 3.219 秒；另一次不指定 `provider.only` 的 SG 默认路由对照 1.859 秒。三次均 HTTP 403，错误为 `The request is prohibited due to a violation of provider Terms Of Service.`；路由元数据均 `attempt=0`、上游未选中、没有实际模型响应或 usage。此前的一次 403 保留在旧证据，不计入本轮三次。
- 原 `10819` 监听的 `Claude-US-Only` 组只有 REJECT，未修改。临时建立独立 loopback `10829`，初选 US 01 的公开网络请求超时，未向它发模型请求；改用现有 US 02 TCP 节点，经原 Clash 连接，Cloudflare trace 确认为 US、OpenRouter 目录 HTTP 200、TLS 校验开启。Opus 在该线路上仍被拒绝，不能把问题归为简单的出口国家错误，也不能确定具体账户/政策原因。
- 临时代理进程已结束；主 Clash、系统代理、TUN、原专用监听未改，主配置 SHA 核对不变。私有代理配置留在仓库外受 ACL 保护的 `D:/Sim-BIM-Agent/local-connections/openrouter/proxy`；API key 未写入证据或仓库。小测 4096 输出上限只是连接检查保护，不是后续测评预算。
- **尚未接通，不准入正式模型配置。** OpenRouter runtime adapter 尚未实施。下一步需要在 OpenRouter 账户/支持侧查明并解决该 403，随后再验证小型图片/工具续接；不要通过目录可见或只读鉴权成功推断可运行。

脱敏证据：`D:/Sim-BIM-Agent/cases/energy-plaza-mixed-20261021/audit/openrouter-opus55-effort-preflight-20261021/receipt.json`。可向服务支持提供该错误、时间、模型、SIN/LAX 路由地区和 attempt=0；无需发送 API key 或案例资料。本轮没有代发支持消息。

依据：[Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)、[OpenRouter 参数映射与目录说明](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)、[OpenRouter 错误与路由诊断](https://openrouter.ai/docs/api_reference/errors-and-debugging)。OpenRouter 当日默认档显示 high，与 Anthropic 文档的原生默认 medium 不同；本案明确传 xhigh，不依赖默认值。

## Kimi：high 请求接受，补齐真实历史字段

- [Kimi 官方文档](https://platform.kimi.ai/docs/guide/use-reasoning-effort)列出 `low / high / max`，默认 max，次高档为 **high**。[Go 官方路由](https://opencode.ai/v2/docs/console/go/)使用当前 `/inference/go/openai/v1/chat/completions`。
- 两次极简请求均通过既有 Go 物理 IPv4 传输，明确发送 high，HTTP 200 / stop / OK。合计 11.875 秒，实报 180 输入 + 59 输出 = **239 tokens**，其中 34 reasoning 已含于输出；0 重试、工具或建筑输入。第二次经过正式 `provider_parameters` 入口。
- 本地 Kimi effort 允许列表由仅 max 扩为 high/max，服务默认行为保持。尚未核验的 low 不随本轮一起开放。服务没有回显实际生效 effort，因此不能把 HTTP 接受说成对内部计算档位的独立证明。
- 第一批 34 项离线契约检查通过后，独审发现真实 Go 响应使用 `reasoning` + `reasoning_details`，原 runtime 只回传 `reasoning_content`；假响应测试未覆盖这一差异。已仅对 Go/Kimi 补齐原样历史保留、新运行/恢复一致配置，以及 current_tool_chain 的旧轮字段清理。真实回包结构的离线工具/图片续接、响应后中断恢复、all/current_tool_chain 历史与其他路由边界均已验证。
- 修后 38 项 Go 检查（4.89 秒）与 17 项相关既有回归（3.78 秒，65 项未选）通过，不与修前 34 项重复相加。修复过程 0 新增真实调用。两份派生 receipt 的 reasoning 字符误计由 0 更正为 54/86，原请求/回包和 usage 未变，详见 `native-history-correction.json`。
- 修后独立只读复核确认原推理历史 P2 与计数错误均闭合，无新增实质缺陷；未重复模型调用或已通过的相同检查。

证据：`D:/Sim-BIM-Agent/cases/energy-plaza-mixed-20261021/audit/kimi-k3-effort-preflight-20261021/receipt.json`。原请求和回包保留，字段计数提取器的更正单独记录。实时小测不包含图片工具循环、长上下文或 BIM；离线覆盖与实时覆盖须分别报告。

## 配置与后续边界

沿用已定的 BIM 主要交付、平面可选、80 模型请求 / 400 工具 / 5400 秒总经过时间保护、全案 tokens=null、不额外限制思考、效率另扣有证据网络故障时间。质量细则可以边测讨论，统一回评全部保留结果。协议更新另存版本，不覆盖原 v0.1 约定。

Kimi 初次允许列表改动登记 runtime37，历史字段修正后最终为 **runtime38/domain135**，注册与 verify 已通过。最新协议为 `D:/Sim-BIM-Agent/cases/energy-plaza-mixed-20261021/audit/comparison-protocol.v0.2.json`，v0.1 原件保留；Opus 明确标为未接通，不能将本轮配置当作四模型全部就绪。

当前 0 幸福中心正式测试，未更新桌面产品，尚未确定首批顺序。收尾磁盘剩余 C盘24.38GB / D盘111.26GB。
