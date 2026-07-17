# MoviePilot-Plugins

MoviePilot 社区插件仓库，收录社区开发的各种插件。

本仓库包含 [chaomarks](https://github.com/chaomarks) 维护的插件，默认从 `package.v2.json` 索引加载。

## 插件列表

| 插件 | 说明 | 版本 |
|------|------|------|
| [ClSearch](./plugins.v2/clsearch) | 观影磁力搜 — 搜索影视磁力资源，直接添加到115网盘离线下载 | v1.4.0 |

## ClSearch 观影磁力搜

搜索影视磁力资源，选择版本后直接添加到 115 网盘离线下载。

### 功能特性

- 🔍 **磁力搜索** — 多关键词搜索，返回资源标题、大小、做种数等
- 📄 **资源详情** — 查看磁力链接、种子下载、文件列表
- ⬇️ **115 离线下载** — 一键添加到 115 网盘离线任务
- 🔐 **自动登录** — 支持 Cookie 认证和账号密码自动登录
- 🛡️ **PoW 验证** — 自动处理站点安全验证
- 🧠 **智能体工具** — 支持 MoviePilot AI 智能体调用（搜索/详情/离线下载）

### 使用方法

1. 在 MoviePilot 插件市场安装 **观影磁力搜** 插件
2. 配置资源站点地址和认证信息（Cookie 或账号密码）
3. 配置 115 网盘 Cookie 和目标目录 CID
4. 保存配置后，通过 `/clsearch 关键词` 搜索资源
5. 选择资源后自动添加到 115 离线下载

### 配置说明

| 配置项 | 说明 |
|--------|------|
| 站点地址 | 磁力资源站点 URL |
| 用户名/密码 | 站点登录凭证（可选，与 Cookie 二选一） |
| 115 Cookie | 115 网盘认证 Cookie |
| 目录 CID | 115 网盘离线下载目标目录的 CID |

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