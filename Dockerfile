# 基础镜像
FROM alpine:edge AS onnxruntime-alpine-apk-builder

# 维护者信息
LABEL maintainer="a76yyyy <q981331502@163.com>"
LABEL org.opencontainers.image.source=https://github.com/qd-today/onnxruntime-docker
ARG TARGETPLATFORM

ARG APK_MIRROR=""  #e.g., https://mirrors.tuna.tsinghua.edu.cn
ARG PIP_MIRROR=""  #e.g., https://pypi.tuna.tsinghua.edu.cn/simple
ARG GIT_DOMAIN=https://github.com   #e.g., https://gh-proxy.com/https://github.com or https://gitee.com

# Envirenment for onnxruntime
ARG ONNX_VERSION=v1.20.1

RUN echo "http://dl-cdn.alpinelinux.org/alpine/edge/testing" >> /etc/apk/repositories \
    # 替换为国内清华源
    && sed -i 's/dl-cdn.alpinelinux.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apk/repositories \
    && apk add \
        --update-cache \
        abuild \
        alpine-conf \
        alpine-sdk \
        doas \
    && setup-apkcache /var/cache/apk \
    && mkdir -p /pkgs/apk \
    && echo 'builder ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers \
    && echo "permit nopass :abuild" >> /etc/doas.d/doas.conf \
    && adduser -D -G abuild builder && su builder -c 'abuild-keygen -a -n -i'

COPY alpine/ /src/testing/onnxruntime
RUN chown -R builder /pkgs /src
USER builder

RUN cd /src/testing/onnxruntime \
    && abuild checksum \
    && git init \
    && abuild -r -P /pkgs/apk

# OnnxRuntime Alpine Docker
FROM alpine:edge

# 维护者信息
LABEL maintainer="a76yyyy <q981331502@163.com>"
LABEL org.opencontainers.image.source=https://github.com/qd-today/onnxruntime-docker
ARG TARGETPLATFORM

COPY --from=onnxruntime-alpine-apk-builder /pkgs/apk/ /pkgs/apk/
RUN apk add \
        --no-cache \
        --allow-untrusted /pkgs/apk/*/*/*.apk \
    && rm -rf /pkgs
