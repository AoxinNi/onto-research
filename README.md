# onto-research

科学研究 Agent 工作台。通过类型化本体组织研究问题、概念、证据和关系。Agent 提议的结构改动进入变更集，经确认后形成历史版本。

首次启动的项目列表为空。创建项目时初始化类型与空表。数据绑定由用户明确添加，自动采集、新闻、链上事件与自动报告默认关闭。

### 类型包

创建项目时选择类型包（`ontology/schema/packs/`）：

- **研究**（默认）：问题、假设、论断、观测，以及概念、方法、主体、事件。
- **市场监测**：资产、叙事、事件、宏观指标、信号，即早期的 BTC 模型。

引入类型包之前创建的项目都按市场监测处理。已有项目可以在首页的项目菜单中选择「加入研究类型」，也可以执行下面的命令：

```sh
.venv/bin/python -m ontology.cli project-add-pack <project-id> research
```

两个包中同名的类型（概念、主体、事件）会合并：属性和枚举值取并集，两边都必填的属性才保持必填，所以已有节点仍然有效。

回滚说明：使用研究类型的项目不能用引入类型包之前的版本打开，旧版本不认识 `subject`/`reasoning` 层。

### 文献库

工作区左上角可以在「图谱」和「文献」之间切换。在文献模式里可以：

- 按 DOI、arXiv 编号、ISBN、OpenAlex 编号或网址添加文献，元数据由对应服务返回；也可以上传 PDF 或文本，或者导入 BibTeX、RIS、CSL-JSON 文件。PDF 中印有 DOI 时会自动补全题录。
- 阅读提取出的正文，选中一段生成摘录，再把摘录关联到图中的节点。关联有四种：陈述、支持、反驳、限定。这样的关联称为「依据」。你直接添加的依据立即生效；Agent 提出的依据和其他改动一样，要经过审核。两种依据都记入历史，可以撤销。
- 写笔记。用 `[@引用键]` 引用文献，用 `[[n_…]]` 指向节点。

Agent 只能通过标识符添加文献，不能自己填写题录。它写入的摘录必须能在正文中找到。图谱中没有依据的论断、假设和观测，标签前会显示 `◌`。

相关环境变量：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `WORKBENCH_LIBRARY_FILE_MB` | 50 | 单个文件的大小上限 |
| `WORKBENCH_LIBRARY_PROJECT_MB` | 1024 | 一个项目所有文件的总量上限 |
| `WORKBENCH_LIBRARY_FETCH_HOSTS` | doi.org、Crossref、DataCite、arXiv、OpenAlex、Open Library | 允许访问的元数据服务，逗号分隔 |
| `WORKBENCH_LIBRARY_FETCH_ANY` | 本地为 `1`，云端为 `0` | 是否允许抓取任意网页。内网地址始终拒绝 |

文献库功能新增了依赖 `pypdf`，升级后需要重新执行 `pip install -e '.[dev]'`。新的表由项目迁移 `010_library`、`011_groundings` 创建，工作台启动时会应用到所有已有项目（见「数据库迁移」）。

### 写作

模式切换里的「写作」（快捷键 `3`；`1` 图谱，`2` 文献）打开项目里的论文。一篇论文由若干节组成，每节用 Markdown 写：

- `[@引用键]`、`[@甲; @乙, 第 12 页]` 引用文献库里的文献。输入 `[@` 会弹出文献库的候选。
- `[这句话]{.claim node=n_…}` 把一句话锚定到图中的论断。输入 `[[`，或者选中句子后点「锚定论断」，按句子内容推荐节点。
- 右侧列出本节引用的文献和锚定的论断及其依据。「引用检查」报告文献库里找不到的引用键、指向不存在或待审核节点的锚点、没有依据的论断，以及引用的文献和论断依据对不上的地方。
- 正文停止输入 1.5 秒后自动保存。保存时带上开始编辑时的版本号；如果另一个页面或 Agent 已经改过这一节，页面会显示两边的差别，由你选用哪一版。每节保留历史版本，可以恢复。

Agent 不直接改论文。它通过 `suggest_edit`、`suggest_section`、`suggest_outline` 提出改写、新增一节或调整大纲，页面以差别的形式显示，由你逐条接受或拒绝。研报面板的「插入论文」把选中的研报段落变成待接受的新节：涉及论断的句子带上锚点，并引用这些论断已生效的文献依据。

