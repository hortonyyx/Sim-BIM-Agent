# OpenCode Go 订阅接入

2026-10-15：用户指定 OpenCode Go 与 Paratera 承担后续 work model 消耗，并授权环境准备、接口测试与接线。Go 是独立的 `opencode-go` provider，不借用 `paratera` 名称或人民币价格表；客户端不切到其他服务或按量端点。

## 凭据与配置

本机私有文件为 `D:/Sim-BIM-Agent/local-connections/opencode-go/credentials.env`，凭据变量为`OPENCODE_GO_BASE_URL`（固定为`https://opencode.ai/inference/go/v1`）和`OPENCODE_GO_API_KEY`；另允许公开网络参数`OPENCODE_GO_DNS_BOOTSTRAP_IPV4`。该base是凭据的订阅范围与模型目录入口，不直接拼接推理路径；实际推理base按下表协议选择。不读取环境变量作凭据或网络参数回退，不展开env插值，不把key写到配置、journal或发布包。

2026-10-19起，Go独立采用Windows物理IPv4直连：动态发现已连接物理网卡和默认路由，按路由/接口metric之和选择，最低metric并列或无可用路由则报错。连接前按socket选接口和绑定源IP，DNS与Go推理都避开HTTP代理和TUN，保留原域名、Host及TLS证书验证；不改系统路由/代理、不影响Paratera、ChatGPT等其他provider。暂只支持Windows物理IPv4，失败不会退回代理或系统fake-IP DNS；不固定网卡编号、源IP或Go服务器IP。

DNS使用`https://cloudflare-dns.com/dns-query`，仅查询`opencode.ai`的A记录，不携带Go鉴权/session；解析和连接计入既有超时，结果按TTL最多缓存300秒。bootstrap默认`1.1.1.1`；本机该地址直连超时，因此在同一显式仓库外文件配置当前核验可达的Cloudflare DNS地址`104.16.248.249`。这是DNS服务的引导地址，非Go服务器静态地址；变更后仍必须通过Cloudflare域名证书校验。没有按失败自动切换DNS、模型服务或网络路径。机器网络改变后，新建Go运行/适配器时重新发现物理接口；现有适配器保留已选接口，旧运行不原地更换封印版本。

可复制 [Go配置示例](../../product/config.opencode-go.example.json)，修改实际输入和输出目录，再执行产品`check/run`。原 [默认配置](../../product/config.example.json)仍是6 Sol，两个示例不互相改写历史结果。Go的Kimi K3默认省略思考档和采样参数，按服务默认运行；2026-10-21核对官方low/high/max及两次Go实测后，明确接纳`high`与`max`，次高档为high，未顺带开放未经该路由核验的low。真实Go回包的`reasoning`和`reasoning_details`按原结构保留于后续工具轮，历史策略只作用于发送投影。实测/离线边界与Opus接入状态见[次高档核验](../reports/2026-10-21_model_effort_preflight.md)；HTTP接受不等于对服务内部计算档位的独立证明。

Haiku整案采用独立的 [流程贯通配置](../../product/config.opencode-go-haiku.example.json)：`claude-haiku-5-5/medium`、64,000输出tokens、48请求/240工具、无整案时间硬截止。输入和楼层范围应按实际案例填写。runtime v19/domain v80已修复签名历史前缀、状态快照移动和无正式交付却报完成的问题；sm25原六图单次冷启动在8分39秒自主完成两层BIM及查看器。相同代码的`high/32,000`仍连续截断，不能沿用旧参数宣称已经跑通；质量和跨次稳定性未评，详见 [Haiku整案修复记录](../reports/2026-10-15_haiku_whole_case_repair.md)。

## 已接入型号与协议

| 精确model ID | 推理base | API |
|---|---|---|
| `glm-5.3-flash`、`kimi-k3`、`mimo-v2.6-flash` | `https://opencode.ai/inference/go/openai/v1` | `/chat/completions` |
| `minimax-m3`、`qwen3.8-max`、`claude-haiku-5-5` | `https://opencode.ai/inference/go/anthropic/v1` | `/messages` |
| `gpt-6-luna` | `https://opencode.ai/inference/go/openai/v1` | `/responses` |

2026-10-19按官方协议路径修正，`ConnectionDescriptor.base_url`记录实际推理base，传输器拒绝目录base及不匹配的协议base。旧运行绑定旧路由/版本，不能原地换线路恢复；新验证使用新运行目录。目录查询仍是`https://opencode.ai/inference/go/v1/models`。

这七个ID有明确协议、参数和预算profile；2026-10-19实时Go目录返回37个型号，不代表全部已接线。其他ID在模型请求前拒绝，不猜协议或接受未知实验型号。GPT-6 Sol不在已配置Go目录中。

