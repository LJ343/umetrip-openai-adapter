# 航旅 OpenAI 兼容中转

本地项目：`/home/liujie/code/openai-proxy`。Ubuntu 现有部署：`/opt/openai-proxy`，systemd 服务名 `openai-proxy`。

2026-10-21 17:23（北京时间）服务器已切换到生产上游 `https://assistant.umetrip.com/gateway/api/web/sse/v1/chat/completions`。平台继续使用 `http://162.14.82.104:8000/v1/keepalive/chat/completions`、模型 `qianrang` 和原中转密钥。此次切换仅修改服务器 `.env` 的 `UPSTREAM_URL`；卡片、正文、会话、超时和日志配置不变。新部署模板使用生产地址；代码未配置 URL 时的历史兜底仍是灰度，部署时必须显式配置。

本版按“保留上游输出，移除卡片”的要求调整，只负责协议包装，不调用其他模型改写答案，也不代替上游执行办理业务。

## 输出规则

- **正文完整保留**：过程文字、多个段落、表格、链接、标点和首尾空白全部保留。不取最后一段，不摘要、不去重、不纠正前后矛盾；默认不添加段间空行。
- **移除卡片**：删除 `<jump_card>…</jump_card>`、`<flightstatuslist_card>…</flightstatuslist_card>` 等 `*_card` 标签及其内容；标签跨多个 chunk 或 `text_end` 时也能处理。以 `*_card` 命名的独立卡片事件也移除。其余文字和普通 `<` 保留。
- **保留工具业务结果**：`tool_result_end` 的结果正文、结果数据、业务状态等进入 `content`，结构化结果以 JSON 文本表示，前后空行仅用于分隔独立结果。工具调用的 name、id、call_id、tool_key、parameters 属于调用元数据，不当作业务结果展示。只有占位文字“查询结果：是否成功”且状态为成功/空时不显示。
- **隐藏内部通知**：`prepare_model_input`、`ttft`、`tool_call_start` 不进入回答；`text_end` 和 `thinking_end` 作为分段信号处理。工具的实际执行仍由上游完成。
- **思考独立保留**：`thinking_delta` 和 `delta.reasoning_content` 进入 `reasoning_content`，不混入答案，不额外插入思考段落分隔符。该字段是常见兼容扩展，后台是否显示它取决于客户端。
- **其余业务数据保留**：未知的业务事件、chunk 扩展字段转换成标准 `content` 中的 JSON 文本，不会因平台不认识字段而静默丢弃。新增事件的展示可能比较原始，之后可按真实协议优化。
- **标准字段保留**：支持 refusal、annotations、logprobs、tool_calls、function_call、system_fingerprint、service_tier，以及真实的 usage。原生函数调用参数不改写，也不自动执行。

同一份上游响应，无论客户端选择流式还是非流式，上游正文的内容和顺序一致。流式长时间没有正文时可额外添加一次等待提示，规则见下文；关闭该提示后两种模式的正文一致。两次独立请求的上游答案可能不同，不保证跨请求文字相同。

如果某项信息只存在于卡片中，移除卡片后它不会自动变成文字。卡片附近的“点击查看”话术仍保留原文。中转不会补造入口或业务数据。

## OpenAI 包装

- `POST /v1/chat/completions`，`stream:false` 或省略：聚合为 `chat.completion`，正文位于 `choices[0].message.content`。
- `stream:true`：输出 `chat.completion.chunk`，正文位于 `choices[0].delta.content`。统一 id、created、model，保留实际结束原因。
- `GET /v1/models`：返回模型列表；`GET /healthz`：检查进程状态。
- 上游始终按 SSE 调用。除覆盖 `stream:true` 外，请求参数、消息和历史原样转发，`stream_options` 也会转发。
- 只支持单个候选回答 `n=1`。返回结构异常时明确报错，不默默合并多个候选。
- 上游没有 token 计数时返回 `usage:null`，不编造零用量；请求流式 usage 时，最后一个 usage chunk 的 choices 为空，usage 可以是 null。
- 收到上游 `[DONE]` 后立即完成，不等待服务器关闭连接。默认严格入口遇到上游断流、错误或缺少结束信息时返回 JSON/SSE error；语音失败兼容模式见下文，它只完成明确的本地失败说明。

## 10 秒超时与流式等待提示

