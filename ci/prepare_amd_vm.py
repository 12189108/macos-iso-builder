#!/usr/bin/env python3
"""Prepare the released OC4VM AMD template on the GitHub-hosted runner."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import urllib.request
import zipfile

VERSION = "3.0.1"
ARCHIVE_SHA256 = "143b947e6d538a8b635a9c919e611705b03356964a7cfcbbe96e923e54dc1dbb"
RELEASE_URL = f"https://github.com/DrDonk/OC4VM/releases/download/{VERSION}"
bundle = Path(os.environ["BUNDLE_DIR"])
bundle.mkdir(parents=True, exist_ok=True)
scratch = Path(os.environ["RUNNER_TEMP"]) / "oc4vm-release"
scratch.mkdir(parents=True, exist_ok=True)


def digest(path, algorithm="sha256"):
    result = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def download(name):
    target = scratch / name
    print(f"Downloading official OC4VM release: {name}", flush=True)
    request = urllib.request.Request(f"{RELEASE_URL}/{name}", headers={"User-Agent": "macOS-AMD-VMware-builder"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle, 1024 * 1024)
    return target


archive_path = download(f"oc4vm-{VERSION}.zip")
checksum_path = download(f"oc4vm-{VERSION}.sha512")
if digest(archive_path) != ARCHIVE_SHA256:
    raise RuntimeError("OC4VM ZIP does not match the pinned GitHub release SHA-256")
expected_sha512 = checksum_path.read_text().split()[0]
if digest(archive_path, "sha512") != expected_sha512:
    raise RuntimeError("OC4VM ZIP does not match its published SHA-512")

with zipfile.ZipFile(archive_path) as archive:
    candidates = [name for name in archive.namelist()
                  if name.endswith("vmware/amd/macos.vmx")]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one released AMD VM template; found {candidates}")
    prefix = candidates[0].rsplit("/", 1)[0] + "/"
    for name in archive.namelist():
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        relative = PurePosixPath(name[len(prefix):])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe archive entry: {name}")
        destination = bundle.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    licenses = [name for name in archive.namelist()
                if PurePosixPath(name).name == "LICENSE" and "/vmware/" not in name]
    if licenses:
        (bundle / "LICENSE.OC4VM").write_bytes(archive.read(licenses[0]))

required = ("macos.vmx", "macos.vmdk", "macos.nvram", "opencore.iso", "opencore.vmdk")
for name in required:
    if not (bundle / name).is_file():
        raise RuntimeError(f"Missing file in released AMD template: {name}")

installer_type = os.environ["INSTALLER_TYPE"]
if installer_type not in ("recovery", "full"):
    raise RuntimeError("Unsupported installer type")
installer_name = "macOS_Tahoe_Recovery.iso" if installer_type == "recovery" else "macOS_Tahoe_Installer.iso"
vmx_path = bundle / "macos.vmx"
vmx = vmx_path.read_text(encoding="utf-8")


def set_setting(key, value):
    global vmx
    pattern = re.compile(r"^" + re.escape(key) + r"\s*=.*$", re.MULTILINE)
    line = f'{key} = "{value}"'
    if pattern.search(vmx):
        vmx = pattern.sub(lambda match: line, vmx)
    else:
        vmx += "\n" + line + "\n"


for key, value in {
    "displayName": "macOS Tahoe 26 AMD - OC4VM 3.0.1",
    "memsize": "8192",
    "numvcpus": "4",
    "sata0:1.deviceType": "cdrom-image",
    "sata0:1.fileName": installer_name,
    "sata0:1.autodetect": "FALSE",
    "sata0:1.present": "TRUE",
    "sata0:1.startConnected": "TRUE",
}.items():
    set_setting(key, value)
vmx_path.write_text(vmx, encoding="utf-8")

shutil.copyfile("LICENSE", bundle / "LICENSE.macos-iso-builder")
(bundle / "SOURCE-INFO.json").write_text(json.dumps({
    "builder_repository": os.environ["GITHUB_REPOSITORY"],
    "builder_commit": os.environ["GITHUB_SHA"],
    "workflow_run": f'https://github.com/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{os.environ["GITHUB_RUN_ID"]}',
    "upstream_builder": "https://github.com/LongQT-sea/macos-iso-builder",
    "oc4vm_release": f"https://github.com/DrDonk/OC4VM/releases/tag/{VERSION}",
    "oc4vm_zip_sha256": ARCHIVE_SHA256,
    "macrecovery_commit": os.environ["MACRECOVERY_COMMIT"],
    "installer_type": installer_type,
    "installer_file": installer_name,
    "target_cpu": "AMD x86_64; intended host Ryzen 7 8845H",
    "vcpus": 4,
    "memory_mb": 8192,
}, indent=2) + "\n", encoding="utf-8")

instructions = """macOS Tahoe 26 / AMD / VMware Workstation

使用方式
1. 完整解压这个 ZIP 到一个固定目录，保留所有文件。
2. 在 VMware Workstation 中打开 macos.vmx；询问移动或复制时选择“我已复制”。
3. 模板已经连接 AMD OpenCore 引导盘和 macOS 安装 ISO，无需再次创建虚拟机。
4. 开机后在 OpenCore 中选择 macOS Base System / Recovery / Install macOS Tahoe。
   如果安装入口隐藏，按空格显示辅助启动项。
5. 恢复安装包需要联网从 Apple 下载系统，请保持 NAT 网络连接。
   完整安装包包含离线安装介质。
6. 选择模板中的 APFS 系统盘安装。若需抹盘，只操作虚拟机系统盘：
   磁盘工具 -> 显示所有设备 -> 系统虚拟硬盘 -> GUID 分区图 / APFS。
7. 安装会多次重启；继续经 OpenCore 启动 macOS Installer，之后启动已安装的系统。

默认配置：4 个 vCPU、8 GB 内存、UEFI、e1000e NAT。
系统盘来自 OC4VM 官方模板，是动态增长的稀疏虚拟磁盘。
不要删除 opencore.iso 或 opencore.vmdk；安装后它们仍用于 AMD 启动。
这个 ZIP 里有安装 ISO 和单独的 OpenCore 引导盘，它们不是二选一。
安装完成后，可从引导卷 /Volumes/OPENCORE/OC4VM/iso/darwin.iso 安装 VMware Tools。
恢复介质版本和文件校验值分别见 BUILD-REPORT.md、SHA256SUMS.txt。

Build runs entirely on GitHub Actions. No local VMware installation is inspected.
Official OC4VM AMD template plus an Apple installer built with macos-iso-builder.
Validation checks media layout and boot files; it does not claim a completed
VMware/macOS installation or hardware graphics acceleration.
"""
(bundle / "README.txt").write_text(instructions, encoding="utf-8-sig")
print(f"AMD template ready: {bundle}; installer attachment: {installer_name}", flush=True)
