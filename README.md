# onnxruntime Docker 镜像（Alpine）
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fqd-today%2Fonnxruntime-docker.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2Fqd-today%2Fonnxruntime-docker?ref=badge_shield)


onnxruntime 与 numpy 的 musllinux wheel 分发，另附装好的可运行镜像。两个包都从 Alpine 的 apk
预编译包重打包成 wheel，不产出 apk 包。

- Docker Hub：<https://hub.docker.com/r/a76yyyy/onnxruntime>
- 源码：[qd-today/onnxruntime-docker](https://github.com/qd-today/onnxruntime-docker)

## 快速开始

```dockerfile
# syntax=docker/dockerfile:1.7
FROM alpine:3.24
RUN --mount=type=bind,from=a76yyyy/onnxruntime:wheelhouse,source=/wheels,target=/w1 \
    apk add --no-cache $(cat /w1/system-packages.txt) uv && \
    uv pip install --system --break-system-packages --no-cache \
        --index-url https://pypi.org/simple \
        --extra-index-url file:///w1/simple \
        "numpy==2.4.6" "onnxruntime==1.24.4"
```

| 写法 | 原因 |
|---|---|
| `apk add $(cat /w1/system-packages.txt)` | 清单是 wheelhouse 的运行时前置条件（解释器与 `libz`），写错会在这里构建失败 |
| `--index-url https://pypi.org/simple` | 默认索引是**最低**优先级，只用于兜底纯 Python 依赖（flatbuffers / packaging / protobuf / sympy） |
| `--extra-index-url file:///w1/simple` | first-index 策略下第一个含该包的索引胜出，因此本地那份一定被选中 |
| 发行名与 PyPI 同名 | wheel 用 PyPI 同名发行名，换回 PyPI 版不用改任何配置 |

`/w1/simple` 是 PEP 503 索引，链接为相对路径，可与 wheel 目录一起挂到任意位置。

## 镜像内容

| 组件 | wheelhouse | runtime |
|---|---|---|
| 基础镜像 | `scratch` | `alpine:3.24`；loong64 为 `ghcr.io/loong64/alpine:3.24` |
| Python | — | 3.14 |
| onnxruntime | 1.24.4（`musllinux_1_2`） | 同左 |
| numpy | 2.4.6（`musllinux_1_2`） | 同左 |
| 额外系统包 | — | `ca-certificates`、`icu-data-en` |
| 内容位置 | `/wheels` | `/usr/lib/python3.14/site-packages` |

## 支持的平台

| 平台 | wheelhouse | runtime | wheel 架构名 |
|---|---|---|---|
| `linux/amd64` | ✓ | ✓ | `x86_64` |
| `linux/arm64` | ✓ | ✓ | `aarch64` |
| `linux/arm/v7` | ✓ | ✓ | `armv7l` |
| `linux/ppc64le` | ✓ | ✓ | `ppc64le` |
| `linux/riscv64` | ✓ | ✓ | `riscv64` |
| `linux/loong64` | ✓ | ✓ | `loongarch64` |

`linux/386`、`linux/s390x` 不提供：Alpine 的 onnxruntime 配方是 `arch="all !s390x !x86"`，
这两个架构没有可用的 Python 绑定。

loong64 构建需 `--build-arg BASE_IMAGE=ghcr.io/loong64/alpine:3.24`（Docker Hub 官方
alpine 无此架构）。

## 标签

| 标签 | 内容 |
|---|---|
| `<ver>-wheelhouse` | `scratch` + `/wheels`：onnxruntime、numpy、`SHA256SUMS`、`system-packages.txt`、PEP 503 索引 |
| `<ver>` | `alpine:3.24` + Python 3.14 + 上述两个包 |
| `wheelhouse` / `latest` | 上面两者的滚动标签 |
| `dev` / `dev-wheelhouse` | 上面两者的开发版，跟随 main 分支的最新构建 |

`<ver>` 与 `latest` 只在 tag 推送时产出，latest 始终追最近一次 tag；main 推送与 workflow_dispatch 只推进 dev，不动正式标签与滚动标签。

## 构建

wheel 的构建与镜像的组装分两步：第一步产出 wheel，第二步用这份产物组装镜像。两步之间可以用
不同的架构环境（CI 里 arm 系列在原生 arm64 上重打包，组装统一在 x86_64）。

```bash
# ① 编译产物：/wheels
docker buildx build --platform linux/arm64 --target artifacts \
    --output type=local,dest=./dist .

# ② 可运行镜像
docker buildx build --platform linux/arm64 --target runtime \
    --build-context wheelsrc=./dist -t onnxruntime:1.24.4 .

# ③ wheelhouse 镜像（供上层 COPY --from / bind mount）
docker buildx build --platform linux/arm64 --target wheel-from-artifacts \
    --build-context wheelsrc=./dist -t onnxruntime:wheelhouse .
```

`--target runtime` 不传 `--build-context wheelsrc` 会失败：镜像里必须有 onnxruntime，宁可报错
也不要静默产出一个空基座。

| 参数 | 默认 | 说明 |
|---|---|---|
| `ALPINE_VERSION` | `3.24` | 决定 Python 版本与 wheel 的 `cpXX` tag |
| `BASE_IMAGE` | `alpine:${ALPINE_VERSION}` | loong64 传 `ghcr.io/loong64/alpine:3.24` |
| `ONNX_VERSION` | `1.24.4` | 与 apk 版本不一致时构建失败 |
| `NUMPY_VERSION` | `2.4.6` | 同上 |
| `AUDITWHEEL_VERSION` | `6.8.2` | 依赖收集与打标 |
| `APK_MIRROR` | 空 | 本地加速用，如 `https://mirrors.tuna.tsinghua.edu.cn` |
| `PIP_MIRROR` | 空 | 安装 auditwheel 用；空 = 官方 PyPI |

## 产物

```
/wheels/
  onnxruntime-1.24.4-cp314-cp314-musllinux_1_2_<arch>.whl
  numpy-2.4.6-cp314-cp314-musllinux_1_2_<arch>.whl
  SHA256SUMS
  system-packages.txt
  simple/
    index.html
    <project>/index.html
```

`system-packages.txt` 是上层需要 `apk add` 的包（一行一个）：`python3` 与 `zlib`。前者是
解释器；后者因为 musllinux 白名单只有 `libc` 与 `libz`，两个 wheel 里的部分 `.so` 都动态链接了
`libz.so.1`，它不会被收进 `.libs/`。

| 标签 | 含义 | PyPI 接受 |
|---|---|---|
| `musllinux_1_2_<arch>` | 在 musl ≥ 1.2 上构建，除 musl 外不依赖系统库 | 是 |
| `linux_<arch>` | 只说明 CPU 架构 | 否 |

## 依赖处理

### numpy

`Requires-Dist: numpy>=1.21.6`，而 PyPI 只有 `x86_64` / `aarch64` 的 numpy musllinux 轮子，
因此所有架构都从 Alpine 的 `py3-numpy` 重打包。

| | 自建（Alpine `py3-numpy`） | PyPI 官方 |
|---|---|---|
| OpenBLAS | Alpine 系统包 | `scipy-openblas64` |
| `DYNAMIC_ARCH` | 0（编译期锁定） | 1（运行期派发） |
| BLAS 整数宽度 | 32 位 | 64 位 |
| 覆盖架构 | 全部支持的架构 | `x86_64` / `aarch64` |

OCR 与数据处理不走 BLAS，未按架构拆分。

### auditwheel

打包前 wheel 里只放三个 `.so`，其余由 `auditwheel repair` 按 `DT_NEEDED` 补进 `<pkg>.libs/`
并改写 RPATH。它看不到的两项：

| 项 | 处理 |
|---|---|
| `libonnxruntime_providers_shared.so` | 非 `DT_NEEDED`，由 `Env::GetRuntimePath()` 拼路径 `dlopen`，须手动放进 `capi/` |
| `capi/libonnxruntime.so.1` | 不在 pybind 的 `DT_NEEDED` 里，留给 C API 与 `dlopen` |

`auditwheel repair` 要从系统里解析每个 `DT_NEEDED`，因此构建镜像必须已装被解析的库；缺少时报
`Cannot repair wheel, because required library "libabsl_..." could not be located`。

| 打包目标 | 需已安装的 apk 包 | 由它带入 |
|---|---|---|
| onnxruntime | `py3-onnxruntime` | `abseil-cpp-*`、`re2`、`libprotobuf-lite`、`icu-libs` |
| numpy | `py3-numpy` | `libopenblas`、`libgfortran` |

不必装 `-dev` 包，运行时库即可。不传 `--plat`：auditwheel 从 ELF interpreter 认出 `libc=musl`。

### 镜像里的空间占用

| 做法 | 原因 |
|---|---|
| `RUN --mount=type=bind` 挂 `/wheels` | `COPY` + `rm` 会留下下层字节 |
| `uv pip install --no-cache` | 否则解压副本写入 `/root/.cache/uv` |
| 装完 `apk del uv`，且与安装同在一个 RUN | uv 是 27 MiB 的独立二进制，只是工具；分两层删除无效 |

## 已知限制

- 只含 `icu-data-en`。`libre2` 处理非英语 Unicode 字符类需 `apk add icu-data-full`，会让镜像
  的可选依赖体积翻几倍；默认不装。
- sympy + mpmath 是 runtime 镜像里最大的一块，onnxruntime 的硬依赖，去掉会破坏形状推断。
- wheel 的 zip 条目带 mtime，构建不可逐字节复现；内容一致，`sha256` 每次不同。

## 维护者

<details>
<summary>版本参数</summary>

上游发新版时，改这里或 dispatch 时传同名输入即可构建新版本；产物版本与传入版本不一致时
构建失败（`--expect-version` 断言）。

| 参数 | 作用 | 默认值 |
|---|---|---|
| `onnx_version` | onnxruntime 版本，同时是 runtime 镜像的 tag | 1.24.4 |
| `numpy_version` | numpy 版本（决定 wheelhouse 里那个 numpy wheel） | 2.4.6 |
| `alpine_version` | alpine 版本，决定 Python 小版本与 wheel 的 cpXX tag | 3.24 |

这两个包来自 Alpine 的 apk 预编译包（`py3-onnxruntime` / `py3-numpy`），版本受 apk 仓限制：
参数只能钉住 apk 里已有的版本，apk 更新后再改这里（版本对不上时构建会在断言处失败，
不会产出错标版本的 wheel）。PyPI 的 musllinux 轮子只覆盖 x86_64 与 aarch64，其余架构
只能走 apk 或自行源码构建。

</details>

<details>
<summary>CI 结构</summary>

`Build Image.yml` 是调用方，共用 `.github/workflows/_image.yml`，四段：
plan → build-wheel → build → smoke → publish。

- 仅 `workflow_dispatch` 与 tag push 触发
- 每平台两个 job：`build-wheel` 重打包 wheel 并导出成 artifact，`build` 用这份产物组装两个出口
  并推 digest。拆开是为了让 arm 系列在原生 arm64 runner 上重打包（`--output type=local`
  不支持多平台，只能按平台拆）
- `build` 统一跑 x86_64：arm64 runner 走内核 compat 模式时 Python 自报 `linux-aarch64`，
  uv 在那装 wheel 会挂死
- wheel 的 platform tag 由 `_PYTHON_HOST_PLATFORM` 显式指定（按 `TARGETARCH`/`TARGETVARIANT`
  算，CI 经 build-arg 传入 Dockerfile 的 ENV）。不指定时 compat 模式下 Python 会把 arm32 的
  wheel 标成 `linux_aarch64`，auditwheel 随即因 `.so` 与标签不符失败
- 冒烟直接跑 runtime 镜像：`tests/smoke.py` 校验环境架构、numpy 与一次线性代数、onnxruntime
  版本与 provider、四个产物的 ELF `e_machine`、一次真实推理
- 闸门 fail-closed：build 与 smoke 均 `success` && 平台集合完整，才合并 manifest

</details>

<details>
<summary>归档</summary>

`archive/source-build` 分支保留从源码编译 onnxruntime 的版本（`Dockerfile` + `alpine/` 配方快照）。
它只产 onnxruntime 的 wheel，不产 numpy 与 runtime 镜像，不在 CI 覆盖范围内。

</details>

<details>
<summary>src/pack_wheel.py</summary>

三个入口：`--wheel-dir`（已归档的源码路径）、`--from-apk`（当前路径，`--package` 选包）、
`--emit-index`（生成 PEP 503 索引）。保留的两步：解引用 apk 里的符号链接、按 SONAME 改名
（`libonnxruntime.so.1.24.4` → `libonnxruntime.so.1`）；软链不能进 wheel，`package_data`
会跟随软链导致同一份内容打两遍。

</details>


## License
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fqd-today%2Fonnxruntime-docker.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2Fqd-today%2Fonnxruntime-docker?ref=badge_large)