# syntax=docker/dockerfile:1.7
#
# onnxruntime + numpy -> self-contained wheel，**不编译**：直接复用 Alpine 官方 apk
#
# 与 Dockerfile（源码编译）产出**完全同名同结构**的 onnxruntime wheel，供 CI 在
# ppc64le / riscv64 这类 QEMU 下一编就是数小时的架构上走快速通道。
#
# 原理：Alpine 官方 community/onnxruntime（1.24.4，arch="all !s390x !x86"）已经把
# onnxruntime 编好并打成 py3-onnxruntime。装进镜像后：
#   * python 包在 /usr/lib/python3.14/site-packages/onnxruntime/
#   * capi/ 里 3 个 .so 全是指向 /usr/lib 的符号链接，真实主库在
#     /usr/lib/libonnxruntime.so.1.24.4
# 本文件只做「解引用复制 + 按 SONAME 对齐 + 补 dist-info + 打 zip」，然后交给
# auditwheel repair 收集依赖（改名为 onnxruntime.libs/ 下的 xxx-<hash>.so）、
# 改 RPATH、并把标签重打成 musllinux_1_2_<arch>。逻辑全在 pack_wheel.py 的
# --from-apk 分支，与源码路径共用同一份代码。
#
# 为什么连 numpy 一起重打包：onnxruntime 依赖 numpy，而 numpy 在 armv7 / riscv64 /
# ppc64le 上 PyPI **没有** musllinux 轮子，uv 会退回源码编译 —— 运行时镜像里没有
# 编译器。把 Alpine 的 py3-numpy（同样只依赖 musl + libopenblas）重打包进
# wheelhouse 后，`uv pip install --find-links /wheels` 在这些架构上也有二进制轮子。
# numpy 走的是同一条 --from-apk 路径，只是 --package numpy，且不需要 capi 那套
# 软链处理；auditwheel 会把 /usr/lib/libopenblas.so.3（及它依赖的 libgfortran）
# 收进 numpy.libs/，所以 BLAS 在干净镜像里也能用。
#
# 注意 auditwheel **必须**在这个装了依赖的镜像里跑：它靠遍历 musl 搜索路径定位
# 每个 DT_NEEDED，而 abseil / re2 / protobuf / icu 是 py3-onnxruntime 的运行时
# 依赖、libopenblas 是 py3-numpy 的，正好由 apk 装好（在只装 python3 的干净镜像
# 里跑会直接 "Cannot repair wheel, because required library ... could not be located"）。
#
# 用法：
#   docker buildx build --platform linux/arm64 \
#       --build-arg APK_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
#       --target wheel --output type=local,dest=./dist -f Dockerfile.repackage .
#
# 产物：./dist/wheels/onnxruntime-<ver>-cp314-cp314-musllinux_1_2_<arch>.whl
#       ./dist/wheels/numpy-<ver>-cp314-cp314-musllinux_1_2_<arch>.whl
#       ./dist/wheels/SHA256SUMS（覆盖以上两个）
# （onnxruntime wheel 的目录结构与命名与 Dockerfile 一致，CI 与 tests/smoke.py 都不用改）
#
# 另一个出口是可直接运行的 runtime 镜像（见文件末尾），它只从 wheelhouse 装包，
# 不再 `apk add py3-numpy`。

ARG ALPINE_VERSION=3.24

# loong64 没有官方 alpine 镜像，得走 ghcr.io/loong64/alpine（那个仓库里有
# py3-onnxruntime 与 py3-numpy，版本与官方仓一致），所以基座做成参数。
ARG BASE_IMAGE=alpine:${ALPINE_VERSION}

FROM ${BASE_IMAGE} AS repack

# platform tag 由 CI 按目标平台算好传进来（plan.py 的 python_host_platform）。
# 为什么需要它：arm 容器在原生 arm64 runner 上走内核 compat 模式时 uname 报 aarch64，
# Python 的 sysconfig.get_platform() 与 EXT_SUFFIX 都会跟着错——cmake 生成的扩展模块名错、
# 打包时按正确 tag 去找就找不到 .so，auditwheel 报 'no ELF executable or shared library file'。
#
# 但不能用 ENV 无条件设 _PYTHON_HOST_PLATFORM：没传 build-arg 时它是空串，而
# sysconfig.get_platform() 只要看到这个变量存在就原样返回 —— 空串会让 pip 的平台标签
# 全错（--find-links 里的 musllinux 轮子都匹配不上），wheel 名字也会变成
# xxx-cp314-cp314-.whl 被 wheel pack 拒掉。所以只在真给了值时才 export，
# 见下面每个跑 python 的 RUN 的第二行。
ARG PYTHON_HOST_PLATFORM=""


ARG ONNX_VERSION=1.24.4
ARG NUMPY_VERSION=2.4.6
ARG APK_MIRROR=""
ARG PIP_MIRROR=""
ARG AUDITWHEEL_VERSION=6.8.2
ARG TARGETARCH

LABEL maintainer="a76yyyy <q981331502@163.com>"
LABEL org.opencontainers.image.source=https://github.com/qd-today/onnxruntime-docker

