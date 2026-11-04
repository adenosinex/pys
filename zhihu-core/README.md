# 知乎提取最小核心

这是从原项目复制并独立维护的代码，整个 `zhihu_core` 文件夹可以单独复制、改名或提交为新仓库。原项目不会导入这个目录，这里的代码也不导入父项目。

核心解析、HTTP 抓取、命令行和本地 API 全部只依赖 **Python 3.10+ 标准库**。Linux 上云时安装 `requirements.txt`，仅直接依赖 Gunicorn 生产服务器及其传递依赖。建议云端使用 Python 3.11 或更新版本。

支持知乎专栏文章、单个回答和问题描述，输出 Markdown / TXT / JSON，保留标题、作者、日期、赞同、评论和图片链接。问题链接不会展开所有回答。图片仅保留链接，正文中的代码块会保留在纯文本中。

## 文件职责

| 文件 | 后续修改位置 |
| --- | --- |
| `extractor.py` | 公共调用入口、URL 白名单、HTTP 请求、Cookie 配置、大小限制、错误类型 |
| `parser.py` | 页面结构化数据 / DOM、HTML → Markdown、文档模型 |
| `app.py` | 最小 WSGI API、Token 校验、参数校验、并发限制 |
| `cli.py` | 命令行入口 |
| `server.py` | Windows / macOS / Linux 本地开发服务器 |
| `gunicorn.conf.py`、`Dockerfile`、`Procfile` | Linux 云部署 |
| `tests/test_core.py` | 无外网依赖的解析、HTTP、API 测试 |

## 先运行命令行：无需 pip 安装

在本目录打开 PowerShell，配置自己的登录 Cookie：

```powershell
$env:ZHIHU_COOKIE = 'd_c0=你的值; z_c0=你的值; ...'
python cli.py 'https://zhuanlan.zhihu.com/p/文章ID' --format markdown -o result.md
```

也可以使用 UTF-8 的单行 Cookie 请求头文件：

```powershell
python cli.py 'https://www.zhihu.com/question/问题ID/answer/回答ID' --cookie-file 'C:\private\zhihu-cookie.txt' --format text
```

Cookie 配置优先级：代码显式传入 / CLI `--cookie-file` → `ZHIHU_COOKIE` → `ZHIHU_COOKIE_FILE`。不配置时会明确报错，不会寻找原项目的 Cookie。核心不会保存、自动续期或输出 Cookie；需要更新登录态时由你替换配置。`.env.example` 仅是配置说明，不会自动加载 `.env`，因此不需要 python-dotenv。

## 直接在 Python 中调用

在本目录运行时：

```python
from extractor import extract, extract_html

doc = extract('https://zhuanlan.zhihu.com/p/123456')  # Cookie 来自环境变量
print(doc.markdown)
print(doc.text)
data = doc.to_dict()

# 已有网页 HTML 时可以仅解析，不请求网络、不需要 Cookie：
# doc = extract_html(url, html)
```

在父目录中作为包调用可用 `from zhihu_core import extract`。

## 本地 API：无需 pip 安装

```powershell
$env:EXTRACTOR_API_KEY = '换成你生成的足够长的随机密钥'
$env:ZHIHU_COOKIE = 'd_c0=你的值; z_c0=你的值; ...'
python server.py
```

默认监听 `127.0.0.1:8000`。`PORT`、`HOST` 可改变本地监听；公网生产使用下面的 Gunicorn / Docker 入口。本地服务器用于开发验证。

```powershell
$headers = @{ Authorization = "Bearer $env:EXTRACTOR_API_KEY" }
$body = @{ url = 'https://zhuanlan.zhihu.com/p/123456'; format = 'markdown' } | ConvertTo-Json
Invoke-RestMethod 'http://127.0.0.1:8000/api/extract' -Method Post -Headers $headers -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

| 接口 | 行为 |
| --- | --- |
| `GET /health`、`GET /api/health` | 进程健康状态，无需密钥，不请求知乎 |
| `POST /api/extract` | 需 `Authorization: Bearer <EXTRACTOR_API_KEY>` |

提取请求仅接受 `url`、`format`（markdown / text / json，默认 markdown）、`keep_images`（布尔值，默认 true）。成功返回 `ok`、文档元信息和 `content`；`format=json` 时 `content` 为文档 JSON 字符串。失败返回对应 HTTP 状态码及 `ok=false`、`code`、`error`。没有 Cookie 管理、服务节点管理或视频接口。

API 未设置密钥时提取接口返回 503；错误密钥返回 401。默认每个进程最多同时提取 2 个任务（`MAX_CONCURRENCY`），满载返回 429；请求体上限 16 KiB，页面下载及 gzip 解压后的大小上限分别为 2 MiB。抓取使用 20 秒 I/O 超时并检查读取预算；底层单次阻塞读取仍可能晚于预算返回。仅请求规范化的知乎 HTTPS 页面，重定向逐跳检查，登录页或其他站点不会继续抓取。

## Linux 云部署

将整个文件夹作为独立项目部署，或把平台项目根目录设为 `zhihu_core`。

```sh
python -m pip install -r requirements.txt
gunicorn --config gunicorn.conf.py app:app
```

在平台的私密环境变量中设置 `EXTRACTOR_API_KEY`、`ZHIHU_COOKIE`；平台提供的 `PORT` 会自动使用。也可将 Cookie 挂载为只读文件并设置 `ZHIHU_COOKIE_FILE`。服务不需要持久化磁盘或数据库。HTTPS 由平台或反向代理提供。

Docker 只使用当前目录作为构建上下文：

```sh
docker build -t zhihu-core .
docker run --rm -p 8000:8000 --env-file .env zhihu-core
```

`.dockerignore` 使用源码白名单，镜像不携带本地配置。容器使用非 root 用户，健康检查跟随 `PORT`。Gunicorn 默认 1 个进程、4 个线程；实际提取并发仍受 `MAX_CONCURRENCY` 限制，其他线程可响应健康检查。

这是单个运营者 Cookie + 单个访问密钥的精简服务。多用户账号、结果缓存、队列、界面和浏览器登录可在此基础上另加。源码具备基本访问控制，但不代表云出口已经通过知乎访问验证；Cookie 失效、验证码、权限不足或页面结构变化仍会明确失败。只有摘要的结构化文章不会当成全文成功返回；其他正文完整性仍受页面实际提供的内容限制。

## 独立验证

```sh
python -S -m unittest discover -s tests -v
```

`-S` 禁用第三方 site-packages，测试仅使用标准库与本目录文件。测试通过本机 HTTP 服务提供受控文章页面，覆盖真实 HTTP 读取、gzip 大小限制、重定向、正文解析、Token、参数和并发控制；不访问知乎，不需要真实 Cookie。

2026-10-29 验证：Python 3.10.1、3.11.0 各 19 项测试通过；复制到改名的中文 / 空格目录后，禁用第三方包仍可运行。独立 HTTP API 健康检查 200、错误密钥 401、未配置 Cookie 503，未读取父项目登录态。原项目 65 个源码与配置文件的 SHA-256 保持一致。当前 Windows 环境未实际运行 Linux Gunicorn / Docker，也未验证云出口的知乎抓取成功率。
