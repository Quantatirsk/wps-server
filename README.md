# WPS API Service

这是一个基于 `WPS Office for Linux + pywpsrpc + FastAPI` 的无头 PDF 转换服务。

它的职责很单一：

- 接收 `doc` / `docx`
- 接收 `ppt` / `pptx`
- 接收 `xls` / `xlsx`
- 调用 WPS 导出 PDF
- 直接返回 PDF 或批量 ZIP

当前版本刻意保持 KISS：

- 只做 `convert to pdf`
- 不做任务队列
- 不做数据库
- 不做 Ghostscript 后处理
- 不改 `pywpsrpc` 上游源码

## 为什么现在不再做 PDF 后处理

这轮排查后的结论已经很明确：

- `docx -> pdf` 体积膨胀的根因，不是“Linux WPS 天然导出很大”
- 真正根因是部分字体文件的嵌入权限不对，导致正文文本被光栅化为图片
- 典型案例是旧版 `方正仿宋简体`，嵌入权限为 `0x2`
- 当替换为可嵌入版本后，PDF 体积和转换速度都会恢复正常

所以当前策略已经收敛为：

1. 镜像内置中文字体包
2. 启动时刷新字体缓存
3. WPS 直接导出 PDF
4. 服务直接返回结果

这比“先导出，再额外压缩十几秒”更符合 KISS 和 Fail-Fast。

## API

### `GET /api/v1/healthz`

进程存活检查。

响应示例：

```json
{"ok": true}
```

### `GET /api/v1/readyz`

运行环境检查。当前只验证真正必需的条件：

- `jobs` 目录可写
- `runtime` 目录可写
- `DISPLAY` 已配置
- `XDG_RUNTIME_DIR` 已配置
- `pywpsrpc` 可导入

### `POST /api/v1/convert-to-pdf`

上传单个文件并返回 PDF。

```bash
curl -X POST \
  -F "file=@./example.docx" \
  http://127.0.0.1:8000/api/v1/convert-to-pdf \
  --output output.pdf
```

### `POST /api/v1/convert-to-pdf/batch`

上传多个文件并返回 ZIP。

```bash
curl -X POST \
  -F "files=@./a.docx" \
  -F "files=@./b.pptx" \
  -F "files=@./c.xlsx" \
  http://127.0.0.1:8000/api/v1/convert-to-pdf/batch \
  --output outputs.zip
```

## 支持格式

- Writer: `.doc`, `.docx`
- Presentation: `.ppt`, `.pptx`
- Spreadsheet: `.xls`, `.xlsx`

## 目录结构

```text
.
├── app/
│   ├── adapters/
│   ├── api/
│   ├── services/
│   └── utils/
├── docker/
├── scripts/
├── Dockerfile
├── Office.conf
├── requirements.txt
└── README.md
```

## 运行方式

### 构建镜像

最简单的方式：

```bash
docker build -t wps-api-service:local .
```

也可以使用交互式脚本：

```bash
./scripts/build_image.sh
```

当前 `Dockerfile` 默认会：

- 下载 WPS Linux 安装包
- 下载 `https://software.cdn.vect.one/Fonts.zip`
- 把中文字体打进镜像

如果你要替换下载源，可以覆盖下面两个构建参数：

```bash
docker build \
  --build-arg WPS_DEB_URL_BASE=https://your-mirror.example.com/wps-office.deb \
  --build-arg FONTS_ZIP_URL=https://your-cdn.example.com/Fonts.zip \
  -t wps-api-service:local .
```

### 运行容器

```bash
docker run --rm \
  -p 8000:8000 \
  -v $(pwd)/workspace:/workspace \
  wps-api-service:local
```

## 本地调试

如果本机已经具备完整 Linux WPS 运行时，可以直接启动 API：

```bash
./scripts/run_local_api.sh
```

快速烟测：

```bash
./scripts/smoke_test_api.sh
./scripts/smoke_test_api.sh tests/files/经责审计报告示例.docx
```

## Dispatcher 模式

如果你想把 `docx` 批量吞吐提高到 `2x~4x`，推荐把当前服务作为轻量 dispatcher 使用，再横向起多个 worker 实例。

当前实现保持这些原则：

- 单文件接口仍然走本地 WPS 转换
- 批量接口在配置了 worker 列表后，会按轮询把文件分发到多个远程 worker
- 每个 worker 仍保持单实例内同文档族串行，稳定性边界不变

典型部署方式：

- 1 个 dispatcher
- 2~4 个 worker
- dispatcher 的 `POST /api/v1/convert-to-pdf/batch` 负责分发和打包
- worker 继续暴露同一个 `POST /api/v1/convert-to-pdf` 单文件接口

## 环境变量

- `WPS_WORKSPACE_ROOT`: 工作目录根路径，默认 `/workspace`
- `WPS_CONVERSION_TIMEOUT_SECONDS`: 转换超时秒数，默认 `120`
- `WPS_CLEANUP_MAX_AGE_SECONDS`: 历史任务清理阈值，默认 `86400`
- `WPS_MAX_UPLOAD_SIZE_BYTES`: 上传大小上限，默认 `52428800`
- `WPS_BATCH_MAX_FILES`: 批量文件数上限，默认 `10`
- `WPS_BATCH_WORKER_URLS`: 逗号分隔的 worker 基础地址列表；配置后批量接口会启用远程分发
- `WPS_DISPATCHER_REQUEST_TIMEOUT_SECONDS`: dispatcher 调 worker 的超时秒数，默认 `180`

示例：

```bash
export WPS_BATCH_WORKER_URLS=http://10.0.0.11:8000,http://10.0.0.12:8000,http://10.0.0.13:8000
export WPS_DISPATCHER_REQUEST_TIMEOUT_SECONDS=180
```

## 当前实现边界

- 单实例内同文档族串行执行，避免 WPS 自动化通道互相干扰
- 批量接口是受控并发，不保证部分成功返回
- 当前没有鉴权、队列、重试中心和任务持久化
- 如果字体缺失或字体嵌入权限异常，输出体积与兼容性会明显变差