2026-10-21 日志显示后台的兼容性检测发送 `stream:false`。固定 10 秒的检测仍可能超时；不能将它强制改为 SSE，也不能先返回一份完整的“请稍等”JSON 后再追加第二份回答。中转没有平台的消息推送或回调入口，不能靠内部队列在已结束的请求上再次回复。

流式请求 `stream:true` 的处理：

1. 本地鉴权和参数校验通过后立即返回 SSE 响应头和空的 assistant 角色块，不再等上游先返回 HTTP 响应头。
2. 每次输出后静默 3 秒，发标准的空 `delta.content` chunk 保活；不污染回答正文。
3. 从请求开始 6 秒仍无正文时，额外添加一次 `正在处理，请稍等。\n\n`，没有结束标记。已有正文、拒绝信息或原生工具调用时取消提示；快速回答不添加。
4. 上游结果依序进入同一个队列和同一条 SSE 流，不被提示替换或挤掉。成功回答只在收到上游实际完成信号后发送终止块和 `[DONE]`。可选语音失败收尾有独立记录。
5. 默认严格入口在等待期间上游报错时输出标准 SSE error；语音兼容入口可以播报明确失败说明。断开客户端时取消上游连接和重连等待。

等待提示属于中转额外的可见文字，不会参与上游推理，也不会写入本地对话历史。调用平台若把显示文本作为历史再发来，仍会照常转发该历史。结构化 `response_format`（例如 `json_object`/`json_schema`）或仅音频的请求禁用可见提示，保留空心跳，避免破坏输出格式。

默认配置：

```ini
HEARTBEAT_INTERVAL=3
STREAM_WAIT_AFTER=6
STREAM_WAIT_MESSAGE='正在处理，请稍等。\n\n'
```

`STREAM_WAIT_AFTER=0` 或空的 `STREAM_WAIT_MESSAGE` 关闭可见提示，`HEARTBEAT_INTERVAL=0` 关闭空心跳。

这些措施适用于按“连续无数据时长”计算的读取超时。若平台强制“整次请求最多 10 秒”，或只接受非流式完整 JSON，心跳不能延长它的截止时间。平台还可能忽略空 chunk；正式聊天需要实测能否续流。这里不伪造快速成功结果，也不提前终止流来让检测通过。

因为流式现在先发响应头，上游随后返回的 HTTP 401/429/504 等在流内输出为 OpenAI error 对象；不能再改变已经发出的下游 HTTP 200。非流式仍返回对应的 HTTP 错误状态。日志新增 `upstream_headers_ms`、`first_content_ms`、`wait_notice` 和客户端断开状态，便于判断慢在何处。

## 可选：非流式 JSON 空白保活

如果平台仍卡在非流式兼容性检测，可将模型 URL 改为：

```text
http://162.14.82.104:8000/v1/keepalive/chat/completions
```

仍使用 `qianrang` 和现有中转密钥。此入口运行在同一个服务/端口，原 `/v1/chat/completions` 保持原有行为；SDK 使用新入口时 base_url 为 `http://162.14.82.104:8000/v1/keepalive`，模型列表也提供在该路径下。

对 `stream:false`，完整结果 3 秒内返回就原样响应；3 秒后仍未完成则先发 JSON 允许的换行空白，之后每 3 秒继续发空白，最后追加**一份完整的 `chat.completion` JSON**。成功的响应仍为 `application/json`，正文内容不添加“请稍等”，不丢字、不返回 SSE、不让客户端轮询，也不提前返回一个假的成功回答。客户端 JSON 解析会自然忽略开头空白。流式请求走现有 SSE 处理。

这个办法只能帮助每次读取等待 10 秒的客户端。平台若计算整次请求总时长、在中间缓冲了全部响应，或者按应用层等待完整答案的时间计时，仍会约 10 秒失败，需要平台调整或上游提速，不能靠中转单方面保证。

**错误状态限制**：快速参数错误、鉴权错误等仍保留原 HTTP 状态；但一旦开始发空白，下游 HTTP 200 就已提交。此后上游失败仍返回 OpenAI 的 `{"error":{...}}` JSON，无法再把 HTTP 状态改为 4xx/5xx，部分 SDK 的异常类别会与原入口不同。这是可选兼容模式的明确代价；要求严格 HTTP 错误状态语义时使用原入口。此模式不自动重放业务；连接尚未建立前的重连策略见下文。

```ini
JSON_KEEPALIVE_AFTER=3
JSON_KEEPALIVE_INTERVAL=3
```

