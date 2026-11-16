# AI Processing Service

StudyHub 的异步文档处理 worker，无 HTTP API。消费 Content 的 `document.uploaded`，
下载指定版本文件，提取/转写文本，生成单文档摘要（含关键事实）及原文分块向量，
回报 Content，并发布结果供 Search 使用。

## 本地运行

从仓库根目录创建 `.env`（可使用已有 setup 脚本），启动现有 Content Compose 栈。
填写根 `.env` 的 AI 配置后：

```bash
cd ai-processing-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py --check-config
python main.py
```

根 `.env.example` 已配置本机 Ollama（Qwen3 4B + BGE-M3）和原生 MLX 转写地址。
已有 `.env` 不会被自动修改，请将 AI 配置同步到根 `.env`。缺少必要配置时在消费消息前退出。
`--check-config` 只检查配置，不请求外部服务。

| 配置 | 用途 |
| --- | --- |
| `AI_SUMMARY_URL` / `AI_SUMMARY_MODEL` / `AI_SUMMARY_KEY` | 摘要和关键事实，默认原生 Ollama `/api/chat` endpoint |
| `AI_EMBEDDING_URL` / `AI_EMBEDDING_MODEL` / `AI_EMBEDDING_KEY` | 默认原生 Ollama `/api/embed` endpoint |
| `AI_TRANSCRIPTION_URL` / `AI_TRANSCRIPTION_MODEL` / `AI_TRANSCRIPTION_KEY` | Mac 原生转写服务 endpoint；处理音频时才要求配置 |
| `AI_CONTENT_URL` | 本机默认 `http://127.0.0.1:8000`；容器内使用 `http://content-api:8000` |
| `STUDYHUB_PROCESSING_TOKEN` | 与 Content 配置一致的 token |
| `STUDYHUB_DATABASE_URL` / `STUDYHUB_AMQP_URL` | PostgreSQL / RabbitMQ 连接串 |
| `AI_HTTP_TIMEOUT` | HTTP 每次网络操作超时秒数，默认 300 |
| `AI_CHUNK_SIZE` / `AI_CHUNK_OVERLAP` | 字符数，默认 2000 / 200；不是 token 数 |
| `AI_MAX_ATTEMPTS` | 每个文档版本的处理尝试次数，默认 3 |

默认 `AI_MODEL_PROVIDER=ollama` 使用原生接口，关闭 thinking / streaming，
设置 4096 token 上下文和最多 512 个输出 token；embedding 禁止静默截断。
每个文档的摘要、向量阶段在 finally 中分别请求卸载模型，避免长期同时驻留。
`AI_OLLAMA_CONTEXT` 和 `AI_SUMMARY_MAX_TOKENS` 可调整上下文及输出上限。
长摘要按保守字符预算递归归并；字符预算不是精确 tokenizer 计数，仍需用实际材料验证。
输出达到 token 上限会明确失败，不把截断的摘要作为完整结果。

切换第三方兼容接口时设置 `AI_MODEL_PROVIDER=openai` 并更换完整 URL / 模型 / KEY。
两项文字任务当前共用 provider 设置。转写固定使用 multipart 请求并读取 `text`。
KEY 可为空以支持本地模型。
摘要及关键事实当前合并为一个文本字段，未实现独立结构化 JSON 字段。

支持 UTF-8 TXT/MD/CSV、文字型 PDF、DOCX，以及 MP3/WAV/M4A/MP4/OGG/WEBM/FLAC
转写。PDF 不执行 OCR；旧版 DOC 不支持。原文件最大 5 MiB，提取文本最多
200000 字符。长文档逐块摘要再递归合并；原文块用于 embedding，结果含字符偏移。

## 持久化与重试

启动时创建服务自有的 `ai_processing.jobs` 表，不读写 Content 业务表。
本地开发默认复用现有 PostgreSQL 实例和数据库，使用独立 schema；生产应使用
独立数据库/角色。当前迁移只有幂等的初始化 SQL，后续 schema 变更需增加迁移机制。

按 `(documentId, documentVersion)` 去重，PostgreSQL session advisory lock 阻止
同版本并发处理。处理成功后先保存结果，再回调 Content、发布消息，最终 ACK。
回调或发布失败保留暂存结果，重投时无需再次调用模型。发布使用 publisher confirms
和持久化消息；发布成功但更新任务记录前崩溃仍可能重复发布，结果 eventId 保持不变，
Search 必须幂等消费。

处理失败最多尝试配置次数，再报告 `failed`；回调/发布等交付错误每隔至少 3 秒重试，
不计入模型处理次数。回调 409 不覆盖已有状态，会持续重试并需要人工排查。
Content 返回 404 时跳过并清除暂存结果。格式错误事件进入 `processing.invalid-events`。
消费线程等待模型时仍维持 RabbitMQ heartbeat。强制停止时未 ACK 的消息可重投。

