#!/usr/bin/env python3
"""把 apk / 源码里的某个包变成「自包含 wheel」。两条入口共用同一套组装逻辑：

* ``--wheel-dir``（源码路径，Dockerfile 用）：吃 setup.py ``bdist_wheel`` 的产物，
  补齐 capi 里的库、解软链，再交给 auditwheel 修复后重新打包。
* ``--from-apk``（重打包路径，Dockerfile 用）：直接吃 Alpine 官方包
  装进 site-packages 的目录树（``--package`` 指定包名，默认 onnxruntime），
  组装成 wheel 树后修复打包。目前已用于 onnxruntime 与 numpy。
* ``--emit-index``（收尾，Dockerfile 用）：为 ``--out-dir`` 里已有的 wheel 生成
  PEP 503 索引 ``simple/``，供 uv/pip 用 ``--extra-index-url file:///wheels/simple``
  消费。链接是相对路径（``../../<wheel>``），所以挂到任何目录都成立，且多个
  wheelhouse 可以各带一份索引、由 ``--extra-index-url`` 叠加（顺序即优先级）。

依赖收集（DT_NEEDED 递归闭包）与 RPATH 全部交给 ``auditwheel repair``：
它会把 abseil / re2 / protobuf / icu 这类非系统库改名塞进 ``onnxruntime.libs/``，
把每个 ELF 的 RPATH 设成 ``$ORIGIN:$ORIGIN/../../onnxruntime.libs``，并把标签
从 ``linux_<arch>`` 改成 ``musllinux_1_2_<arch>``。因此本脚本只保留三件它做不到
或不该由它做的事（**仅 onnxruntime**，见 ``prepare_capi``；其它包如 numpy 的 .so
是包内真文件，直接原样交给 auditwheel，它会把 libopenblas 之类收进
``numpy.libs/``）：

1. 解引用软链。alpine 的包是这样装的：``libonnxruntime.so.1.24.4`` 和
   ``libonnxruntime_providers_shared.so`` 放在 ``/usr/lib``，python 目录里的
   ``libonnxruntime.so.1`` / ``libonnxruntime.so.1.24.4`` /
   ``libonnxruntime_providers_shared.so`` 是指过去的符号链接。软链不能进 wheel
   （zip 会按每个链接各存一份真实内容，15MB 的主库直接翻倍），这里解引用成真实
   文件。

2. 按 SONAME 对齐名字。cmake 只把 ``libonnxruntime.so.1.24.4``（真实文件）拷进
   ``onnxruntime/capi/``，而 ``libonnxruntime.so.1`` 这个名字是运行时按名字加载
   的 —— wheel 里已有同名库时 auditwheel 不会重复打包，也不会替你改名，所以
   必须先在 capi 里放成 SONAME 文件名，同时避免 ``.so`` / ``.so.1`` / ``.so.1.24.4``
   三份重复。

3. 把 ``libonnxruntime_providers_shared.so`` 一起放进 capi。它是 ``dlopen`` 的
   （没有 DT_NEEDED，也没有任何 DT_NEEDED 指向它），auditwheel 不会把它「收集」
   进来；但它自己也没有任何非系统依赖，原样放进去即可，不需要再加工。

auditwheel 的 policy 不靠 ``--plat`` 硬编码：它从 ELF 的 interpreter 认出 musl，
自动给出 ``musllinux_1_2_<arch>``，所以本脚本对 amd64 / arm64 / armv7 等一视同仁。

最后 ``auditwheel repair`` 自己会重写 RECORD 与 WHEEL 元数据，不必再 ``wheel pack``。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

# 查找依赖库的位置（musl 没有 ldconfig）
SEARCH_DIRS = ("/usr/lib", "/lib", "/usr/local/lib")


def readelf_dyn(path: Path) -> tuple[list[str], str | None]:
    out = subprocess.run(
        ["readelf", "-d", str(path)], capture_output=True, text=True, check=True
    ).stdout
    needed = re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", out)
    soname = re.search(r"\(SONAME\)\s+Library soname: \[(.+?)\]", out)
    return needed, (soname.group(1) if soname else None)


def find_library(name: str, extra_dirs: list[Path]) -> Path | None:
    for d in (*extra_dirs, *(Path(x) for x in SEARCH_DIRS)):
        candidate = d / name
        if candidate.exists():
            return candidate
    return None


def shared_objects(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_file() and ".so" in p.name)


def die(message: str) -> int:
    print(f"错误: {message}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
# 组装 capi：解软链 + 按 SONAME 对齐（两条入口共用）
# --------------------------------------------------------------------------- #
def materialize_symlinks(capi: Path, extra_dirs: list[Path]) -> None:
    """把 capi 里指向 ``/usr/lib`` 的符号链接换成真实文件（按 SONAME 命名）。"""
    # 分两趟：先解出所有 (链接, 真实文件, SONAME)，再统一删链接、再复制。
    # 不能边删边复制 —— 目标名常常与链接同名（libonnxruntime_providers_shared.so
    # 的 SONAME 就是它自己），dest.exists() 会看到链接本身而跳过复制，接着
    # unlink 就把唯一一份删掉了。
    pending = []
    for link in sorted(capi.iterdir()):
        if not link.is_symlink():
            continue
        src = find_library(link.name, extra_dirs)
        if src is None:
            raise SystemExit(f"错误: {link.name} 是符号链接，但系统里找不到真实文件")
        real = Path(os.path.realpath(src))
        pending.append((link, real, readelf_dyn(real)[1] or real.name))

    for link, _, _ in pending:
        link.unlink()
    for _, real, soname in pending:
        dest = capi / soname
        if dest.exists():
            continue
        print(f"解引用 {real.name} -> {soname}")
        shutil.copyfile(real, dest)
        shutil.copymode(real, dest)


def align_sonames(capi: Path) -> None:
    """按 SONAME 重命名 capi 里的库，避免 .so / .so.1 / .so.1.24.4 三份重复。"""
    for lib in shared_objects(capi):
        _, soname = readelf_dyn(lib)
        if not soname or lib.name == soname:
            continue
        target = capi / soname
        if target.exists():
            lib.unlink()
        else:
            print(f"按 SONAME 重命名 {lib.name} -> {soname}")
            lib.rename(target)


def prepare_capi(capi: Path, extra_dirs: list[Path]) -> None:
    materialize_symlinks(capi, extra_dirs)
    align_sonames(capi)


# --------------------------------------------------------------------------- #
# 打包 + 修复
# --------------------------------------------------------------------------- #
def auditwheel_repair(raw_wheel: Path, out_dir: Path) -> None:
    """让 auditwheel 收集 DT_NEEDED 闭包、改名入 <pkg>.libs/、改 RPATH、重打标签。

    必须在**装了这些库的构建镜像**里跑：auditwheel 靠遍历 musl 的搜索路径
    （/lib、/usr/lib、LD_LIBRARY_PATH）去定位每个 NEEDED，找不到就直接
    ``ValueError: Cannot repair wheel, because required library "..." could not be
    located``。不加 ``--plat`` 让它自己从 ELF interpreter 认出 musllinux_1_2_<arch>。
    """
    subprocess.run(
        [
            sys.executable, "-m", "auditwheel", "repair",
            "--wheel-dir", str(out_dir),
            str(raw_wheel),
        ],
        check=True,
    )


def report(wheel: Path, package: str) -> None:
    """打印产物与 auditwheel 收进来的依赖库清单（CI 日志里快速核对，不做断言）。"""
    print(f"\n产物: {wheel.name}")
    print(f"大小: {wheel.stat().st_size / 1024 / 1024:.1f} MiB")
    with zipfile.ZipFile(wheel) as zf:
        entries = [(i.filename, i.file_size) for i in zf.infolist() if ".so" in i.filename]

    libs_prefix = f"{package}.libs/"
    bundled = sorted((e for e in entries if e[0].startswith(libs_prefix)), key=lambda e: -e[1])
    print(f"{libs_prefix} 库数量: {len(bundled)}")
    for name, size in bundled:
        print(f"  {name[len(libs_prefix):]:48s} {size / 1024 / 1024:8.2f} MiB")

    own = sorted((e for e in entries if not e[0].startswith(libs_prefix)), key=lambda e: -e[1])
    print(f"{package}/ 里 .so 数量: {len(own)}")
    for name, size in own:
        if size >= 1024 * 1024:
            print(f"  {name:48s} {size / 1024 / 1024:8.2f} MiB")


def pack_tree(unroot: Path, out_dir: Path, package: str) -> Path:
    """把组装好的 wheel 树打成 wheel，再交给 auditwheel 修复，返回最终产物。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    # 中间产物必须落在 out_dir 之外：CI 用 --output type=local 把整个目录取走，
    # 两个 wheel 混在一起会让下游的 *.whl 通配符装错文件。
    staging = Path(tempfile.mkdtemp(prefix="ortraw-"))
    # out_dir 里可能已经有别的包的 wheel（先打 numpy、再打 onnxruntime），
    # 所以用「新增了哪个」来判断本次产物，而不是断言目录里恰好 1 个。
    before = {p.resolve() for p in out_dir.glob("*.whl")}
    try:
        subprocess.run(
            [sys.executable, "-m", "wheel", "pack", str(unroot), "--dest-dir", str(staging)],
            check=True,
        )
        raw_wheel = next(staging.glob("*.whl"))
        auditwheel_repair(raw_wheel, out_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    produced = sorted({p.resolve() for p in out_dir.glob("*.whl")} - before)
    if len(produced) != 1:
        raise SystemExit(f"错误: 期望 {out_dir} 里新增恰好 1 个 wheel, 实际 {produced}")
    report(produced[0], package)
    return produced[0]


def from_source_wheel(wheel_dir: Path, out_dir: Path, extra_dirs: list[Path]) -> int:
    """源码路径：解包 setup.py 的 wheel -> 组装 capi -> 修复打包。"""
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        return die(f"期望 {wheel_dir} 里恰好 1 个 wheel, 实际 {len(wheels)}: {wheels}")

    work = Path(tempfile.mkdtemp(prefix="ortwheel-"))
    try:
        subprocess.run(
            [sys.executable, "-m", "wheel", "unpack", str(wheels[0]), "-d", str(work)],
            check=True,
        )
        unroot = next(p for p in work.iterdir() if p.is_dir())
        capi = unroot / "onnxruntime" / "capi"
        if not capi.is_dir():
            return die(f"{capi} 不存在")

        # setup.py 的 package_data 有时漏掉 /usr/lib 里的 onnxruntime 库
        for pattern in ("libonnxruntime.so*", "libonnxruntime_providers_shared.so"):
            if list(capi.glob(pattern)):
                continue
            for d in extra_dirs:
                for src in sorted(d.glob(pattern)):
                    print(f"补充 {src.name} -> onnxruntime/capi/")
                    shutil.copyfile(src, capi / src.name)

        prepare_capi(capi, extra_dirs)
        pack_tree(unroot, out_dir, "onnxruntime")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 重打包路径：Alpine 官方 apk -> wheel
# --------------------------------------------------------------------------- #
def copy_package(src: Path, dst: Path) -> None:
    """复制 python 包目录，跳过 __pycache__ / *.pyc（apk 预编译的用不上）。"""
    shutil.copytree(
        src,
        dst,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def normalize_name(name: str) -> str:
    """PEP 503 归一化：dist-info 目录名里 name 与 version 用 ``-`` 分隔。"""
    return re.sub(r"[-_.]+", "_", name).lower()


def find_metadata_dir(site_packages: Path, package: str) -> Path:
    """找 <package> 的元数据目录。

    setuptools 打的包装成 ``<name>-<ver>.egg-info``（onnxruntime 就是），
    meson-python / PEP 517 的包装成 ``<name>-<ver>.dist-info``（numpy 是）。
    两者都要认，否则必然有一个包得靠 ``--package`` 特判。
    """
    patterns = {
        f"{package}-*.dist-info",
        f"{package}-*.egg-info",
        f"{normalize_name(package)}-*.dist-info",
        f"{normalize_name(package)}-*.egg-info",
    }
    found = sorted({p for pat in patterns for p in site_packages.glob(pat) if p.is_dir()})
    if len(found) != 1:
        raise SystemExit(
            f"错误: 期望 {site_packages} 里恰好 1 个 {package} 的 dist-info/egg-info, "
            f"实际 {found}（apk 没装对应的 py3-{package}？）"
        )
    return found[0]


def metadata_file(meta_dir: Path) -> Path:
    """egg-info 的叫 PKG-INFO，dist-info 的叫 METADATA，内容都是 PEP 566。"""
    for name in ("METADATA", "PKG-INFO"):
        path = meta_dir / name
        if path.exists():
            return path
    raise SystemExit(f"错误: {meta_dir} 里既没有 METADATA 也没有 PKG-INFO")


def metadata_version(pkg_info: Path) -> str:
    for line in pkg_info.read_text(errors="replace").splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
        if not line.strip():
            break
    raise SystemExit(f"错误: {pkg_info} 里没有 Version:")


def host_platform() -> str:
    """打包机的平台标签（``linux_aarch64`` 这种）。

    不能直接用 ``sysconfig.get_platform()``：Dockerfile 里为了跨架构把
    ``_PYTHON_HOST_PLATFORM`` 设成了 ``$PYTHON_HOST_PLATFORM``，本地不传 build-arg
    时它就是个**空串**，而 get_platform() 见到这个变量就原样返回 —— wheel 名字会变成
    ``numpy-2.4.6-cp314-cp314-.whl``，被 ``wheel pack`` 判成 Bad wheel filename 直接
    失败。这里补一个回退：值本身只是过渡用的，auditwheel 之后会把标签重打成
    ``musllinux_1_2_<arch>``。
    """
    plat = sysconfig.get_platform()
    if not plat:
        import platform as _platform

        plat = "linux-" + (_platform.machine() or "unknown")
    return plat.replace("-", "_").replace(".", "_")


def write_dist_info(unroot: Path, meta_dir: Path, version: str, package: str) -> Path:
    """把 apk 里的 dist-info/egg-info 变成 wheel 要的 dist-info 目录。"""
    major, minor = sys.version_info[:2]
    plat_name = host_platform()
    tag = f"cp{major}{minor}-cp{major}{minor}-{plat_name}"
    dist_info = unroot / f"{normalize_name(package)}-{version}.dist-info"

    if meta_dir.name.endswith(".dist-info"):
        # 现代 dist-info 里除了 METADATA 还有 licenses/（PEP 639，METADATA 的
        # License-File: 指向它）、entry_points.txt 等。整体复制，只剔掉三个由
        # wheel 自己重写/本机相关的文件，否则 METADATA 里的 License-File 会指向
        # 一个不存在的路径。
        shutil.copytree(
            meta_dir,
            dist_info,
            ignore=shutil.ignore_patterns(
                "RECORD", "WHEEL", "INSTALLER", "REQUESTED", "direct_url.json"
            ),
        )
    else:
        dist_info.mkdir(parents=True, exist_ok=True)
        # egg-info 的 PKG-INFO 就是 PEP 566 METADATA，直接改名即可
        shutil.copyfile(meta_dir / "PKG-INFO", dist_info / "METADATA")
        entries = meta_dir / "entry_points.txt"
        if entries.exists():
            shutil.copyfile(entries, dist_info / "entry_points.txt")

    # PEP 639：METADATA 的 License-File 指向 dist-info/licenses/ 下的文件。许可文件属于
    # 再分发时该带的内容，不能为了让 pip 不告警就删掉条目；正常情况由 apk 的 -dev 子包
    # 补齐（py3-numpy-dev 就带着 libdivide 的 LICENSE.txt）。真缺了才剔除并告警。
    metadata = dist_info / "METADATA"
    if metadata.exists():
        lines = metadata.read_text(encoding="utf-8").splitlines()
        missing = [
            line.split(":", 1)[1].strip()
            for line in lines
            if line.startswith("License-File:")
            and not (dist_info / "licenses" / line.split(":", 1)[1].strip()).exists()
        ]
        if missing:
            print(f"警告: 缺少许可文件 {missing}，剔除对应 License-File 条目", file=sys.stderr, flush=True)
            kept = [
                line
                for line in lines
                if not (
                    line.startswith("License-File:")
                    and line.split(":", 1)[1].strip() in missing
                )
            ]
            metadata.write_text("\n".join(kept) + "\n", encoding="utf-8")

    # 这里的 plat_name 只是「打包机的平台」，进过 auditwheel 后会被改成
    # musllinux_1_2_<arch>，两头一致才有意义，所以照旧写 sysconfig 的值。
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\n"
        "Generator: onnxruntime-docker pack_wheel.py (apk repackage)\n"
        "Root-Is-Purelib: false\n"
        f"Tag: {tag}\n"
    )
    return dist_info


def from_apk(site_packages: Path, out_dir: Path, package: str, expect_version: str) -> int:
    """重打包路径：site-packages 里的 <package> -> 自包含 wheel。"""
    pkg = site_packages / package
    if not pkg.is_dir():
        return die(f"{pkg} 不存在（apk 没装 py3-{package}？）")

    meta_dir = find_metadata_dir(site_packages, package)
    version = metadata_version(metadata_file(meta_dir))
    if expect_version and version != expect_version:
        return die(
            f"apk 里的 {package} 是 {version}，但期望 {expect_version}"
            f"（版本参数与 ALPINE_VERSION 不匹配？）"
        )

    work = Path(tempfile.mkdtemp(prefix="ortapk-"))
    try:
        unroot = work / "tree"
        unroot.mkdir()
        print(f"从 {pkg} 复制 {package} {version}")
        copy_package(pkg, unroot / package)

        # 少数包会把要 bundle 的私有库放在 site-packages 的 <pkg>.libs/ 下
        # （auditwheel 自己产出的目录结构），apk 版一般没有，有就一起带走。
        libs = site_packages / f"{package}.libs"
        if libs.is_dir():
            print(f"复制 {libs.name}/")
            copy_package(libs, unroot / libs.name)

        # onnxruntime 特有：apk 把库装在 /usr/lib，capi 里全是软链，要解引用并按
        # SONAME 改名。numpy 这类包的 .so 是包内真文件，不动它 —— 进了 auditwheel
        # 自然会把 libopenblas / libgfortran 收进 numpy.libs/ 并改好 RPATH。
        if package == "onnxruntime":
            prepare_capi(unroot / package / "capi", [])

        write_dist_info(unroot, meta_dir, version, package)
        pack_tree(unroot, out_dir, package)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def emit_index(root: Path) -> int:
    """为 root 下的 wheel 生成 PEP 503 索引，写入 root/simple/。

    链接用相对路径 ``../../<wheel>#sha256=...``，所以索引与 wheel 的相对位置
    固定（simple/<name> -> 上两级），但整个目录可以被挂载到任意路径。
    重复执行会重建 simple/，不会残留上一次的项目目录。
    """
    wheels = sorted(root.glob("*.whl"))
    if not wheels:
        return die(f"{root} 下没有 .whl，无法生成索引")

    simple = root / "simple"
    if simple.exists():
        shutil.rmtree(simple)

    projects: dict[str, list[Path]] = {}
    for wheel in wheels:
        # wheel 文件名首段是 distribution（PEP 427 要求其中的 - 已经换成 _）
        projects.setdefault(normalize_name(wheel.name.split("-")[0]), []).append(wheel)

    for name, files in sorted(projects.items()):
        page = simple / name
        page.mkdir(parents=True)
        links = "\n".join(
            f'    <a href="../../{w.name}#sha256={hashlib.sha256(w.read_bytes()).hexdigest()}">{w.name}</a><br/>'
            for w in files
        )
        (page / "index.html").write_text(
            f'<!DOCTYPE html>\n<html>\n  <head><meta charset="utf-8"/><title>{name}</title></head>\n'
            f"  <body>\n{links}\n  </body>\n</html>\n",
            encoding="utf-8",
        )

    links = "\n".join(f'    <a href="{n}/">{n}</a><br/>' for n in sorted(projects))
    (simple / "index.html").write_text(
        "<!DOCTYPE html>\n<html>\n  <head><meta charset=\"utf-8\"/><title>Simple index</title></head>\n"
        f"  <body>\n{links}\n  </body>\n</html>\n",
        encoding="utf-8",
    )

    print(f"索引: {simple}（{len(projects)} 个项目 / {len(wheels)} 个 wheel）", flush=True)
    for name, files in sorted(projects.items()):
        print(f"  {name}: {len(files)}", flush=True)
    return 0


def run_compliance(out_dir: Path, deny: list[str], allow_unknown: bool = False) -> None:
    """收尾的合规层：deny 断言 + NOTICE / SOURCE-OFFER / SBOM / 许可证全文。

    本仓库的 wheel 是 Alpine py3-onnxruntime / py3-numpy 的重打包，随包的
    abseil / re2 / libprotobuf-lite / libopenblas / libgfortran 全部来自 apk，
    版本号由构建镜像的 apk 数据库反查得到，NOTICE 里能给出精确版本。
    """
    import compliance

    compliance.run(out_dir, deny=deny or None, allow_unknown=allow_unknown)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel-dir", help="含单个 setup.py 产物 wheel 的目录（源码路径）")
    ap.add_argument(
        "--from-apk",
        metavar="SITE_PACKAGES",
        help="apk 装好的 site-packages 目录（重打包路径），如 /usr/lib/python3.14/site-packages",
    )
    ap.add_argument(
        "--package",
        default="onnxruntime",
        help="要重打包的包名（--from-apk 用），如 onnxruntime / numpy",
    )
    ap.add_argument(
        "--emit-index",
        action="store_true",
        help="只为 --out-dir 里已有的 wheel 生成 PEP 503 索引（simple/），不打包",
    )
    ap.add_argument("--out-dir", required=True, help="wheel 输出目录；--emit-index 时为索引所在目录")
    ap.add_argument(
        "--compliance",
        action="store_true",
        help="配合 --emit-index：做 deny 断言并生成 NOTICE / SOURCE-OFFER / SBOM / 许可证全文",
    )
    ap.add_argument(
        "--deny-lib",
        action="append",
        default=[],
        metavar="GLOB",
        help="禁止随包分发的库（可重复）；默认用 compliance.DEFAULT_DENY",
    )
    ap.add_argument(
        "--allow-unknown",
        action="store_true",
        help="允许许可证无法识别的随包库（默认拦下，合规门禁里 unknown 等于未评估）",
    )
    ap.add_argument(
        "--lib-dir",
        default="",
        help="cmake 构建目录下的 onnxruntime/capi，作为依赖查找的补充路径（源码路径）",
    )
    ap.add_argument(
        "--expect-version",
        default="",
        help="断言产出的版本，防止 alpine 升级后与 ONNX_VERSION 不一致",
    )
    args = ap.parse_args()

    modes = [bool(args.wheel_dir), bool(args.from_apk), bool(args.emit_index)]
    if sum(modes) != 1:
        return die("--wheel-dir / --from-apk / --emit-index 必须且只能给一个")

    out_dir = Path(args.out_dir).resolve()
    if args.emit_index:
        if args.compliance:
            run_compliance(out_dir, args.deny_lib, args.allow_unknown)
        return emit_index(out_dir)
    if args.from_apk:
        return from_apk(Path(args.from_apk).resolve(), out_dir, args.package, args.expect_version)

    extra_dirs = [Path(args.lib_dir).resolve()] if args.lib_dir else []
    return from_source_wheel(Path(args.wheel_dir).resolve(), out_dir, extra_dirs)


if __name__ == "__main__":
    sys.exit(main())
