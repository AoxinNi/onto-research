# ONTO Research 桌面端

Tauri 2 桌面应用。复用 `workbench/static` 的页面，连接部署在 CloudBase 云托管的工作台，也可以连接本机工作台。

| 层 | 页面 | 内容 |
| --- | --- | --- |
| 1 登录层 | `index.html`（来自 `login.html`） | 检查已保存的会话、选择服务器、账号密码登录、手机号注册或重设密码、微信扫码 |
| 2 项目总览层 | `home.html` | 项目列表、创建与导入、导出、数据源、平台设置（模型连接） |
| 3 项目工作层 | `workspace.html?project=<id>` | 三维本体图、Agent 对话、变更审核、研报、时间线 |

## Tauri 应用的一般开发流程

1. **准备环境**：Rust 稳定版、Node 20 以上、各平台的 WebView 依赖。macOS 需要 Xcode Command Line Tools；Windows 需要 WebView2（Windows 11 已自带）；Linux 需要 `libwebkit2gtk-4.1-dev` 等系统库。
2. **创建项目**：通常用 `npm create tauri-app`。本项目手动搭建，以便直接复用现有前端。目录分两部分：前端（HTML/JS，可以用任意框架）和 `src-tauri/`（Rust 后端）。
3. **定义前后端边界**：网络请求、凭据、文件和系统对话框放在 Rust 命令（`#[tauri::command]`）中，前端用 `invoke` 调用。Tauri 2 默认拒绝所有权限，每个窗口能调用的命令要在 `capabilities/*.json` 中逐项授权。
4. **开发调试**：`npm run dev`（即 `tauri dev`）启动原生窗口，修改 Rust 代码后会自动重新编译。调试构建可以右键打开 WebView 开发者工具。
5. **安全加固**：设置严格的 CSP，限制主窗口只能导航到应用自带页面，外部链接交给系统浏览器打开，远程页面不授予 IPC 权限。
6. **测试**：Rust 单元测试和集成测试（`cargo test`）、前端测试、Clippy 与 rustfmt，在 CI 中同时运行。
7. **图标与元数据**：用 `tauri icon` 从一张源图生成各平台图标；在 `tauri.conf.json` 中设置标识符、版本、类别和版权信息。
8. **构建与签名**：`tauri build` 生成 `.app`/`.dmg`、`.msi`/`.exe`、`.deb`/`.AppImage`/`.rpm`。macOS 需要 Developer ID 签名和公证；Windows 需要代码签名证书，否则会触发 SmartScreen 警告。
9. **发布与更新**：用 CI（`tauri-action`）在各平台构建，发布到 GitHub Releases 或自己的下载服务器。自动更新使用 `tauri-plugin-updater` 和更新签名密钥。
10. **上线后运维**：收集日志（本应用写入系统日志目录）、跟踪服务端 API 版本兼容性，以及按平台处理权限和杀毒软件误报。

## 架构

```
WebView（workbench/static 页面）
  └─ net.js：request() / socket() / download() / confirmAction()
       浏览器里：fetch、WebSocket、location
       桌面端里：invoke → Rust
Rust（src-tauri/src）
  ├─ backend.rs   唯一访问服务器的通道：X-Onto-Client 头、Bearer 会话、路径白名单、不跟随跳转、大小与超时限制
  ├─ credentials.rs 会话令牌存入系统钥匙串（macOS Keychain / Windows 凭据管理器 / Linux Secret Service），按服务器分开保存
  ├─ socket.rs    在 Rust 中建立工作台 WebSocket，通过 Channel 转发给页面
  ├─ wechat.rs    在独立原生窗口中显示微信扫码页，拦截回调地址，用启动扫码时拿到的 challenge 完成登录
  ├─ config.rs    服务器地址（HTTPS，本机地址可用 HTTP），保存在应用配置目录
  └─ lib.rs       命令、主窗口、导航限制、单实例、窗口状态、日志
```

服务器端（`workbench/cloud.py`、`workbench/auth.py`）对带有 `X-Onto-Client: desktop/<版本>` 的请求：