模型步骤尚无逐块检查点，处理阶段中途失败/进程崩溃可能再次产生模型调用费用；
跨外部模型调用无法保证 exactly-once。已完成任务会清除暂存输出，成功消息由 Search
保存。未实现重处理 API、删除事件监听、监控或 OCR。

## Search 对接：待小组确认的协议

新增 durable topic exchange `processing.events`，routing key `document.processed`，
绑定 durable queue `search.processing-results`。这是本服务提出的契约，现有 Search
实现尚未对接，不代表四服务已联调完成。Content 的现有队列/事件格式不变。

```json
{
  "schemaVersion": 1,
  "eventId": "稳定的结果事件 UUID",
  "eventType": "document.processed",
  "documentId": "文档 UUID",
  "documentVersion": 1,
  "summary": "摘要与关键事实",
  "embeddingModel": "配置的模型名",
  "summaryModel": "配置的模型名",
  "chunks": [
    {"chunkIndex": 0, "startOffset": 0, "endOffset": 4,
     "text": "原文内容", "embedding": [0.1, 0.2]}
  ]
}
```

Search 负责写入向量数据库，处理版本和删除事件的乱序，并在返回搜索结果前执行
Content 的实时权限检查。Content 的 `succeeded` 在这里表示 AI 结果已持久暂存，
不代表 Search 已建好索引。最终回调后仍可能发生删除，Search 必须防止迟到结果
重新激活被删除文档。大量向量直接放消息适合初期联调，后续可改为结果引用。

## Docker 与验证

已提供 Dockerfile，但未修改根 Compose。若加入 Compose，需要提供上述环境变量、
数据库权限，并使用容器内地址 `db:5432`、`rabbitmq:5672` 和 `content-api:8000`。

```bash
python -m unittest discover -s tests -v
```

测试使用假的 Content/模型及内存任务记录，覆盖重投、交付恢复、失败上限、
删除、文本分块及 pipeline；不代表真实 PostgreSQL/RabbitMQ/模型端到端验证通过。


## Mac 原生模型接入

Ollama 原生运行在 macOS；Linux worker 容器不安装 MLX。
已下载 `qwen3:4b`、`bge-m3` 后，开启 Ollama 应用即可提供接口。

MLX Whisper 需要在 Mac 单独启动 HTTP 桥接服务：

```bash
# 从 ai-processing-service/ 执行；已有兼容虚拟环境也可复用
python3 -m venv .venv-native
source .venv-native/bin/activate
pip install -r native/requirements.txt
# 若尚未安装 ffmpeg：brew install ffmpeg
python native/transcription_server.py
```

默认监听 `127.0.0.1:8001`，健康检查为 `GET /health`（只检查 HTTP 存活，不加载模型）。
转写接口为 `POST /v1/audio/transcriptions`，字段 `file` 和 `model`。
默认只允许配置的 `mlx-community/whisper-small-mlx` 模型；首次使用缺少缓存时会下载。
每次转写在单独子进程执行，退出后释放模型内存；同一时间只接受一个转写任务。
最多接收 5 MiB 音频，临时文件在请求结束后清除。

### Docker worker 调用 Mac

在容器环境覆盖（不是修改 Mac 原生服务使用的地址）：

```dotenv
AI_CONTENT_URL=http://content-api:8000
AI_SUMMARY_URL=http://host.docker.internal:11434/api/chat
AI_EMBEDDING_URL=http://host.docker.internal:11434/api/embed
AI_TRANSCRIPTION_URL=http://host.docker.internal:8001/v1/audio/transcriptions
```

原生转写服务如需监听非 loopback，设置 `AI_TRANSCRIPTION_BIND=0.0.0.0`，
同时给 Mac 服务和 worker 配置相同的非空 `AI_TRANSCRIPTION_KEY`。
Ollama 如无法从容器访问，需要调整本机 Ollama 的监听配置并限制局域网访问。
根 Compose 尚未加入 AI worker，本次只提供模型对接代码和容器调用配置说明。

HTTP 超时默认 300 秒，超时不保证服务端计算立即停止；Mac 睡眠或模型服务未启动会导致调用失败。
多个 worker / Search 同时调用模型时仍可能出现资源竞争，当前建议一次处理一个任务。

本机若已有 RabbitMQ 占用 5672，可在根 `.env` 设置 `RABBITMQ_HOST_PORT=5673`、
`RABBITMQ_MANAGEMENT_PORT=15673`，并确保宿主机 `STUDYHUB_AMQP_URL` 使用该端口。
Compose 内部通信仍使用 `rabbitmq:5672`。
