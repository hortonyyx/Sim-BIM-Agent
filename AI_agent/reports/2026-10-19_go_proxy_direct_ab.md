# Go 官方接法核对与相同失败请求的代理/直连对照

2026-10-19，用户要求先查官方文档和我们的连接方式，再用同一请求对比代理与直连，mesh 保持不动。**原 GLM 失败请求经代理在 60.242 秒断开，经物理网卡直连在 20.577 秒完整返回 HTTP 200。代理路径的嫌疑显著增强，但一组串行对照仍不能锁定本地代理进程或证明长期稳定。** 本轮止于这两次模型请求，无新增产品代码或网络配置修改。

## 官方要求与当前实现

核对日期为 2026-10-19。依据：[Go 模型端点与客户端要求](https://opencode.ai/v2/docs/console/go/)、[Inference 认证和示例](https://opencode.ai/v2/docs/console/api/inference/)。Go 专页的订阅路径优先于通用 Inference 示例；没有改用按量服务。

| 项目 | 官方说明与当前代码 | 判断 |
|---|---|---|
| GLM/MiMo 端点 | `/inference/go/openai/v1/chat/completions`；`opencode_go.inference_base_url` 与实际 trace 一致 | 已对应 |
| 认证与 JSON | `Authorization: Bearer`、`Content-Type: application/json`；显式仓库外 Go 凭据 | 已对应，无凭据环境回退 |
| 客户端标识 | 自有 `User-Agent`；实际为 `Sim-BIM-Agent/work-model` | 已对应 |
| 会话 | 每段会话稳定 `x-opencode-session`；从运行根目录和 task ID 派生 UUID，角色入口与单模型入口均创建 Go adapter | 已对应，未泄露本机路径 |
| 流式 | Go 页面未规定必须流式；通用 Chat Inference 示例未启用 stream | 不能把当前 `stream=false` 定为违规或已确认根因 |
| 超时、重试 | 未查到 Go 文档规定的客户端固定超时；本案真实发送是 450 秒，0 自动重试/重定向 | 60 秒 EOF 不是本地 450 秒超时触发 |

相关实现为 `src/agent_runtime/opencode_go.py`、`connections.py`、`adapter.py` 和 `src/agent/runtime_roles/entry.py`。Go 文档面向 coding-agent 类型流量；Sim-BIM 的图纸读图不是官方列出的已验证客户端/工作负载，接法匹配不等于官方承诺此工作负载稳定。Go 页面也未提供本项目全部模型参数的逐字段规范；本次完整 200 只证明这一份正文当时可以被处理。

**发现的实际连接行为：** `HttpChatAdapter` 构造 `httpx.AsyncClient` 时使用默认 `trust_env=True`，因此会继承环境/系统代理。当前进程 `HTTP_PROXY`、`HTTPS_PROXY` 均指向 `http://127.0.0.1:10809`；这不是显式直连。行为符合 [HTTPX 官方环境变量说明](https://www.python-httpx.org/environment_variables/)，不是 Go 协议错误；连接描述符目前也不显式记录这一网络路径。

## 为什么不能仅设置 trust_env=False

现场 Meta TUN 已开启（`auto-route`、`strict-route` 均开启），系统 DNS 为 `opencode.ai` 返回 `198.18.0.68` / `2001:2::50`，属于本机代理使用的虚拟地址。因此仅关闭 HTTP 代理继承仍可能走 TUN，不能据此称为真实直连。

本次诊断独立绑定当前物理以太网 IF9，在 TCP connect **之前**设置 Windows `IP_UNICAST_IF`，绑定物理源地址，并连接通过 DoH 查到的真实 A 地址；HTTPX 的原域名、Host、TLS SNI 与证书验证保留。设置及字节序依据 [Microsoft IPPROTO_IP socket options](https://learn.microsoft.com/en-us/windows/win32/winsock/ipproto-ip-socket-options)。没有更改系统路由、hosts、代理、TUN 或产品默认连接。

预检查两条路线的公开 models GET 均返回 200/37 型号，0 推理调用。直连 trace 验证 `getsockopt(IP_UNICAST_IF)=9`、真实 peer 和 `opencode.ai` SNI；只用于本机此次排查，不将固定网卡编号或 IP 写入产品。

## 固定请求与结果

- 产品代码冻结 `fbf33f845077fa5c2412f98c2313b164ffc44a60`，runtime30/domain113；531 个程序/脚本/产品文件与检查前一致，也与前次读图运行的程序清单一致。
- 来源：`D:/Sim-BIM-Agent/runs/reader-connection-retest-20261019/glm-go/f1-03`，`event-000062`、`f1-03-unit` 的第 5 请求，原来等待 45.481 秒后 EOF。
- 精确重建 1,755,143 字节正文，6 张图片、17 条消息、13 个工具定义；SHA-256 为 `6df2175d9787ee94451e928b26e4b03691b43661e3676bbd43d717e88459430e`。两次发送均逐字节断言相等。
- `glm-5.3-flash/high`、`max_tokens=32000`、`stream=false`，原 session。相同 URL、认证来源及全部 headers（私有值仅在内存比对，日志脱敏）、HTTP/1.1、450 秒请求/总体上限，每臂新 client。代理先、直连后串行；0 重试、0 provider 回退、0 返回工具执行。沿用原正文，无额外总 token/金额上限；完整费用未知。

下表耗时从 HTTP request_start 到响应完整读取/异常，不含新建 adapter/client。原始 result 的臂总耗时另为 62.277/23.761 秒，不能混用。

| 路线 | 请求耗时 | 结果 | 已知 tokens |
|---|---:|---|---:|
| 当前 HTTP 代理 `127.0.0.1:10809` | 60.242 s | TCP、CONNECT、TLS、请求体发送均完成；等待响应头时 `RemoteProtocolError: Server disconnected without sending a response.`，无 HTTP 状态 | 未知 1 请求 |
| 绕过 HTTP 代理及 Meta TUN 的物理网卡直连 | 20.577 s | 完整 HTTP 200，返回 `glm-5.3-flash`；JSON/两个工具参数可解析，finish_reason=tool_calls | 37,030 |

总计 **2 模型请求、1 完整响应、37,030 已知 tokens，另 1 请求 usage 未知**。直连 input 34,194、output 2,836，其中 cached input 34,176、reasoning 2,734。返回 2 个工具调用但执行 0 个；未启动读图循环、没有正式提交、BIM/mesh 生成或质量评价。目录/DoH 查询不计模型调用。

## 结论与边界

1. 当前检查到的 Go 端点、鉴权与客户端/会话标识没有发现不符合文档之处；原失败正文在真实直连完整返回，不能把它归为必然无效请求。
2. 代理路径再次复现提前 EOF，直连成功，后续应优先排查代理出口/路由。尚不能区分本地 mihomo、代理远端出口、Go 边缘或上游，也没有证据证明固定统一 60 秒截止。
3. 只有一组代理先/直连后的实验，直连又命中大量缓存；先后顺序、缓存、服务波动和可能的不同上游路由都是限制。预检 catalog 的 cf-ray 分别为 PDX/EWR，实际成功 inference 为 PDX，失败 inference 无响应头；**不能写成两次推理已证实落到不同边缘**。
4. 直连响应仅 20.577 秒，没有覆盖持续数分钟无响应的请求，更没有复跑完整 reader；不宣称长任务已修好。非流式长时间无数据与链路空闲截止仅是待证假设，未在本轮更改 stream 来混淆变量。
5. 若后续改变 Go 默认路线，应同时考虑 TUN；单改 `trust_env=False` 或 `NO_PROXY` 不能在当前机器保证直连。本轮保留默认设置，下一步网络策略或流式对照另按用户方向推进。

## 审核、证据与交接

- 缓存/复放脚本：`D:/Sim-BIM-Agent/cache/go-proxy-ab-20261019`，包含 exact request、preflight、route transport、replay、network context 和程序哈希清单。
- 正式证据：`D:/Sim-BIM-Agent/runs/go-proxy-ab-20261019`，包含 plan、每臂 transport/result、完整成功响应、summary、audit 和 `evidence-sha256.json`。
- 离线审计按实际 `request_start`/终态重新计数，核验源正文、session、headers、450 秒超时、实际线路、response SHA、工具 JSON 和源码保持，结果通过。没有运行无关产品测试。
- 原 provider 调研子代理独立核对官方资料、诊断脚本和终态证据，无阻断发现；该代理 0 模型调用、0 文件修改。失败路径实际边缘未知、缓存/顺序及耗时口径均已按复核意见明确。
- 产品仍 runtime30/domain113，固定桌面仍 runtime24/domain110；无桌面发布。mesh 两个候选保持原状，继续等用户检视/讨论，不追加生成或测试。

离线审计文件 SHA-256：`f371b17706c68af94cd0477d74a7441acf8b7c0ec2db1f3f08c23dd64adcfc09`。收尾磁盘可用空间 C 45.83 GiB、D 121.72 GiB；未新建工作树，保留既有含独立工作的 Go 工作树。