MiMo于2026-10-19补入，使用服务默认思考，不传温度或未登记effort。按[官方图片请求格式](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/multimodal-understanding/image-understanding)发送`max_completion_tokens`；图片保留原字节，发送前去掉本地`detail`显示提示并登记来源投影。runtime28在保存工具呈现前准备模型可接受的图片格式，保留原MCP、图片hash及省略hint，修复响应到达后把hint变化误判为丢图的审计故障。1,048,576上下文来自当日模型元数据；Go图片预算仍为未独立校准的proxy。旧代理路径下串行复测仍有传输失败，当轮真实读图质量未评，见[复测报告](../reports/2026-10-19_reader_transport_retry.md)。

运行请求携带自身`User-Agent: Sim-BIM-Agent/work-model`及`x-opencode-session`。会话ID由运行目录与任务ID派生为UUID，同一任务恢复时稳定，不同并发reader各有自己的ID；不发送本机原始路径。没有伪装成OpenCode或Claude客户端。

Go Responses采用标准非流式请求，实际传送`max_output_tokens`，保留无状态的原生输出/加密推理历史；不沿用ChatGPT订阅预览“只本地预留、不能传输出cap”的规则。Messages沿用现有逻辑消息/图片转换及原生工具回执，使用标准版本头，不带GLM专用beta。Chat路由不发送Paratera的`enable_thinking`扩展或多采样`n`。

## 并发、预算与用量

单模型和图纸`role_division`都可选Go。角色分别解析其`provider+model`连接，允许同一Go订阅的不同角色使用不同协议；不会因同一provider名称把全部角色送到一个协议。并发上限、根预算、任务恢复和交付逻辑沿用现有实现。

分工入口仍需有限的`limits.seconds`，单模型支持null。若Go与Paratera混合，现有`credentials_file`可包含两组变量，分别只读取自己的变量；不得在role配置内写key。顶层路由须与coordinator一致。

Go按订阅记账，保留供应商实报tokens，不套Paratera单价、不生成伪人民币费用或零费用结论。供应商input/prompt tokens已含图像，不额外叠加本地估算图像tokens。预算与图片预留仍为本地估算；新模型图片公式是明确标注的proxy，Go部署cap也未做满容量测试，不能称供应商计费公式已校准。

## 验证范围

第一轮环境检查：40模型目录HTTP200，GLM工具调用和续接、Kimi合成小图通过；3模型请求，751实报tokens，0整案。该记录位于`D:/Sim-BIM-Agent/local-connections/opencode-go/verification.json`。

本轮接线验证记录见 [接线报告](../reports/2026-10-15_opencode_go/README.md)。协议闭环、CLI配置、订阅记账和并发配置分别验证；整楼图纸保真、模型比较和其他目录型号的可用性不由接口检查替代。

2026-10-19连接修复与小图片/工具续接验收见[连接修复报告](../reports/2026-10-19_provider_connection_repair.md)。小请求成功不能证明此前长请求的EOF已彻底消失；本轮没有增加重试、超时或切换默认代理。

随后按用户授权完成[原读图任务长请求复测](../reports/2026-10-19_reader_connection_retest.md)：新官方路径下，GLM平面及MiMo平面/北立面仍复现等待响应头时EOF，均早于450秒上限。当轮旧代理路径下Go长请求问题仍未解决；当时证据不能区分本机代理路径、网关或上游责任。

2026-10-19追加[相同失败请求代理/直连对照](../reports/2026-10-19_go_proxy_direct_ab.md)：已核对端点、鉴权、自有UA、稳定session与当前官方文档；没有查到强制流式要求。当前HttpChatAdapter默认继承环境/系统代理；本机又启用Meta TUN/fake-IP，不能将trust_env=False等同真实直连。诊断在不改系统设置的前提下，单socket绑定物理网卡并保留TLS域名验证。原GLM第5请求逐字节重放，代理60.242秒EOF、物理直连20.577秒200；2请求、37,030已知tokens另1未知，0工具执行。优先怀疑代理路径，但单组先后/高缓存/服务波动不足以定位本地代理或证明多分钟/整任务稳定；当时产品默认网络设置尚未更改。

2026-10-19随后按授权实施[Go独立物理直连及完整读图复测](../reports/2026-10-19_go_direct_reader_retest.md)，开发runtime31/domain113。104项定向通过，独立代码审阅无P1/P2。三个Go节点31请求/29完整200，另2请求因原900秒节点预算取消，0提前EOF；15个完整响应超过60秒、最长281.717秒。Go及其DNS逐请求有物理出口证据；Paratera测试前后实际仍走10809代理。GLM F1无有效稿，MiMo F1未提交草稿质量失败，MiMo North638.825秒正式提交且本张数值/实际图独评通过。单批证据不等于长期稳定或整楼能力；没有自动扩测、改reader或发布桌面，固定桌面仍runtime24/domain110。

官方依据：[Go端点、会话和模型说明](https://opencode.ai/v2/docs/console/go/)、[OpenCode模型元数据](https://models.dev/api.json)、[标准Responses工具循环](https://developers.openai.com/api/docs/guides/function-calling)、[无状态推理历史](https://developers.openai.com/api/docs/guides/reasoning)。
