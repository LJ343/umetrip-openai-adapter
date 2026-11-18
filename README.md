# FlyAI 旅行助手 · OpenAI Chat Completions 接口

将 [FlyAI 官方 Skill](https://github.com/alibaba-flyai/flyai-skill) 的八类旅行查询封装成可多轮聊天的 OpenAI 兼容接口。服务内部通过兼容 OpenAI 工具调用的大模型理解需求、选择命令和组织追问，调用官方 CLI 获取结果；**无需安装 OpenClaw**。

本分支为 **`codex/flyai` / 独立8002服务**。[main 分支](https://github.com/LJ343/umetrip-openai-adapter/tree/main)维护航旅纵横 qianrang / 8000适配器，由航旅纵横上游处理业务。两个分支使用不同配置、依赖和 systemd 单元；请分别部署。

## 查询能力

- `search-flight`：航班、机票方案查询。
- `search-train`：火车、高铁车次查询。
- `search-hotel`：酒店查询。
- `search-poi`：景点、兴趣点查询。
- `search-marriott-hotel`：万豪集团酒店查询。
- `search-marriott-package`：万豪酒店套餐查询。
- `keyword-search`：旅游商品及其他旅行品类的关键词搜索。
- `ai-search`：自然语言语义查询、复杂意图和组合行程搜索。

八类工具始终可供模型选择；同一问题可以组合多个工具。专属查询优先由对应工具执行，语义搜索用于复杂需求或补充查询。实际返回范围、字段、价格及库存由上游决定，不能保证每个问题都有结果。查询与原始链接跳转不等于自动下单、付款、值机、改签或退款。

## 当前结果策略

本分支包含2026-11-18的8002维护版本：

- 不再用本地推测的起终点、日期、预算等覆盖模型已填写的业务参数。保留官方参数格式校验及明确的 CLI 兼容映射。
- 按上游顺序展示全部返回条目，不默认限制三条，不按本地推测的条件再删除结果。
- `ai-search` 的自然语言结果继续展示；不再用本地日期扫描整段屏蔽。
- 面向用户的供应商品牌文字使用中性措辞；原始图片与预订 URL 保持可用的 Markdown 链接，不改写 URL。
- 价格、日期和链接来自同次工具结果，不让最终模型重新生成一套报价或预订地址。
- 服务默认身份为“德创数字的航班助手小德”，见 [app/agent.py](app/agent.py) 和 [app/grounding.py](app/grounding.py)。

完整展示也会保留上游自身的旧信息、地域不匹配或前后矛盾。`ai-search` 不等同于实时航班/高铁库存接口；需要现价与余票时，应结合对应专属工具的本次结果。资料和测试样本中带日期的报价、时刻及“明天”只属于当时上下文，不代表当前结果。

## 安装和配置

需要 Python 3.10及以上、Node.js 18及以上，以及支持 OpenAI `tool_calls` 的大模型接口。准备三类独立凭据：大模型 API Key、FlyAI 正式 API Key、调用本中转服务的访问 Key。前两者分别向对应服务申请，第三者由配置脚本生成。

```bash
git clone --branch codex/flyai https://github.com/LJ343/umetrip-openai-adapter.git flyai-openai
cd flyai-openai
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
node --version
.venv/bin/python scripts/install_runtime.py
cp .env.example .env
chmod 600 .env
.venv/bin/python scripts/configure.py
```

在交互配置中输入实际的大模型 Base URL、模型名和两种上游 Key；Key 隐藏输入，不出现在命令行参数中。随后检查 `.env` 的 `NODE_BIN` 是否为 `command -v node` 返回的绝对路径。模型有专用参数时通过 `LLM_EXTRA_BODY_JSON` 配置，例如关闭思考的参数需按实际模型支持情况填写。

安装脚本依据 [manifest.json](docs/upstream/manifest.json) 下载官方 `@fly-ai/flyai-cli` **1.0.16**，先验证固定的 SHA-512 完整性再提取运行文件，不执行业务查询。仓库不包含 Node 二进制、CLI 安装产物或实际凭据；不需要全局安装 Skill 或 CLI。

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8002 --workers 1 --no-access-log
```

## OpenAI 兼容调用

- 聊天接口：`POST http://127.0.0.1:8002/v1/chat/completions`。
- OpenAI SDK 的 `base_url`：`http://127.0.0.1:8002/v1`，不要重复追加 `/chat/completions`。
- 要求填写完整接口 URL 的平台：填写 `http://你的服务器:8002/v1/chat/completions`。
- 对外模型名：`flyai-travel`，可通过 `PUBLIC_MODEL` 修改；它与内部使用的大模型名分别配置。
- `Authorization: Bearer <PROXY_API_KEYS中的访问Key>`；不能用大模型 Key 或 FlyAI Key 代替。
- 支持 `stream:false` 完整 JSON，以及 `stream:true` 的 SSE、空内容心跳和 `[DONE]`；可请求 `stream_options.include_usage`。
- 还提供 `GET /v1/models`、`GET /healthz` 和需鉴权的 `GET /readyz`。健康检查不代表上游业务已查询成功。

将独立访问 Key 放入当前终端的 `FLYAI_PROXY_KEY` 环境变量后：

```bash
curl http://127.0.0.1:8002/v1/chat/completions \
  -H "Authorization: Bearer ${FLYAI_PROXY_KEY}" \
  -H 'Content-Type: application/json' \
  -H 'session_id: trip-demo-001' \
  --data '{"model":"flyai-travel","messages":[{"role":"user","content":"帮我查明天西安到北京下午的航班"}],"stream":false}'
```

官方 Python SDK 示例（先在调用方环境安装 `openai`）：

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["FLYAI_PROXY_KEY"],
    base_url="http://127.0.0.1:8002/v1",
    timeout=260,
    max_retries=0,
)
for question in ["帮我查明天西安到北京下午的航班", "改成后天上午", "返程也看看"]:
    answer = client.chat.completions.create(
        model="flyai-travel",
        messages=[{"role": "user", "content": question}],
        extra_headers={"session_id": "trip-demo-002"},
    )
    print(answer.choices[0].message.content)
```

接口支持纯文本 `system/developer/user/assistant` 消息、单候选 `n=1` 及常见生成参数。工具调用由服务内部执行，不接收客户端自定义 `tools`，也不提供音频、多模态、Responses API 或任意 OpenAI 功能的全量兼容。响应里的可点击链接能否显示、是否被语音读出，取决于调用平台的渲染和播报方式。

## 多轮与保活

同一对话复用 `session_id` 请求头（兼容 `x-session-id`），服务按访问 Key 与会话 ID 隔离历史并保存到私有 SQLite。可只发送最新一条用户消息，也可提交一致的完整历史；冲突历史返回409。不同用户、新对话使用不同 ID；同会话不要并发提交。

没有会话头时，客户端须每次提交完整 `messages` 历史。服务不按 IP、访问 Key 或相似问题猜测会话。历史报价只能用于上下文，不能代替新一轮库存查询。

可选入口 `/v1/keepalive/chat/completions` 为非流式慢请求发送 JSON 允许的空白保活，最后仍只返回一个 JSON 文档；SDK 使用该入口时 `base_url` 为 `/v1/keepalive`。保活只能缓解读取空闲超时，不能突破平台的整次请求时限。响应头提前提交后发生的错误也无法再更改 HTTP 状态，需要读取最终响应内容。

## 测试和运维

```bash
.venv/bin/python -m pip install -r requirements-test.txt
NODE_BIN="$(command -v node)" .venv/bin/python -m unittest discover -s tests -v
```

测试使用模拟模型、模拟工具和脱敏历史结构样本，不调用真实旅行查询；CLI 检查仅执行八个命令的 `--help`。覆盖 SDK 严格解析、SSE、错误、会话隔离、工具参数及结果完整展示。请先执行安装脚本准备固定版本 CLI。样本中的跳转地址为 `example.com`，不能作为真实预订入口。

部署模板见 [deploy/flyai-openai.service](deploy/flyai-openai.service)，操作与独立回退见 [deploy/OPERATIONS.md](deploy/OPERATIONS.md)。默认两并发、单进程；保留鉴权、格式校验、请求容量、工具超时与资源限制。业务结果不做本地二次筛除，不表示移除这些运行保护。

`.env`、运行目录、会话库、完整问答、原始 SSE 和私有证据均不进入仓库。`RECORD_CONVERSATIONS` 默认关闭；开启后需保护私有 `state/conversations`，日志并不证明业务事实正确。排障时应对照同一请求的输入、实际工具参数、原始结果和客户端完整回复，不能只比较两次独立生成的答案。

## 上游资料

固定版本的官方能力与参数文档保存在 [docs/upstream](docs/upstream)，来源提交和 CLI 完整性见 manifest。复制的上游资料保留其 [MIT 许可](docs/upstream/LICENSE)，归属说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
