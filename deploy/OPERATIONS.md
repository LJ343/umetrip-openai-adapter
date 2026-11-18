# FlyAI / 8002 独立部署与回退

本文件仅适用于 `codex/flyai` 分支。航旅纵横 / 8000 使用 main 分支及其独立部署方式。

## 独立布局

- 应用与虚拟环境：`/opt/flyai-openai`、`/opt/flyai-openai/.venv`。
- 系统用户与组：`flyai-agent`，专用于此服务，不加入旧服务的用户组。
- 配置：`/opt/flyai-openai/.env`，root:flyai-agent、0640，单独配置上游与访问 Key。
- 状态：`/var/lib/flyai-openai`，专用用户可写，目录权限0700。
- systemd：`flyai-openai.service`，监听8002、单 worker。安装前确认端口空闲。

按 README 安装本分支依赖和固定版本 CLI。`NODE_BIN` 须指向专用用户能读取和执行的 Node 路径，不要使用桌面用户家目录中的运行时；不需要为此替换旧服务的系统依赖。设置 `STATE_DIR=/var/lib/flyai-openai`；模板中已通过环境变量指定该目录。

在服务器创建上述独立用户、目录和配置后，先检查模板的路径、资源限额及执行权限，再安装单元：

```bash
sudo systemd-analyze verify deploy/flyai-openai.service
sudo install -m 644 deploy/flyai-openai.service /etc/systemd/system/flyai-openai.service
sudo systemctl daemon-reload
sudo systemctl enable --now flyai-openai
sudo systemctl status flyai-openai --no-pager
```

上述命令不负责创建用户、安装依赖或填写凭据。应用源码应只读，状态目录才可写；完整问答记录按需单独启用。

先验证本机 healthz、需鉴权的 readyz/models、流式/非流式与会话，再按需要提供外部访问。公网长期使用应配置 HTTPS。不要覆盖或重启 `openai-proxy`，不要将本分支部署到 `/opt/openai-proxy`，不要复用其 `.env`、数据库或端口。

## 版本更新与回退

更新前在仓库之外私有保存当前提交号、源码、配置和一致性数据库副本。新版本通过离线测试后只更新本服务，保留现有 `.env` 和状态目录：

```bash
sudo systemctl restart flyai-openai
sudo journalctl -u flyai-openai -n 60 --no-pager
```

若失败，只将 `/opt/flyai-openai` 的源码与必要运行依赖恢复到事先保存的 FlyAI 版本，再重启 `flyai-openai` 并验证；不把 main 分支当成8002的回退版本，不自动覆盖活跃数据库。配置变更须按备份另行核对。

停用新服务只需：

```bash
sudo systemctl stop flyai-openai
```

已有8000入口继续由其独立服务提供；使用者如需切回，应在调用平台更改 URL、模型名和对应访问 Key。

## 排障

响应头 `X-Request-Id` 与私有完整链路记录对应。`RECORD_CONVERSATIONS=true` 时，记录位于 `STATE_DIR/conversations`，包含问题、模型 SSE、工具输入输出及最终正文；保持私有，不上传到 issue 或 Git 仓库。容量或写入失败可能使记录不完整，核对终止标记后再作结论。

`query_contract` 中的 `argument_policy=model_passthrough` 与 `result_policy=all_source_items` 表示当前开放展示策略。模型到 CLI 仍有明确参数格式与机场代码映射，查询条件标题不等于每个返回条目都满足该条件。HTTP200、`stop` 和 `[DONE]` 说明协议完成，不保证上游查到了符合要求的库存或信息没有过时。
