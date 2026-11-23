# 内网启动说明

## 前置条件

- 本机 LLM Wiki 桌面端已开启 Settings -> API + MCP -> 本地 HTTP API。
- 防火墙允许同网段访问本机前端端口，例如 `5173` 或 `8080`。后端默认使用本机 `8091`，通常只由前端代理访问。
- 其他机器访问时使用服务端电脑的局域网 IP，例如 `http://192.168.1.20:5173`。

## 后端启动

交付包不包含 `.venv`。首次部署时，请先安装 Python，并在 `backend` 目录创建虚拟环境、安装依赖（需要可访问的依赖源）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖安装完成并按下文创建管理员账户后，在 `backend` 目录启动后端：

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
$env:DJANGO_ALLOWED_HOSTS = "*"
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8091 --noreload
```

## 权限与账户

所有使用者必须登录；未登录时只显示登录页面，业务接口返回 401。普通用户登录后可以使用：检索问答、关系图谱、查看文档内容和领域知识层。
只有管理员才能使用：知识摄入、登记本地原文项目、只读 Markdown 导出和移除项目登记。

首次部署后，需要创建一个管理员账户（用户名、密码自定义）：

```powershell
cd backend
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
```

创建时 `is_staff` 和 `is_superuser` 都会被设为 true，登录前端即可看到知识摄入管理区。普通账户（仅能登录、不是管理员）可通过 Django admin 或 shell 创建：

```powershell
.\.venv\Scripts\python.exe manage.py shell -c "from django.contrib.auth.models import User; User.objects.create_user('alice', password='alice-pass-123')"
```

普通账户不显示知识摄入管理区，访问管理接口返回 403；匿名请求业务接口返回 401。MCP 接口继续使用独立 Bearer 令牌认证。

## 前端启动

```powershell
cd frontend
npm run dev -- --host 0.0.0.0
```

同网段其他机器打开：

```text
http://服务端电脑局域网IP:5173
```

## 说明

- 前端 dev server 会把 `/api` 代理到服务端本机 `http://127.0.0.1:8091`，所以其他机器只需要访问前端端口。
- 后端仍然连接服务端电脑上的 LLM Wiki API，默认是 `http://127.0.0.1:19828`。
- `backend/.env` 可能包含 LLM key 和 LLM Wiki token，默认不进入交付包；首次部署后按 `.env.example` 自行填写。
