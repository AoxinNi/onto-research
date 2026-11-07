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

容器监听 8080，健康检查为 `GET /healthz`。工作台通过 CloudBase Auth 登录，默认仅接受受邀账号。

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | CloudBase PostgreSQL 实例直连地址，使用控制台要求的 SSL 配置 |
| `WORKBENCH_CLOUDBASE_ENV_ID` | 提供身份认证的 CloudBase 环境 ID |
| `WORKBENCH_PUBLIC_ORIGIN` | 浏览器访问工作台的 HTTPS 来源，不带路径或末尾斜杠 |
| `WORKBENCH_ALLOWED_USER_IDS` | 受邀账号的 CloudBase 用户 ID，以逗号分隔 |
| `WORKBENCH_REGISTRATION_PROVIDERS` | 自助注册渠道，可填 `sms`、`wechat` 或 `sms,wechat`，默认关闭 |
| `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`WORKBENCH_MODEL` | 可选模型服务配置 |
| `WORKBENCH_MODEL_SETTINGS_KEY` | 云端保存个人模型凭据用的 AES-256 密钥，32 字节随机值的 Base64 编码，保存在服务环境中并跨版本保留 |
| `WORKBENCH_MODEL_ALLOWED_HOSTS` | 云端自定义模型可访问的 HTTPS 域名，以逗号分隔，默认 `api.anthropic.com` |

目前使用单个工作台实例。项目连接依赖 PostgreSQL 会话级 `search_path`。启动时初始化 `onto_research` 注册表，首次创建项目时会在 `onto_research_p_*` 下应用项目迁移，因此数据库登录角色需要访问注册表并创建项目 schema。

先在 CloudBase 身份认证里启用账号密码登录并准备首个账号，再把该账号的用户 ID 加入允许名单。凭据由用户在登录页输入。工作台会话最长两小时，可通过退出登录立即撤销。

新项目由登录账号自动拥有。已有项目升级时保留为未归属，云端账号看不到。备份并停止旧版共享口令入口后，数据库运营者可按已核实的项目 ID 逐个绑定：

```sh
.venv/bin/python -m ontology.cli project-claim <project-id> <cloudbase-user-id>
```

本地单用户 Web 入口只读取未归属项目。日常开发请配置独立的本地开发库。CLI 和本地 stdio MCP 使用数据库运营凭据，仅供受信任的运营者操作。

私有数据启用后，回滚使用支持账号隔离的版本。旧版共享口令服务接入同一数据库时可读取项目，因此切流后应关闭旧版访问入口，并保持项目 schema 仅供后端访问。

### 自助注册

- 短信需要上海地域、已启用的短信验证码登录，以及已通过审核的短信签名和模板。页面接受中国大陆手机号，验证码验证后由 CloudBase 创建用户或登录现有用户。应用对发送尝试设有同号码 60 秒冷却、滚动 24 小时 5 次，以及全站每分钟 10 次、滚动 24 小时 100 次上限。
- 微信需要配置 CloudBase 的微信开放平台网站应用身份源并启用自动注册。将工作台 HTTPS 域名加入 CloudBase 安全域名及微信授权回调域名，应用回调路径固定为 `/auth/wechat/callback`。AppSecret 在 CloudBase 控制台配置。
- 两种方式以 CloudBase 返回的 UID 进入工作区。同一 UID 复用既有项目；尚未关联到原账号的手机号或微信会进入自己的工作区。工作台的注册记录仅保存 UID、首次注册渠道和时间。

按渠道配置后做一次真实注册、退出和重新登录验收，再对用户开放。关闭渠道会收起其注册和登录入口，已有工作区和当前有效会话仍可使用。需要封禁账号时，先在 CloudBase 停用用户，再移除工作台的注册记录或邀请名单条目以撤销访问。

### Agent 模型设置

从工作台顶部打开「平台设置」。自定义接口须支持 Anthropic Messages，填写 Base URL、模型 ID、鉴权方式和 API Key，保存后用「测试已保存连接」发起一次短模型请求。保存时留空 API Key 可保留同一连接的旧凭据，修改 URL 或鉴权方式时需重新输入。已有对话中显式选过的模型 ID 优先于平台默认模型。

云端按登录账号保存配置，API Key 加密入库。部署者仅应把可信的公网模型域名加入允许名单，云端发起请求时禁用 HTTP 跳转。发布新版本时保留加密密钥，否则现有凭据需重新录入。

本机运行工作台时可选择 Magpie 或 CC Switch。Magpie 读取其本地设置里的端口，默认 3425。CC Switch 默认 15721，开启 Claude Code 路由后可读取其写入 Claude 设置的本机地址。工作台使用路由暴露的 Messages 接口和可用模型目录，本机配置以仅用户可读写的权限保存在 `~/.config/onto-research/model.json`（或 `$XDG_CONFIG_HOME` 下的对应位置）。CloudBase 运行在云端，无法直接访问电脑的回环端口。

本地配置、密钥、测试数据和虚拟环境都应排除在上传目录外。上传到云托管的源代码目录仅需 `Dockerfile`、`.dockerignore`、`pyproject.toml`、`ontology/` 和 `workbench/`。容器进程以非 root 用户运行。

## 测试

CI 使用临时的 PostgreSQL 16。测试会删除并重建测试库的 schema。运行测试前将 `TEST_DATABASE_URL` 指向本机可销毁的 `onto_research_test`，再执行 `.venv/bin/python -m pytest -q`。