导出：

| 格式 | 内容 | 需要 pandoc |
| --- | --- | --- |
| Word（DOCX） | 按所选 CSL 样式排好引用和参考文献，宋体正文、黑体标题、A4 版面 | 是 |
| LaTeX 工程（zip） | `main.tex`（ctexart/ctexrep + biblatex，GB/T 7714 用 `gb7714-2015` 样式）、`references.bib` 和编译说明，用 `latexmk -xelatex` 编译 | 是 |
| Markdown + BibTeX（zip） | 保留 `[@引用键]` 的 Pandoc Markdown 和 `.bib`、CSL-JSON | 否 |
| Markdown（引用已排版） | 引用换成编号或作者-年份，文末附参考文献 | 否（没有 pandoc 时用简化格式） |
| BibTeX | 本文引用的文献 | 否 |

引用样式有 GB/T 7714—2015 顺序编码制和著者-出版年制、APA、Chicago 著者-出版年制、IEEE，CSL 文件随代码分发（见 `ontology/export/csl/README.md`）。pandoc 总是带 `--sandbox` 运行，正文里的图片链接、文件路径不会被读取。

服务端从 `PATH` 中查找 pandoc，也可以用 `WORKBENCH_PANDOC` 指定路径。镜像（`Dockerfile`）安装的是 Debian 自带的 pandoc；本地开发请自行安装（如 `brew install pandoc`）。论文的表由项目迁移 `012_manuscripts` 创建。

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
| `WORKBENCH_AUTO_MIGRATE` | 启动时自动升级数据库表结构，默认开启；设为 `0` 时改由运营者手动执行迁移 |
| `WORKBENCH_DESKTOP_SESSION_DAYS` | 桌面端会话有效天数，1–90，默认 14 |
| `WORKBENCH_MODEL_ALLOWED_HOSTS` | 云端自定义模型可访问的 HTTPS 域名，以逗号分隔，默认 `api.anthropic.com` |

目前使用单个工作台实例。项目连接依赖 PostgreSQL 会话级 `search_path`。共享的 `onto_research` schema 保存注册表、会话和账号，每个项目使用自己的 `onto_research_p_*` schema。数据库登录角色需要访问注册表并能创建项目 schema。表结构的升级方式见下方「数据库迁移」。

先在 CloudBase 身份认证里启用账号密码登录并准备首个账号，再把该账号的用户 ID 加入允许名单。凭据由用户在登录页输入。工作台会话最长两小时，可通过退出登录立即撤销。

新项目由登录账号自动拥有。已有项目升级时保留为未归属，云端账号看不到。备份并停止旧版共享口令入口后，数据库运营者可按已核实的项目 ID 逐个绑定：

```sh
.venv/bin/python -m ontology.cli project-claim <project-id> <cloudbase-user-id>
```

本地单用户 Web 入口只读取未归属项目。日常开发请配置独立的本地开发库。CLI 和本地 stdio MCP 使用数据库运营凭据，仅供受信任的运营者操作。

私有数据启用后，回滚使用支持账号隔离的版本。旧版共享口令服务接入同一数据库时可读取项目，因此切流后应关闭旧版访问入口，并保持项目 schema 仅供后端访问。

### 自助注册

- 手机号注册需要上海地域、启用短信验证码和账号密码登录，以及已通过审核的短信签名和模板。新用户验证中国大陆手机号并设置密码，此后可用手机号和密码登录。已有短信账号可在「设置或重设密码」验证原手机号，继续使用原工作区。应用对发送尝试设有同号码 60 秒冷却、滚动 24 小时 5 次，以及全站每分钟 10 次、滚动 24 小时 100 次上限。
- 微信需要配置 CloudBase 的微信开放平台网站应用身份源并启用自动注册。将工作台 HTTPS 域名加入 CloudBase 安全域名及微信授权回调域名，应用回调路径固定为 `/auth/wechat/callback`。AppSecret 在 CloudBase 控制台配置。
- 两种方式以 CloudBase 返回的 UID 进入工作区。同一 UID 复用既有项目；尚未关联到原账号的手机号或微信会进入自己的工作区。工作台的注册记录仅保存 UID、首次注册渠道和时间。