- 从 `Authorization: Bearer` 读取会话，忽略 Cookie，因此不需要 Origin 检查。浏览器跨域发送自定义头时必须先通过 CORS 预检，而工作台从不放行预检，所以网页无法伪造这类请求。
- 登录、短信注册和微信登录在响应体中返回 `{token, expires_in}`；短信与微信的 challenge 也通过响应体传递，不写入 Cookie。
- 桌面会话默认有效 14 天，可用 `WORKBENCH_DESKTOP_SESSION_DAYS` 设置为 1–90 天。会话保存在服务端，退出登录、在注册记录或邀请名单中移除账号、以及短信重设密码都会立即使它失效。
- `/auth/options` 返回 `mode`（`cloud` 或 `local`）和 `api` 版本号，桌面端据此判断是否需要登录以及版本是否兼容。

页面中的凭据：网页层无法读取令牌、短信 challenge 或微信 challenge。所有这些都只保存在 Rust 进程和系统钥匙串中。

## 开发

```sh
cd desktop
npm ci
npm run dev            # 组装 dist/ 并打开原生窗口
```

默认连接 `http://127.0.0.1:8766`。先在仓库根目录启动本机工作台（`./start-workbench.sh`）。本机模式不需要登录，应用启动后直接进入项目总览。要连接云端，在登录页底部「服务器」中填写 HTTPS 地址，或在构建时设置 `ONTO_DEFAULT_SERVER`。

修改 `workbench/static` 下的页面后，重新运行 `npm run dev` 即可（`npm run assemble` 会把页面复制到 `dist/`）。网页版和桌面端共用同一份代码。新增的网络调用请使用 `net.js`，不要直接调用 `fetch` 或 `WebSocket`。新增的 Rust 命令要同时登记在 `build.rs` 和 `capabilities/main.json` 中。

测试：

```sh
npm test                              # net.js 桌面模式
cd src-tauri && cargo test            # 地址校验、路径白名单、令牌与 challenge 处理（含模拟 HTTP 服务器）
cargo clippy --all-targets -- -D warnings && cargo fmt --check
```

服务端测试在仓库根目录运行 `pytest`（`tests/test_desktop_client.py` 覆盖桌面模式）。

## 发布前检查

1. **应用标识符**：`tauri.conf.json` 中的 `identifier`（目前为 `com.ontoresearch.desktop`）应改为你拥有的域名的反写，并在首次发布前确定。发布后修改会让系统把它当作另一个应用，已保存的登录状态也会丢失。
2. **默认服务器**：在仓库变量 `ONTO_DEFAULT_SERVER` 中填写生产工作台的 HTTPS 来源。
3. **CloudBase 服务端**：先部署本次服务端改动，再分发应用。旧版服务端会返回「服务器版本过旧」。如果开放微信登录，回调路径不变（`/auth/wechat/callback`），无需额外配置域名。
4. **macOS 签名与公证**：按 `.github/workflows/desktop-release.yml` 顶部注释配置 Apple 证书和公证凭据。未签名的应用会被 Gatekeeper 阻止，且钥匙串在每次重新构建后都会再次请求授权。
5. **Windows 签名**：取得代码签名证书后，在 `bundle.windows` 中配置 `certificateThumbprint` 或 `signCommand`。
6. **自动更新**（尚未启用）：运行 `npx tauri signer generate` 生成更新签名密钥，私钥保存为 CI secret（不要提交到仓库）；然后加入 `tauri-plugin-updater`，在 `tauri.conf.json` 中填写公钥和更新地址（`latest.json`）。
7. **打标签发布**：`git tag desktop-v0.1.0 && git push --tags`，CI 会创建草稿 Release，检查后再发布。

日志位置：macOS `~/Library/Logs/com.ontoresearch.desktop/`，Windows `%LOCALAPPDATA%\com.ontoresearch.desktop\logs\`，Linux `~/.local/share/com.ontoresearch.desktop/logs/`。日志只记录请求方法、路径和状态码，不包含令牌或请求内容。
