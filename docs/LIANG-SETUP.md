# Liang page 本地使用

本地目录：`D:\sport_data`。主页只有新版仪表盘：`/` 是首页，`/summary` 是汇总，轨迹墙在首页导航中。旧 `/classic`、`/dashboard` 和它们的 summary 地址会保留查询条件和活动 ID，跳转到新版。标题、头像及 GitHub 链接配置在 `src/static/site-metadata.ts`；跑步目标在 `src/components/Dashboard/config.ts`。

原作者的数据库、活动 JSON 和轨迹时间线已移动到 `.local-backup/original-data-*`，不会参与新站展示或同步。首次初始化为空，完成授权与同步后展示个人活动。备份与 `.env` 均已加入 Git 忽略规则。

## 1. 创建你的 Strava 应用

用自己的 Strava 账号打开 [My API Application](https://www.strava.com/settings/api)，创建应用：

- Application Name：`Liang page`。
- Website：`http://localhost:5173`（本地开发使用）。
- Authorization Callback Domain：`localhost`，只填域名。
- 其他资料按页面要求填写。

记录页面给出的 **Client ID** 和 **Client Secret**。按当前 [Strava 官方入门文档](https://developers.strava.com/docs/getting-started/)，创建新应用需要 Strava 订阅，且初始仅允许创建者自己的账号授权；以你的 API 设置页实际提示为准。

## 2. 在本机保存应用凭据

`.env` 已创建，用编辑器打开，填这两项：

```dotenv
STRAVA_CLIENT_ID=你的数字ID
STRAVA_CLIENT_SECRET=你的应用密钥
```

先保留 `STRAVA_CLIENT_REFRESH_TOKEN` 和 `STRAVA_ATHLETE_ID` 为空。不要把密钥发到聊天里，也不要放进前端 `src/` 或 `VITE_` 环境变量。`.env` 不提交 Git。

在 PowerShell 中运行：

```powershell
cd D:\sport_data
.\.venv\Scripts\python.exe run_page\strava_auth.py
```

脚本会打开 Strava 授权页。确认当前是你自己的账号，勾选活动读取权限，点击授权。本地回调服务使用 `127.0.0.1:8088`，会自动校验授权结果并将 refresh token、athlete ID 保存到 `.env`，无需手工复制 code 或 token。端口被占用时加 `--port 8089`。

授权范围为 `read,activity:read_all`，包含仅自己可见的活动，不请求活动写入权限。同步导出的活动和路线会出现在站点中，发布站点前自行选择哪些记录适合公开。权限含义见 [Strava OAuth 文档](https://developers.strava.com/docs/authentication/)。

## 3. 优先同步近期数据

```powershell
cd D:\sport_data
powershell -ExecutionPolicy Bypass -File .\sync-strava-local.ps1
```

首次默认获取最近 **90 天**；以后按最新 Strava 记录回溯 7 天增量更新，且限制在 90 天窗口内。该窗口可在 `.env` 的 `STRAVA_RECENT_DAYS` 或命令行 `--days` 修改。活动逐条保存，API 限流时保留已同步的近期记录；基础活动数据会先写入站点，再补个人最佳、时间线与天气。

首次想快速看到基本记录，可跳过额外回填：

```powershell
powershell -ExecutionPolicy Bypass -File .\sync-strava-local.ps1 --skip-details --timeline-limit 0 --skip-temperature-backfill
```

之后再运行普通同步补齐详情。历史数据建议分批补，例如获取 2026-07-01 之前的 90 天：

```powershell
powershell -ExecutionPolicy Bypass -File .\sync-strava-local.ps1 --before 2026-07-01 --days 90
```

下一批以更早的日期为边界。`--force` 获取全部历史，可能触及 API 配额。限流后等待配额恢复再执行；401/403 时检查授权并重新运行授权脚本。同步不会向 Strava 上传或修改活动。日期过滤参数见 [活动 API](https://developers.strava.com/docs/reference/#api-Activities-getLoggedInAthleteActivities)。

刷新 token 时会更新本地 `.env`，并检查配置的 athlete ID，避免把另一个账号的数据合并进来。请勿直接替换为别人的 refresh token。

## 4. 启动网站

```powershell
cd D:\sport_data
powershell -ExecutionPolicy Bypass -File .\start-local.ps1
```

打开终端显示的 `http://127.0.0.1:5173`。本机已准备好 Python 虚拟环境、前端依赖和项目内 Node.js 运行时。同步后开发服务器会加载新的活动文件；已构建的 `dist/` 需要重新构建。

有系统 Node.js / pnpm 的机器可使用标准命令：

```powershell
pnpm install --frozen-lockfile
pnpm dev
pnpm build
```

首次在其他机器准备 Python：`python -m venv .venv`，再运行 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`。

## 网络与后续部署

如果本机需要代理，在 `.env` 填 `HTTPS_PROXY=http://127.0.0.1:你的端口`。端口由你使用的代理软件决定；不要关闭 TLS 证书校验。

Vercel 使用仓库中的 `vercel.json` 配置，以 Vite 构建，发布 `dist/`，支持主页和 `/summary` 等页面直接访问。部署不需要 Strava 密钥；它们只用于本地同步或 GitHub Actions。`.vercelignore` 排除了本地凭据、数据库和备份。

## 连续补历史与周期 push

```powershell
cd D:\sport_data
$env:GIT_EXECUTABLE = 'C:\Program Files\Git\cmd\git.exe'
.\.venv\Scripts\python.exe run_page\strava_backfill_local.py --batch-size 100
```

本地先检查新活动，再按从新到旧的顺序连续补历史。每批最多 100 条，保存后 commit 并 push；收到 Strava HTTP 429 后停止，不会绕过限流。全部历史抓完也会停止。再次运行时从断点续传。批量补历史优先保存活动与 GPS 概览，地点细分、详细流和天气留待后续补齐；已缓存的精细轨迹不会降级。

进度保存在 `.strava-sync/state.json`。其中轮换后的 refresh token 使用 AES-GCM 加密，仅持有 `STRAVA_CLIENT_SECRET` 才能解密，没有明文凭据；该目录不会上传到 Vercel。实际活动仍保存在 `src/static/activities.json`，云端会据此重建数据库，避免依赖本地 SQLite。

GitHub Actions 工作流已准备为每 6 小时运行一次（北京时间 02:17、08:17、14:17、20:17，实际启动可能排队）。只有仓库变量 `ENABLE_STRAVA_SYNC=true` 时才运行。本地连续抓取期间保持它关闭，避免两边同时 push；以后可单独开启，云端每次补 50 条历史并保存进度。GitHub Secrets 需要 Client ID、Client Secret、初始 Refresh Token 和 Athlete ID。

## 省份足迹

每次导出活动时自动补齐 `province`：优先保留已有地点文字中的省份，否则使用活动起点 GPS 与项目内的省界数据离线匹配，不调用 Strava 或第三方地理接口。省份图、个人概览和按省份筛选共用该字段。无 GPS 且无有效地点的记录不猜测省份；跨省活动按起点统计。省份总数不包含无名称的海上辅助边界。

push 当前只更新 GitHub；Vercel 的 GitHub 自动部署关联尚未完成，需要另行关联或部署。地图沿用项目的 Mapbox 公共令牌，可在 `src/utils/const.ts` 换成自己的令牌。原项目和 Dashboard 的 MIT 许可保持保留。
