# pys — App Hub 的 Python 应用代码

本仓存放 App Hub 平台上全部 Python 应用（web 项目）的代码，一项目一个文件夹，与盒上代码树 `/srv/apphub/code/python` 一一对应。同步走面板控制台「3 GitHub」的预览后推送：项目在盒上提交/更新时自动落本地提交，推送前先看预览。

## 平台五仓

平台的代码与资料按性质分五个仓库（账号 adenosinex），放东西前先对号入座：

- **apphub**（私有）— 平台本体：面板代码与经验文档。https://github.com/adenosinex/apphub
- **pys**（本仓，公开）— Python 应用代码。
- **htmls**（公开）— HTML 应用代码，一项目一个文件夹。https://github.com/adenosinex/htmls
- **data**（私有）— 项目数据：白名单内有价值、后续可用的数据，单向镜像发布。https://github.com/adenosinex/data
- **materials**（公开）— 项目素材（语料），按项目分文件夹存放。https://github.com/adenosinex/materials

## 本仓禁入物

- 凭据类文件（令牌、cookie、密钥、账号文件）永不进仓。
- 项目数据与素材不进本仓：数据归 data 仓，素材归 materials 仓。
- 运行时缓存与备份文件（`.runtime/`、`*.bak-*`）不进仓，已由 `.gitignore` 排除。