## v2 文档与会话 ID

`智能助手接入文档_v2.md` 相比旧版新增了请求头 `session_id` 和屏蔽卡片要求，没有补充业务能力清单、用户登录或支付流程。

```ini
SESSION_HEADER=session_id
```

上游会话头按 v2 配置为 `session_id`。取值优先使用客户端 `session_id`，兼容旧的 `x-session-id`；若均未提供，则每次请求生成新的随机 ID。相同对话需由调用方复用同一个 ID，不同用户和新对话需使用不同 ID。代理不会按共享 apiKey、IP、相同首句或相似问题推测会话，避免合并不同用户。

平台若只发送标准 `messages` 而不提供稳定会话标识，中转仍会完整转发历史，但是否有效取决于上游是否读取这些历史。中转不能从缺少身份标识的请求中可靠推导一条跨请求会话。`session_id` 是上下文标识，不是个人账号登录凭据。已有 `.env` 中旧的 `SESSION_HEADER=x-session-id` 必须一并升级，不能只替换源代码。

旧版对其他头名称的串话/历史测试不能直接用来断定 v2 的 `session_id` 仍有相同问题，需要按新版重测。

本次灰度受控测试：相同 `session_id` 能回忆上一轮随机测试口令，另一 ID 明确回复未记录。新 ID 仅依赖 `messages` 历史的测试未命中口令，不能据此保证无会话头时的多轮能力。这是有限样本，不代表全部用户身份和业务隔离已验收。

不必查看平台后台日志：中转接受请求时记录 `session_source`、`session_hash`（ID 的脱敏摘要）、`messages` 和 `user_messages` 条数，不记录消息正文、原始会话 ID 或密钥。正式聊天可用无个人信息的测试口令验证：会话 A 记口令并追问；平台新建独立会话 B 后询问是否记得；再回到 A 追问。A 的稳定会话摘要应相同，B 应不同。`session_source=generated` 表示客户端没有传上述会话头；若历史条数也始终只有一条，则缺少跨轮上下文来源，不能当作已支持多轮。刷新页面不等于新建独立会话。

## 未提供会话头的平台：文字历史适配

本服务器已启用 `PACK_HISTORY_WITHOUT_SESSION=true`，代码默认值和示例配置为关闭，其他部署可按需开启。

2026-10-21 的外部聊天日志显示，平台所有受查请求均未提供会话头，虽然 `messages` 条数递增，上游会话每次都变化。受控测试证实：仅在 `messages` 历史中提供虚构航站楼信息，上游追问时说没有这段信息；把相同文字记录作为本轮输入后能答出 T3。因此单纯转发历史不能保证该上游支持追问。

启用后，仅在没有显式会话头、记录为普通文本多轮、且最后一条为用户消息时，将完整用户/助手记录序列化放入本次用户输入，并明确要求回答最后的问题。原有前置 system/developer 消息仍保持独立角色。不会总结、删改旧文本，不按 IP、密钥或首句绑定会话，不在中转保存共享对话状态。客户端已有会话头、工具调用、多模态、未知消息字段或对话中间的系统消息均保持原样。

该方法只补充本轮可见文字上下文，不能恢复上游之前的工具内部状态、用户登录、账号权限、订单确认或支付状态。历史增长也会增加输入长度；本次没有承诺整体提速。没有传来的助手回答仍无法补造。关闭该开关可恢复原样透传。

## 请求诊断日志

兼容性检测和正式聊天只要到达中转，都能记录诊断信息。除 `/healthz` 外，每个进入应用的请求在鉴权和参数校验前生成独立 `request_id`，响应头也返回 `X-Request-Id`。因此 401、参数错误、JSON 格式错误和路径不存在也可追踪。日志包括请求时间、方法、路径、客户端地址、HTTP 状态和响应头等待时间；校验通过后还有流式模式、会话来源与摘要、消息条数，以及上游处理结果和耗时。流式结束日志包含首段正文耗时、心跳次数、等待提示和断开状态。新增 `first_upstream_event_ms`、`first_reasoning_ms`、`first_upstream_text_ms`、`reasoning_chars`、`tool_starts`、`tool_results`，区分上游沉默、思考和工具阶段。接受请求时记录思考参数、最后 12 条消息的角色/内容类型/长度，以及末条文本用户消息的脱敏摘要；这些摘要只用于诊断，不用于生成或合并会话。没有相应内容时，首段时间为 `None`。这些字段只反映接口公开输出；没有思考文字或工具事件，不能证明上游内部没有思考或调用工具。`history_mode` 记录是否使用了文字历史适配。

