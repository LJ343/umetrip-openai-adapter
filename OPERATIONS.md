# 发布与回退

本项目把航旅 qianrang SSE 转成 OpenAI Chat Completions。服务器敏感配置保存在 `/opt/openai-proxy/.env`，不要加入 Git、报告或日志包。Git 只管理源码、空值配置模板、测试和文档；服务器问答日志、证据、备份和虚拟环境不进入仓库。

## 卡片引导语版本

`STRIP_CARDS=true` 继续删除卡片本体；在实际 `.env` 中显式设置 `STRIP_CARD_HINTS=true` 才启用纯卡片导航语清理。默认模板关闭此开关。

清理器只删除已确认的导航片段，不调用另一模型，不增加上游查询。普通正文、过程、工具业务结果、reasoning_content、查不到的答复、价格和限制继续保留。未知、引用和部分格式化内容保守放行。候选上限128字、200毫秒；该等待上限与本地扫描耗时是不同指标。

本地回归：

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/offline_test.py
```

正式服务仍为 `openai-proxy`，端口8000，平台 URL 和密钥沿用现有配置。更新源码前保存实际 `.env`、源码、systemd 单元及权限。不要用 `.env.example` 覆盖实际 `.env`。

## 2026-10-22 本次完整版本回退

发布前服务器备份目录：

`/opt/openai-proxy-backups/20261022-185430-before-card-hint-production`

其中保存本次发布前的原始源码、原始配置、systemd 单元和逐文件哈希/权限清单，目录仅root可读。虚拟环境和依赖本次未更改。

只核对备份和回退计划，不修改服务：

```bash
sudo python3 /opt/openai-proxy-backups/20261022-185430-before-card-hint-production/rollback.py --check
```

完整回退并重启8000：

```bash
sudo python3 /opt/openai-proxy-backups/20261022-185430-before-card-hint-production/rollback.py
```

回退会先另存当前文件，恢复原始文件和配置、移除本次新增文件、重启并核对健康状态与恢复后的哈希。问答日志不删除。该路径只用于本次发布；以后每次发布应创建自己的备份与回退脚本。

如只需暂停新清理规则，可将实际 `.env` 的 `STRIP_CARD_HINTS` 改为 `false` 后重启；这会继续使用新源码，完整版本恢复请用上面的回退命令。

Git 标签 `pre-card-hint-filter-20261022` 保存发布前源码，`card-hints-20261022.1` 保存此次最终规则源码（`card-hints-20261022` 为首次发布版本）。Git 标签不包含服务器密钥和 systemd 状态，完整部署回退以服务器备份为准。远端上传是否完成以实际 push 结果为准。