# 镜像源（国内构建用）：把 APK_MIRROR 末尾的 / 去掉后整体替换 host
RUN if [ -n "${APK_MIRROR}" ]; then \
        sed -i "s|https://dl-cdn.alpinelinux.org|${APK_MIRROR%/}|g" /etc/apk/repositories; \
    fi \
 && cat /etc/apk/repositories

# 没有官方 apk 的架构先 fail-fast，别让 apk 报一句 "unsatisfiable constraints" 就完事：
#   386 / s390x —— 官方配方 arch="all !s390x !x86" 排除了它们。注意 Alpine 的 `x86`
#                  指 32 位的 i386，不是 amd64；amd64 是有包的
#   armv6      —— 官方配方 armhf 分支只出 base 包，Python 绑定被 py3-flatbuffers 挡住
#   loong64    —— 官方没有这个架构，但 ghcr.io/loong64/alpine 有同版本的
#                  py3-onnxruntime / py3-numpy，把 BASE_IMAGE 指过去即可
# 注意：TARGETARCH 对 armv6 和 armv7 都是 `arm`，必须连 TARGETVARIANT 一起看，
# 否则 armv7 会被误拦。
RUN case "${TARGETARCH}/${TARGETVARIANT}" in \
        386/|s390x/|arm/v6) \
            echo "错误: 该架构无 apk py3-onnxruntime（386/s390x 是 Alpine 配方 arch=\"all !s390x !x86\" 排除；armv6 的 Python 绑定被 py3-flatbuffers 挡住）" >&2; \
            exit 1 ;; \
    esac && \
    if ! apk search -x py3-onnxruntime >/dev/null 2>&1; then \
        echo "错误: 当前基座（${BASE_IMAGE}）没有 py3-onnxruntime；loong64 请传 --build-arg BASE_IMAGE=ghcr.io/loong64/alpine:${ALPINE_VERSION}" >&2; \
        exit 1; \
    fi

# py3-onnxruntime 提供包本体，同时把 abseil-cpp / re2 / libprotobuf-lite / icu-libs
# 这些运行时依赖一并装好 —— auditwheel 要在本镜像里找得到它们才修得了 wheel；
# py3-numpy 同理：它把 libopenblas / libgfortran 带进来，auditwheel 才能把
# libopenblas.so.3 收进 numpy.libs/（这也是这里装 numpy 而不是 pip 装的原因：
# pip 装出来的 wheel 已经 bundle 过一遍，再 repair 会嵌套）；
# py3-pip 用来装 auditwheel；py3-wheel 提供 `python3 -m wheel pack`；
# binutils 提供 readelf；patchelf 由 auditwheel 自己调用
RUN apk add --no-cache \
        binutils \
        patchelf \
        py3-numpy \
        py3-numpy-dev \
        py3-onnxruntime \
        py3-pip \
        py3-wheel \
        python3

RUN set -eux; \
    if [ -n "${PYTHON_HOST_PLATFORM}" ]; then export _PYTHON_HOST_PLATFORM="${PYTHON_HOST_PLATFORM}"; fi; \
    if [ -n "${PIP_MIRROR}" ]; then \
        pip_args="-i ${PIP_MIRROR}"; \
    else \
        pip_args=""; \
    fi; \
    python3 -m pip install --no-cache-dir --break-system-packages \
        ${pip_args} "auditwheel==${AUDITWHEEL_VERSION}"; \
    python3 -m auditwheel --version

# 合规模块与许可证全文：pack_wheel.py 在收尾（--compliance）时按同目录 import。
# 只进构建阶段，运行镜像里没有这些文件。
COPY src/pack_wheel.py src/compliance.py /usr/local/bin/
COPY src/licenses /usr/local/share/qdpack/licenses
# apk 清单随 wheelhouse 发布，上层照单安装即可，不必各自维护
COPY system-packages.txt /wheels/system-packages.txt