JSON 保活入口提前提交的 HTTP 200 只代表响应头已发出；后续的 `stage=json_keepalive_result result_http_status=...` 记录最终结果原本应有的状态码，排查时需同时查看上游错误日志，不能仅凭访问日志中的 200 判断业务成功。

应用诊断日志不保存 Authorization、原始会话 ID、完整提问、完整回答或全部原始 SSE 事件。这是排障日志，不是问答内容存档。未到达中转的请求无法记录；兼容性检测和聊天使用相同接口时，也不能仅凭路径自动认定是哪种平台操作，需结合测试时间或请求 ID。

在服务器实时查看：

```bash
sudo journalctl -u openai-proxy -f -o cat
```

按某次请求定位：

```bash
sudo journalctl -u openai-proxy --since '30 minutes ago' -o cat | rg 'request_id=替换为实际请求ID'
```

日志由 systemd journal 管理；保存时长取决于服务器的 journald 配置，并非永久保存。

## 关键配置

升级时检查已有 `.env` 中这四项，旧值会覆盖代码默认值：

```ini
TEXT_END_SEPARATOR=''
NONSTREAM_FINAL_SEGMENT_ONLY=false
STRIP_CARDS=true
EMIT_REASONING=true
```

`NONSTREAM_FINAL_SEGMENT_ONLY=true` 仍保留为旧配置兼容行为，但不符合本次“保留全部正文”的要求。

其他配置及 Ubuntu 安装步骤见 `部署方案.md`。已有 `.env` 中的密钥应留在原位置，不放入交付包或测试样例。

## 本地运行与测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python tests/offline_test.py
```

回归测试不调用真实航旅上游。`tests/fixtures/` 保存 2026-10-21 的公开航班/天气查询响应体，不包含 Authorization，用来逐字核对“只删除卡片”。`offline_test.py` 在本机 18100/18101 启动模拟上游和服务，使用官方 OpenAI SDK 严格解析，包含跨块卡片、工具结果和静默心跳验证。另新增真实公开火车票和机票入口响应回放，核对只删卡片时正文仍逐字一致。

本次验证通过：70 项协议、内容保留、会话头、请求诊断、JSON 保活、慢首包和断连收尾回归测试、卡片随机切分 2000 次及边界测试、官方 OpenAI SDK 3.35.1 的流式和非流式严格解析。模拟上游 1.4 秒才发送 HTTP 响应头，SDK 读取超时设为 0.5 秒，流式和可选非流式 JSON 保活入口均能完成；慢答仅一次提示、快答无提示、JSON 格式输出不受文字提示污染。依赖使用与 Ubuntu 现有部署一致的 FastAPI 0.115.6、Uvicorn 0.34.0、httpx 0.28.1。

实际连接测试使用 `tests/sdk_test.py`，只发送问候和测试对话。涉及下单、值机或退改签时，不要使用会重复调用的测试脚本。

手动启动开发服务（先填写本地 `.env`）：

```bash
.venv/bin/uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

## 后续部署

更新顺序为先修改本地并通过测试，再部署到现有服务器。部署时需先备份代码和 `.env`，上传源码后核对上述四项配置，再重启 `openai-proxy` 并验收；依赖版本未变，无须重新安装系统环境。后台地址和 modelId 无须因本次改动改变。

后台地址：`http://162.14.82.104:8000/v1/chat/completions`；OpenAI SDK 的 base_url 使用 `http://162.14.82.104:8000/v1`。

## 业务边界

用户身份、会话隔离、支付、业务确认和幂等仍由上游与调用平台负责。中转传递指定的会话头，避免共享 HTTP 客户端 Cookie 自动串到下一位用户，但不能保证上游真的按会话隔离。

如果上游返回原生待执行 `tool_calls`，中转会按 OpenAI 字段保留它；调用平台仍需具备执行该工具的能力。把确认/订单事件转成文字，也不会自动增加平台缺少的按钮、登录或支付能力。

适配层不自动重放已经发送的业务请求；仅 ConnectError、ConnectTimeout、PoolTimeout 可在连接建立前重连一次。错误响应带 `X-Should-Retry:false`，支持此头的 SDK 会停止自动重试；其他客户端是否遵循需自行确认。报错也不能证明上游操作未发生。

