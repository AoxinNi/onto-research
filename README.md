# onto-research

科学研究 Agent 工作台。通过类型化本体组织研究问题、概念、证据和关系。Agent 提议的结构改动进入变更集，经确认后形成历史版本。

首次启动的项目列表为空。创建项目时初始化类型与空表。数据绑定由用户明确添加，自动采集、新闻、链上事件与自动报告默认关闭。

## 本地开发

用 Python 3.11 或更新版本安装依赖。将 `.env.example` 复制为 `.env`，设置 PostgreSQL 的 `DATABASE_URL`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
./start-workbench.sh
```

访问 <http://127.0.0.1:8766>。本地入口按本机单用户模式运行。用 `.venv/bin/python -m ontology.cli projects` 验证数据库连接和项目列表。对单一项目执行命令时传入 `--project <id>`。

## CloudBase 云托管预发布

容器监听 8080，健康检查为 `GET /healthz`。用部署环境变量配置：

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | CloudBase PostgreSQL 实例直连地址，使用控制台要求的 SSL 配置 |
| `WORKBENCH_STAGE_PASSWORD` | 私有预发布入口的随机密码，至少 32 个字符，用户名为 `research` |
| `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`WORKBENCH_MODEL` | 可选模型服务配置 |

目前使用单个工作台实例。项目连接依赖 PostgreSQL 会话级 `search_path`。启动时初始化 `onto_research` 注册表，首次创建项目时会在 `onto_research_p_*` 下应用项目迁移，因此数据库登录角色需要访问注册表并创建项目 schema。

本地配置、密钥、测试数据和虚拟环境都应排除在上传目录外。上传到云托管的源代码目录仅需 `Dockerfile`、`.dockerignore`、`pyproject.toml`、`ontology/` 和 `workbench/`。容器进程以非 root 用户运行。

## 测试

CI 使用临时的 PostgreSQL 16。测试会删除并重建测试库的 schema。运行测试前将 `TEST_DATABASE_URL` 指向本机可销毁的 `onto_research_test`，再执行 `.venv/bin/python -m pytest -q`。
