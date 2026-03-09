# WPS API 中文文档

本文档是当前 `wps-api` 项目的中文说明，内容以当前代码实现为准。

英文主文档见 [README.md](/Users/quant/Documents/wps-api/README.md)。

## 项目定位

`wps-api` 是一个基于以下组件构建的无头 PDF 转换服务：

- `WPS Office for Linux`
- `pywpsrpc`
- `FastAPI`

项目目标很明确，只做一类事情：

1. 接收 Office 文档上传
2. 调用 WPS 打开文档
3. 导出 PDF
4. 返回单个 PDF 或批量 ZIP

当前它不是通用任务平台，也不是完整文档处理系统，而是一个专注于
Office 转 PDF 的服务。

## 当前架构

### 单容器本地 worker 池

当前代码已经收敛为单容器单服务模型，不再使用旧的多容器 HTTP 分发方案。

运行时结构如下：

- 1 个 FastAPI 进程对外提供 HTTP API
- 1 个本地 `spreadsheet` warm worker
- 1 个本地 `presentation` warm worker
- 多个本地 `writer` warm worker

这些 worker 都在同一个容器内部，通过本地进程间通信协作，不走容器间
HTTP。

### 为什么这样设计

这个项目的核心瓶颈不是纯 Python 代码，而是：

- Qt 初始化
- `pywpsrpc` 建立 RPC 连接
- 获取 WPS Application
- WPS 打开文档并导出 PDF

如果每个请求都完整冷启动一次，上述开销会非常明显。当前架构的主要收益
在于：

- 常驻本地 WPS Application
- 通过 warm worker 复用会话
- `docx` 流量通过多个本地 `writer` worker 提高吞吐
- `xls` / `ppt` 保持单 worker，避免过度并发和资源浪费

## 功能范围

当前支持：

- `doc` / `docx` 转 PDF
- `ppt` / `pptx` 转 PDF
- `xls` / `xlsx` 转 PDF
- 单文件转换
- 多文件批量转换
- 启动时预热本地 WPS 会话
- 健康检查和运行环境检查

当前不提供：

- 鉴权
- 持久化任务队列
- 分布式重试中心
- 部分成功的 batch 返回语义

## API 说明

API 前缀固定为 `/api/v1`。

### `GET /api/v1/healthz`

进程存活检查，只表示 API 进程在运行。

示例响应：

```json
{"ok": true}
```

### `GET /api/v1/readyz`

运行环境检查。当前会检查：

- `jobs` 目录可写
- `runtime` 目录可写
- `DISPLAY` 已配置
- `XDG_RUNTIME_DIR` 已配置
- `pywpsrpc` 可导入

示例响应：

```json
{
  "ok": true,
  "checks": {
    "jobsDirWritable": true,
    "runtimeDirWritable": true,
    "displayConfigured": true,
    "xdgRuntimeDirConfigured": true,
    "pywpsrpcInstalled": true
  }
}
```

### `POST /api/v1/convert-to-pdf`

上传一个文件，字段名必须是 `file`，返回 PDF 文件流。

示例：

```bash
curl -X POST \
  -F "file=@./example.docx" \
  http://127.0.0.1:18000/api/v1/convert-to-pdf \
  --output output.pdf
```

### `POST /api/v1/convert-to-pdf/batch`

上传多个文件，字段名重复使用 `files`，返回 ZIP 文件流。

ZIP 内通常包含：

- 每个输入文件对应的 PDF
- 一个 `manifest.json`

示例：

```bash
curl -X POST \
  -F "files=@./a.docx" \
  -F "files=@./b.pptx" \
  -F "files=@./c.xlsx" \
  http://127.0.0.1:18000/api/v1/convert-to-pdf/batch \
  --output outputs.zip
```

### 文档页面

Swagger UI 路径是 `/docs`。

## 请求处理流程

单个转换请求的大致流程如下：

1. FastAPI 接收 multipart 上传
2. 文件落盘到 `workspace/jobs/<job_id>/`
3. `ConversionService` 根据扩展名判断文档族
4. `WarmSessionManager` 把任务分发给对应 family 的本地 worker
5. worker 复用或启动本地 WPS 会话
6. WPS 打开文档并导出 PDF
7. API 返回 PDF 或 ZIP
8. 后台任务清理临时文件

## worker 规则

### Writer worker 数量

`WPS_WORKER_COUNT` 控制本地 `writer` worker 数量。

#### 自动模式

当 `WPS_WORKER_COUNT` 留空或者设置为 `auto` 时，代码会优先检测宿主机
物理 CPU 核数，并按下面规则计算：

- 小于 `8` 核：直接取核数
- `8..16` 核：取 `核数 - 2`
- 大于 `16` 核：固定取 `16`

#### 手动模式

如果手动传入数字，当前实现会把值夹紧到 `1..32`。

这意味着当前代码的真实语义是：

- 自动模式按上面的公式收敛
- 手动模式仍然允许显式配置高于 `16` 的值，只要不超过 `32`

如果后续希望“全局最多 16”，那需要把手动上限也同步改成 `16`。

### 各 family 的并发模型

- `writer`
  - 多个本地 worker
  - 主要承载 `doc` / `docx`
  - 并发来自多个独立本地进程
- `spreadsheet`
  - 固定 1 个本地 worker
  - 主要承载 `xls` / `xlsx`