## 2026-10-21 扩展验证

本轮服务器直连灰度接口进行 11 个只读样本测试，并将捕获的公开响应通过已部署代码回放，验证流式/非流式格式、正文一致和删除卡片。这不等于全部功能已通过管理平台端到端验收。

- 航班动态、火车票车次/余票/价格，以及用户提供虚构行程的统计返回了结果，完成样本的中转回放通过。
- 机票查询本次上游自行尝试后明确没有查到结果，只返回查询入口卡片；不能记作票价查询成功。
- 酒店查询、账号个人行程统计在上游约 60 秒断流；酒店入口未收到完成标记，火车预订入口返回上游 HTTP 502。本次未通过，不能据此断定这些能力永久不支持。
- 机票预订入口返回 `jump_card`，仅含 `pageId` 和 `jumpParams`，没有可点击 URL。删除卡片后普通文字平台不能执行 App 跳转；需上游提供官方 H5/深链接规则或平台增加跳转能力。实际下单、占座和支付均未执行。
- 所有样本均未输出思考文字。高推理/开思考与低推理/关思考的一次同题对照总耗时分别约 52/32 秒，另一个开思考样本也约 32 秒；不足以证明开关能稳定提速，生产请求参数保持透传。

## 目前能确认的功能

- **已实测接口返回**：普通对话、公开航班信息、机场天气；航班和天气保存了真实上游响应，并回放验证只删卡片。该验证证明响应和转发，不代替核验每条实时业务事实。
- **具备转发能力，尚待逐项验证**：航线航班列表、机票信息、机场设施和服务、行李与乘机规则等。上游若返回正文或工具业务结果，就会进入标准 `content`。不能仅根据助手自称支持就宣布已验收。
- **本次尚未完成的查询**：西安咸阳到北京的航线列表在测试设定的 90 秒内只收到部分正文，没有完成标记，主动结束测试；中转日志记录客户端断开并取消上游读取，没有伪造成功。保活不能保证这类复杂业务及时完成。
- **依赖身份和上游业务权限**：个人行程、订单、会员等。现有文档没有用户登录凭据、身份绑定或会话隔离规则；共享接口密钥不等于个人登录身份，不能保证可用。
- **尚未验证实际办理闭环**：值机选座、购票支付、退改签等。上游若在同一对话内完成，返回结果可传递；若依赖 App 卡片、登录、支付、确认按钮或客户端执行原生工具，当前平台仍缺少这些交互。仅做文字包装不能补齐。
- **明确移除**：所有卡片本体及其中独有的数据和操作入口。用户已选择移除卡片；不能同时保证依赖卡片的所有交互功能完整。

后续业务文档至少需要说明：能力/工具清单、业务结果与卡片的数据结构、用户授权和会话标识、办理确认/幂等/支付流程、错误码。对个人或办理业务不能用缓存他人的结果、自动重试或模拟成功来填补缺少的协议。


### 完整问答排障记录（2026-10-21）

经授权启用 `CONVERSATION_LOG_ENABLED=true` 后，每个鉴权成功的聊天请求单独保存一个 JSONL 文件。记录平台实际传入的完整 JSON、历史适配后实际发往上游的 JSON、过滤前的每一行上游 SSE，以及中转实际发向平台的 HTTP 正文。流式、非流式、JSON 空白保活入口均覆盖，记录器旁观数据，不改写传输内容。请求 ID 与响应 `X-Request-Id`、journald 的 `request_id` 一致，每条记录包含 UTC 时间、累计毫秒与序号。

`proxy_wait_notice` 明确标识本地等待提示；`upstream_done` 标识观察到上游 `[DONE]`。`request_end.response_sent_complete` 仅表示应用已把完整 HTTP 正文交给 ASGI 服务器，不保证前端已展示、语音已播完或业务回答正确。上游 SSE 使用 httpx 的解码及换行规范化结果，保存的是卡片过滤前的内容，不是 TLS/压缩网络包；HTTP 错误保存状态和已读取的响应体。旧版本没有保存的正文无法追溯。

鉴权头、Cookie 不记录；会话 ID 仅存摘要，配置中的密钥会脱敏。完整问答本身可能含个人行程，日志目录 0700、文件 0600，不提供公网查询接口。默认保留 3 天，单请求最多 16 MiB，达到上限明确写 `capture_truncated`。新请求触发清理（跨进程每分钟至多一次）；只清理过期或超过 256 MiB 软上限的已结束记录，活跃请求可能暂时超出软上限。磁盘或权限错误写入运行日志，继续转发请求。