按渠道配置后做一次真实注册、退出和重新登录验收，再对用户开放。关闭短信渠道会收起短信注册和密码设置入口，已有密码的账号仍可登录。需要封禁账号时，先在 CloudBase 停用用户，再移除工作台的注册记录或邀请名单条目以撤销访问。

### Agent 模型设置

从工作台顶部打开「平台设置」。自定义接口须支持 Anthropic Messages，填写 Base URL、模型 ID、鉴权方式和 API Key，保存后用「测试已保存连接」发起一次短模型请求。保存时留空 API Key 可保留同一连接的旧凭据，修改 URL 或鉴权方式时需重新输入。已有对话中显式选过的模型 ID 优先于平台默认模型。

云端按登录账号保存配置，API Key 加密入库。部署者仅应把可信的公网模型域名加入允许名单，云端发起请求时禁用 HTTP 跳转。发布新版本时保留加密密钥，否则现有凭据需重新录入。

本机运行工作台时可选择 Magpie 或 CC Switch。Magpie 读取其本地设置里的端口，默认 3425。CC Switch 默认 15721，开启 Claude Code 路由后可读取其写入 Claude 设置的本机地址。工作台使用路由暴露的 Messages 接口和可用模型目录，本机配置以仅用户可读写的权限保存在 `~/.config/onto-research/model.json`（或 `$XDG_CONFIG_HOME` 下的对应位置）。CloudBase 运行在云端，无法直接访问电脑的回环端口。

本地配置、密钥、测试数据和虚拟环境都应排除在上传目录外。上传到云托管的源代码目录仅需 `Dockerfile`、`.dockerignore`、`pyproject.toml`、`ontology/` 和 `workbench/`。容器进程以非 root 用户运行。

## 数据库迁移

表结构变更以编号 SQL 文件提交，每个文件在一个事务中执行，执行记录写入对应 schema 的 `schema_migrations` 表：

- 项目表：`ontology/migrations/NNN_说明.sql`，应用到每个项目的 schema。
- 共享表（注册表、会话、账号、模型设置）：`ontology/schema/registry/NNN_说明.sql`，应用到 `onto_research`。

工作台启动时，会先升级共享表，再升级所有项目（包括已归档和属于其他账号的项目），然后才开始服务。多个实例同时启动时由数据库锁排队，每个迁移只执行一次。某个项目迁移失败时，该项目保持原状，其他项目照常升级，但工作台不会启动，日志会写明失败的项目。在 CloudBase 上这表现为新版本健康检查失败，旧版本继续提供服务。

开发流程：在本地数据库编写并测试迁移，提交后部署到 CloudBase，新版本启动时会自动完成升级，无需手动连接云端数据库。迁移文件发布后不要修改，后续变更请新增文件。

设置 `WORKBENCH_AUTO_MIGRATE=0` 可关闭自动升级，改由运营者在能访问数据库的环境中手动执行：

```sh
.venv/bin/python -m ontology.cli migrate
```

自动升级只修改表结构，不会把 `types.yaml` 中的种子类型重新同步到已有项目，以免覆盖项目中已确认或导入的类型。如果需要更新已有项目的类型，请对每个项目执行 `--project <id> init`。

## 桌面端

`desktop/` 是 Tauri 2 桌面应用，复用本仓库的工作台页面，连接 CloudBase 上的工作台或本机工作台。启动后依次是登录层、项目总览层和项目工作层。会话令牌保存在系统钥匙串中，网页层无法读取。开发、架构和发布说明见 [desktop/README.md](desktop/README.md)。

服务端对桌面端请求使用 Bearer 会话（请求头 `X-Onto-Client: desktop/<版本>`），默认有效 14 天，可通过 `WORKBENCH_DESKTOP_SESSION_DAYS`（1–90）调整。会话保存在服务端，可随时撤销。

## 测试

CI 使用临时的 PostgreSQL 16。测试会删除并重建测试库的 schema。运行测试前将 `TEST_DATABASE_URL` 指向本机可销毁的 `onto_research_test`，再执行 `.venv/bin/python -m pytest -q`。
