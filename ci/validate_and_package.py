#!/usr/bin/env python3
"""Validate generated media using macOS tools, then create a portable VM ZIP."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import zipfile

bundle = Path(os.environ["BUNDLE_DIR"]).resolve()
info = json.loads((bundle / "SOURCE-INFO.json").read_text())
installer = bundle / info["installer_file"]
if not installer.is_file() or installer.stat().st_size < 100 * 1024 * 1024:
    raise RuntimeError("Missing or unexpectedly small macOS installation ISO")

settings = {}
pattern = re.compile(r'^([^#=\s]+)\s*=\s*(?:"([^"]*)"|(TRUE|FALSE|[0-9]+))', re.IGNORECASE)
for line in (bundle / "macos.vmx").read_text().splitlines():
    match = pattern.match(line.strip())
    if match:
        key, quoted, unquoted = match.groups()
        if key in settings:
            raise RuntimeError(f"Duplicate VMX setting: {key}")
        settings[key] = quoted if quoted is not None else unquoted

expected = {
    "firmware": "efi",
    "smc.present": "FALSE",
    "featMask.vm.cpuid.AMD": "Min:1",
    "usb_xhci:0.fileName": "opencore.vmdk",
    "usb_xhci:0.present": "TRUE",
    "ethernet0.virtualDev": "e1000e",
    "ethernet0.connectionType": "nat",
    "sata0:1.fileName": info["installer_file"],
    "sata0:1.deviceType": "cdrom-image",
    "sata0:1.startConnected": "TRUE",
}
for key, value in expected.items():
    if settings.get(key) != value:
        raise RuntimeError(f"Unexpected VMX setting {key}: {settings.get(key)!r}")
for key in ("sata0:0.fileName", "sata0:1.fileName", "usb_xhci:0.fileName", "nvram"):
    value = Path(settings[key])
    if value.is_absolute() or ".." in value.parts or not (bundle / value).is_file():
        raise RuntimeError(f"Nonportable or missing VM attachment: {key}")

descriptor = (bundle / "opencore.vmdk").read_text()
extent = re.search(r'^RW ([0-9]+) FLAT "([^"]+)" ([0-9]+)', descriptor, re.MULTILINE)
if not extent or extent.group(2) != "opencore.iso" or int(extent.group(3)) != 0:
    raise RuntimeError("Invalid OpenCore VMDK extent mapping")
if int(extent.group(1)) * 512 != (bundle / "opencore.iso").stat().st_size:
    raise RuntimeError("OpenCore ISO size does not match its VMDK descriptor")


@contextmanager
def mount_readonly(image):
    with tempfile.TemporaryDirectory(prefix="verify-media-", dir=os.environ["RUNNER_TEMP"]) as directory:
        mountpoint = Path(directory)
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse",
                        "-mountpoint", str(mountpoint), str(image)], check=True)
        try:
            yield mountpoint
        finally:
            subprocess.run(["hdiutil", "detach", str(mountpoint)], check=True)


with mount_readonly(installer) as root:
    boot = root / "System/Library/CoreServices/boot.efi"
    if not boot.is_file():
        raise RuntimeError("Installation ISO does not contain the macOS EFI boot loader")
    version_file = root / "System/Library/CoreServices/SystemVersion.plist"
    with version_file.open("rb") as handle:
        system_version = plistlib.load(handle)
    product_version = system_version["ProductVersion"]
    if product_version.split(".")[0] != "26":
        raise RuntimeError(f"Expected Intel-compatible Tahoe 26; downloaded {product_version}")
    if info["installer_type"] == "full":
        installer_apps = list(root.glob("Install macOS*.app"))
        if not installer_apps:
            raise RuntimeError("Full installation ISO does not contain the installer app")

with mount_readonly(bundle / "opencore.iso") as root:
    for name in ("EFI/BOOT/BOOTx64.efi", "EFI/OC/OpenCore.efi", "EFI/OC/config.plist"):
        if not (root / name).is_file():
            raise RuntimeError(f"OpenCore boot file missing: {name}")
    with (root / "EFI/OC/config.plist").open("rb") as handle:
        oc_config = plistlib.load(handle)
    patches = [patch for patch in oc_config["Kernel"]["Patch"] if patch.get("Enabled")]
    if not patches:
        raise RuntimeError("AMD OpenCore config has no enabled kernel patches")
    amd_patch_count = len(patches)

validation = {
    "media_product_version": product_version,
    "media_build": system_version.get("ProductBuildVersion"),
    "installer_iso_bytes": installer.stat().st_size,
    "amd_opencore_enabled_kernel_patches": amd_patch_count,
    "portable_vmx_attachments_checked": True,
    "macos_efi_loader_present": True,
    "opencore_efi_loader_present": True,
    "opencore_vmdk_extent_checked": True,
    "vmware_guest_boot_tested": False,
}
(bundle / "VALIDATION.json").write_text(json.dumps(validation, indent=2) + "\n")
report = f"""- Media version: macOS {product_version} ({validation['media_build']})
- Installer type: {info['installer_type']}
- Installer ISO: {installer.name}
- OC4VM release: 3.0.1, official AMD template; SHA-256 and SHA-512 verified
- VM: 4 vCPUs, 8 GB RAM, UEFI, e1000e NAT
- Verified: Apple/macOS EFI loader, OpenCore EFI loader and config, portable VMX paths, VMDK extent
- VMware guest boot: not tested on the GitHub-hosted runner
- Recovery media requires Internet during installation.
"""
(bundle / "BUILD-REPORT.md").write_text(report)
print(report, flush=True)

checksums = []
files = sorted(path for path in bundle.rglob("*") if path.is_file())
for path in files:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    checksums.append(f"{digest.hexdigest()}  {path.relative_to(bundle).as_posix()}")
(bundle / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n")

output = bundle.parent / "macOS-Tahoe-AMD-VMware.zip"
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                     compresslevel=1, allowZip64=True) as archive:
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            archive.write(path, arcname=(Path(bundle.name) / path.relative_to(bundle)).as_posix())
print(f"Ready: {output.name} ({output.stat().st_size:,} bytes)", flush=True)