# 两个包都走同一条 --from-apk 路径，产物落在同一个 /wheels：
#   numpy         —— 包内 .so 是真文件，原样交给 auditwheel
#   onnxruntime   —— capi 里是软链，多两步解引用 + 按 SONAME 命名
RUN set -eux; \
    if [ -n "${PYTHON_HOST_PLATFORM}" ]; then export _PYTHON_HOST_PLATFORM="${PYTHON_HOST_PLATFORM}"; fi; \
    site=/usr/lib/python3.14/site-packages; \
    python3 /usr/local/bin/pack_wheel.py \
        --from-apk "${site}" \
        --package numpy \
        --out-dir /wheels \
        --expect-version "${NUMPY_VERSION}"; \
    python3 /usr/local/bin/pack_wheel.py \
        --from-apk "${site}" \
        --package onnxruntime \
        --out-dir /wheels \
        --expect-version "${ONNX_VERSION}"; \
    python3 /usr/local/bin/pack_wheel.py --emit-index --compliance --out-dir /wheels; \
    cd /wheels; \
    ls -l; \
    sha256sum ./*.whl > SHA256SUMS; \
    cat SHA256SUMS

########################################################################
# 产物出口（与 Dockerfile 的 wheel 阶段一致，CI 用 --output type=local 取出）
########################################################################
FROM scratch AS wheel

COPY --from=repack /wheels/ /wheels/

########################################################################
# 编译产物（两段式构建的第一段导出物）
#
# 与 pycurl-docker / ddddocr-docker 的 Dockerfile 同构。本仓库 runtime 不需要额外
# 二进制，产物只有 /wheels。
#
#   docker buildx build --target artifacts --output type=local,dest=dist .
########################################################################
FROM scratch AS artifacts

COPY --from=repack /wheels/ /wheels/

########################################################################
# 外部产物注入点（--build-context wheelsrc=<dist>）
########################################################################
FROM scratch AS wheelsrc

########################################################################
# 从产物构造 wheelhouse 镜像（两段式的第二段，不需要重新重打包）
########################################################################
FROM scratch AS wheel-from-artifacts

COPY --from=wheelsrc /wheels /wheels

########################################################################
# 运行时出口：自带 numpy / onnxruntime 的可运行镜像，不依赖 apk 的 py3-numpy
#
# 为什么不用 `COPY --from=repack /wheels /wheels` + `RUN rm -rf /wheels`：
# COPY 和 RUN 是两层，删掉的字节仍然留在下层里，`docker images` 照样算进去
# （本架构两个 wheel 合计约 25MB 白花）。这里用 BuildKit 的 bind mount 把
# wheelhouse 只挂进这一条 RUN，装完即随 layer 一起消失，镜像里根本没有 /wheels。
#
# 需要给上层 `COPY --from` 复用 wheel 的，用 `--target wheel` 出的那个镜像
# （CI 里发成 `a76yyyy/onnxruntime:<ver>-wheelhouse`），它本来就只有 /wheels。
########################################################################
# 与 repack 同一个基座：loong64 没有官方 alpine，运行层也得跟着换
FROM ${BASE_IMAGE} AS runtime

ARG ONNX_VERSION=1.24.4
ARG NUMPY_VERSION=2.4.6
ARG APK_MIRROR=""
ARG PIP_MIRROR=""

LABEL maintainer="a76yyyy <q981331502@163.com>"
LABEL org.opencontainers.image.source=https://github.com/qd-today/onnxruntime-docker

RUN if [ -n "${APK_MIRROR}" ]; then \
        sed -i "s|https://dl-cdn.alpinelinux.org|${APK_MIRROR%/}|g" /etc/apk/repositories; \
    fi \
 && cat /etc/apk/repositories

# python3 必须 apk 装：解释器与 musl 的 libc 基线由它提供（两个 wheel 各自把
# libstdc++ / libgcc_s / openblas 之类收在自己的 .libs 下，与 python3 无关）。
# **不能**再装 py3-numpy —— numpy 只从 wheelhouse 来，这正是本文件要证明的事。
# 直接按 wheelhouse 里的清单装：清单写错会在这里构建失败，不会等到上层才发现。
# 装包：本地 wheelhouse 的索引优先，PyPI 兜底纯 Python 依赖。
#
# 索引优先级（uv 官方口径 + 实测）：`--index-url` 指定的默认索引是最低优先级，
# `--extra-index-url` 按定义顺序查询，first-index 策略下第一个含该包的索引胜出。
# 实测（往本地 wheel 里塞标记文件后安装）：本地那份一定被选中，PyPI 只在本地没有该包时兜底。
# 纯 Python 依赖（flatbuffers / packaging / protobuf / sympy）本地没有，走 PyPI。
#
# `--no-cache` 不是省事，是必须：uv 默认把解压后的包再存一份到 /root/.cache/uv，
# 会原样进镜像层（实测多出 130MB，比两个包本身还大）。
# uv 是工具不是运行时依赖：装包和删除放在同一个 RUN 里。它是 27 MiB 的独立二进制，
# 留着白占体积；分两层删除无效（字节仍留在下层）。
RUN --mount=type=bind,from=wheelsrc,source=wheels,target=/wheels \
    set -eux; \
    apk add --no-cache $(cat /wheels/system-packages.txt) uv; \
    uv pip install --system --break-system-packages --no-cache \
        --index-url "${PIP_MIRROR:-https://pypi.org/simple}" \
        --extra-index-url file:///wheels/simple \
        "numpy==${NUMPY_VERSION}" "onnxruntime==${ONNX_VERSION}"; \
    uv pip check --system; \
    python3 -c "import onnxruntime, numpy; \
print('onnxruntime', onnxruntime.__version__, onnxruntime.get_available_providers()); \
print('numpy', numpy.__version__)"; \
    rm -rf /tmp/uv-* /root/.cache/uv; \
    apk del uv

# runtime 是给上层 FROM 的基座，`docker run <image>` 打一次版本自检
CMD ["python3", "-c", "import onnxruntime, numpy; print('onnxruntime', onnxruntime.__version__, '| numpy', numpy.__version__)"]