当前服务器启用的目录是 `/var/log/openai-proxy/conversations`。查看最近记录：

```bash
sudo python3 /opt/openai-proxy/scripts/read_conversation.py --list
sudo python3 /opt/openai-proxy/scripts/read_conversation.py
sudo python3 /opt/openai-proxy/scripts/read_conversation.py --request-id <请求ID>
sudo python3 /opt/openai-proxy/scripts/read_conversation.py --request-id <请求ID> --raw
```

关闭正文记录：在 `.env` 设置 `CONVERSATION_LOG_ENABLED=false` 后重启服务。服务器的历史适配、卡片过滤、等待提示、模型及路由设置不因启用记录而改变。本次新增 7 项记录边界测试，总计 58 项单元测试，另外执行真实 HTTP/SDK 离线验证。


### 通用断连处理与语音失败收尾（2026-10-21）

处理规则统一作用于普通文本语音请求，不依赖城市、航班号或问题关键词。上游完整成功时，继续原样保留正文（按配置删卡片）；连接或生成失败时，中转记录真实原因并结束请求。

当前服务器设置 `VOICE_ERROR_FALLBACK_ENABLED=true`，仅用于现有 `/v1/keepalive/chat/completions` 的 `stream:true` 普通文本请求。失败时通过标准 `delta.content` 播报“出行服务暂时异常，本次未能取得完整结果……”并发送 `finish_reason:stop` 与 `[DONE]`。已输出部分正文时，保留该正文并明确提示回答尚未完整返回。这个结束信号表示**本地失败说明完成**，上游业务仍然失败；完整记录标识 `proxy_voice_failure`、`source_complete:false`，常规日志继续保留异常状态，不会记录为上游成功或补造上游 `upstream_done`。

这种语音兼容模式的 API 响应是完整的失败说明文字，调用方不能仅凭 HTTP 200、stop 或 `[DONE]` 认定业务成功。需要通过 APIError 判断故障的调用方应继续使用原 `/v1/chat/completions`。非流式接口仍返回 OpenAI error JSON；结构化 JSON、纯音频、已输出部分原生 tool_calls/function_call 的流也保留严格错误格式，避免把自然语言混入结构化内容或结束一份不完整的函数参数。

`UPSTREAM_CONNECT_RETRIES=1` 仅允许 ConnectError、ConnectTimeout、PoolTimeout 在连接建立前重连一次，默认等待 0.25 秒。ReadTimeout、WriteTimeout、WriteError、RemoteProtocolError、上游非 200 或 SSE 错误均不自动重放。用户问题是否“只读”不靠自然语言猜测；当前文档没有幂等协议，重放已接收的请求可能造成预订、值机或退改等操作重复执行。只有接口方提供幂等/业务状态协议后，才能扩展安全恢复。

`UPSTREAM_TOTAL_TIMEOUT=180` 为流式和非流式设置上游整轮上限，覆盖连接、重连、读取和聚合，0 可关闭。达到上限会取消本地上游读取并记录 `upstream_total_timeout`；无法据此判断远端业务已取消。成功的慢响应在总上限内继续完整返回；6 秒等待提示和空内容心跳继续按原配置工作。

新增 12 项故障回归，总计 70 项单元测试；另在真实本地 HTTP 与严格 OpenAI SDK 下验证了语音无结果失败收尾、半途断流保留正文并提示未完成，以及原严格入口仍抛出 APIError。客户端断开会取消尚未发出的重连。上游实际稳定性仍需要接口方修复，中转不能生成缺失的业务结果。


### 同一份问答记录中的异常类型

断流、上游读超时、缺少完成标记和整轮超时会新增 `upstream_stream_error`，包含 `error_type`、`code`、`source_complete:false`。例如 `RemoteProtocolError / upstream_error` 表示读取中连接异常，`incomplete_stream` 表示未获得完整结束信号，`upstream_total_timeout` 表示中转整轮时限达到。连接建立前的异常继续由 `upstream_connection_error` 记录，HTTP 拒绝由 `upstream_response` 和错误响应体记录。新增字段不修改请求、答案、卡片规则或重试策略，也不保存异常原文。新增两项测试覆盖流式/非流式连接异常、内容不完整及总超时，单元测试总数 72。
