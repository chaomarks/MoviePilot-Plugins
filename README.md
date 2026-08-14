# MoviePilot-Plugins

MoviePilot 社区插件仓库，收录社区开发的各种插件。

本仓库包含 [chaomarks](https://github.com/chaomarks) 维护的插件，默认从 `package.v2.json` 索引加载。

## 插件列表

| 插件 | 说明 | 版本 |
|------|------|------|
| [ClSearch](./plugins.v2/clsearch) | 观影磁力搜 — 搜索影视磁力资源，直接添加到115网盘离线下载，支持离线完成后自动整理入库 | v1.5.8.8 |

## ClSearch 观影磁力搜

搜索影视磁力资源，选择版本后直接添加到 115 网盘离线下载。

### 功能特性

- **磁力搜索** — 多关键词搜索，返回资源标题、大小、做种数等
- **资源详情** — 查看磁力链接、种子下载、文件列表
- **115 离线下载** — 一键添加到 115 网盘离线任务，需登录 MoviePilot 内置 115 存储
- **后台轮询监控** — 30秒间隔自动检测离线任务完成状态
- **自动整理入库** — 离线完成后自动识别媒体、按MP标准重命名、清理小体积疑似广告、创建Season目录、移动到映射目录
- **智能重命名** — 优先复用 MoviePilot 文件管理的自动识别命名逻辑，使用 MediaChain + TransferChain 推荐命名
- **自动登录** — 支持 Cookie 认证和账号密码自动登录，登录后Cookie自动持久化
- **PoW 验证** — 自动处理站点安全验证
- **智能体工具** — 支持 MoviePilot AI 智能体调用（搜索/详情/离线下载/重命名/移动文件）
- **权限分离** — 智能体判断目录，插件执行移动，不依赖用户渠道权限

### 自动整理流程

离线下载完成后，插件自动执行：

1. 在115离线目录中找到下载完成的文件夹
2. 使用 `MediaChain` 识别媒体信息（标题/年份/类型/TMDB ID）
3. 调用 `_api_recursive_rename` 按 MoviePilot 标准命名重命名：
   - 递归遍历目录内所有媒体文件
   - 优先复用 MoviePilot 文件管理同款 `MediaChain` + `TransferChain.recommend_name` 生成标准文件名
   - 批量重命名文件
   - 创建 Season 子目录（电视剧）并移动文件
   - 重命名外层文件夹为 `标题 (年份) [tmdbid=xxx]`
   - 仅自动删除小体积疑似广告和非媒体杂项，无法识别的大文件保留交给智能体确认
4. 通知智能体：根据MP目录映射判断目标路径，调用 `cl_search_move_file` 工具移动文件夹到入库目录

### 使用方法

1. 在 MoviePilot 插件市场安装 **观影磁力搜** 插件
2. 配置资源站点地址和认证信息（Cookie 或账号密码）
3. 登录 MoviePilot 内置 115 存储，并配置离线下载目标目录 CID
4. 保存配置后，可通过以下两种方式搜索资源：
   - **命令方式**：通过 `/clsearch 关键词` 搜索资源
   - **智能体方式**：直接与 MoviePilot AI 智能体对话，智能体会自动调用插件的搜索、离线下载、重命名、移动文件等工具完成全流程
5. 选择资源后自动添加到 115 离线下载，后台自动监控完成状态

### 配置说明

| 配置项 | 说明 |
|--------|------|
| 站点地址 | 磁力资源站点 URL |
| 用户名/密码 | 站点登录凭证（可选，与 Cookie 二选一） |
| MP内置115存储 | 需在 MoviePilot 内置存储中登录 115，供重命名、移动和删除接口使用 |
| 115 Cookie | 用于115离线下载接口的认证 Cookie |
| 目录 CID | 115 网盘离线下载目标目录的 CID |
| 解析路径 | CID 对应的完整目录路径（保存后自动解析） |
| 完成后自动整理 | 开启后离线完成时自动触发重命名和整理入库 |

## 仓库结构

```text
MoviePilot-Plugins/
├── plugins.v2/
│   └── clsearch/          # 观影磁力搜插件
│       ├── __init__.py     # 插件主体代码
│       └── requirements.txt
├── icons/                  # 插件图标
├── package.v2.json         # V2 插件索引
├── .gitignore
└── README.md
```

## 参考

- [MoviePilot 官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins)
- [MoviePilot 主仓库](https://github.com/jxxghp/MoviePilot)