- `presentation`
  - 固定 1 个本地 worker
  - 主要承载 `ppt` / `pptx`

单个 worker 内仍然是串行处理，避免同一个 WPS Application 内并发自动化带来
不稳定性。

## 预热与会话复用

启动阶段如果 `WPS_WARM_SESSION_PREWARM_ENABLED=true`，服务会：

1. 创建 warm session manager
2. 依次预热 `writer`
3. 预热 `spreadsheet`
4. 预热 `presentation`
5. 预热完成后再进入稳定对外服务状态

会话复用的核心目的：

- 避免每个请求都重复初始化 Qt
- 避免每个请求都重新建立 RPC
- 避免每个请求都重新获取 WPS Application

同时，代码还支持两种回收机制：

- 空闲超时回收
- 单会话处理达到阈值后主动回收

## 配置项

### 运行时配置

- `WPS_WORKSPACE_ROOT`
  - 默认值：`/workspace`
  - 工作目录根路径
- `WPS_CONVERSION_TIMEOUT_SECONDS`
  - 默认值：`120`
  - 单次转换超时时间
- `WPS_CLEANUP_MAX_AGE_SECONDS`
  - 默认值：`86400`
  - 启动时清理历史任务目录的阈值
- `WPS_MAX_UPLOAD_SIZE_BYTES`
  - 默认值：`52428800`
  - 单文件上传大小上限
- `WPS_BATCH_MAX_FILES`
  - 默认值：`12`
  - batch 接口允许的最大文件数
- `WPS_WORKER_COUNT`
  - 默认值：`auto`
  - 本地 `writer` worker 数量
- `WPS_WARM_SESSION_IDLE_TTL_SECONDS`
  - 默认值：`600`
  - 会话空闲回收阈值
- `WPS_WARM_SESSION_MAX_JOBS`
  - 默认值：`100`
  - 单会话累计处理文件数达到阈值后回收
- `WPS_WARM_SESSION_PREWARM_ENABLED`
  - 默认值：`true`
  - 是否在启动时预热本地会话

### 启动脚本相关配置

- `WPS_IMAGE`
  - 默认值：`quantatrisk/wps-api:latest`
  - `scripts/compose_up.sh` 使用的镜像
- `WPS_API_PORT`
  - 默认值：`18000`
  - 主机映射端口

## 构建与启动

### 推荐启动方式

标准入口是：

```bash
./scripts/build_image.sh
./scripts/compose_up.sh
```

不建议直接执行 `docker compose up`。当前仓库约定由
`scripts/compose_up.sh` 负责：

- 解析 worker 数量
- 检查镜像是否存在
- 再统一启动服务

### 构建镜像

交互式方式：

```bash
./scripts/build_image.sh
```

非交互方式：

```bash
docker build -f docker/Dockerfile -t quantatrisk/wps-api:local .
```

可选构建参数：

```bash
docker build \
  -f docker/Dockerfile \
  --build-arg WPS_DEB_URL_BASE=https://your-mirror.example.com/wps-office.deb \
  --build-arg FONTS_ZIP_URL=https://your-cdn.example.com/Fonts.zip \
  -t quantatrisk/wps-api:local .
```

### Compose 启动

```bash
./scripts/compose_up.sh
```

指定参数示例：

```bash
WPS_IMAGE=quantatrisk/wps-api:local \
WPS_API_PORT=18000 \
WPS_WORKER_COUNT=auto \
./scripts/compose_up.sh
```

停止服务：

```bash
docker compose -f docker/docker-compose.yml down --remove-orphans
```

### 本地直接运行

如果宿主机已经具备完整 Linux WPS 运行环境，可以直接运行：

```bash
./scripts/run_local_api.sh
```

这个脚本会设置：

- `WPS_WORKSPACE_ROOT`
- `DISPLAY`
- `QT_QPA_PLATFORM`
- `XDG_RUNTIME_DIR`

## 远程部署约定

当前仓库不再保留单独的远程拉取部署脚本。推荐流程是：

```bash
git clone https://github.com/Quantatirsk/wps-api.git
cd wps-api
./scripts/build_image.sh
./scripts/compose_up.sh
```

也就是说，目标机器需要具备：

- Docker
- 仓库工作树
- 已构建的镜像

如果镜像不存在，`compose_up.sh` 会直接失败。

## 运行边界

- batch 接口不是部分成功语义
- 单个失败会导致整个 batch 失败
- 当前没有认证
- 当前没有任务持久化队列
- 当前没有统一重试调度中心

## 代码结构

```text
.
├── app/
│   ├── adapters/        # 各文档族对应的 WPS 自动化适配层
│   ├── api/             # FastAPI 路由
│   ├── runtime/         # warm worker 池和本地 worker 进程管理
│   ├── services/        # 转换编排
│   └── utils/           # 文件、日志、CPU 检测、错误定义
├── docker/
│   ├── conf/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── entrypoint.sh
├── docs/
│   └── README.zh-CN.md
├── scripts/
├── tests/
└── README.md
```

## 当前优化重点

从业务角度看，当前项目的重点优化方向是：

- `docx -> pdf` 批量吞吐
- 稳定复用 `writer` Application
- 保持 worker 模型简单
- 尽量 fast-fail，不引入过多可选分支

如果后续继续演进，最值得关注的仍然是：

- `writer` worker 数量选择
- 会话复用和回收策略
- batch 请求下的真实吞吐与稳定性
