# 数字媒体整理工具

一个 Windows 本地桌面工具，用于整理照片和视频。项目源于整理自己的素材：扫描多个文件夹，筛选、预览后归档，也可以为照片添加水印、为支持的素材添加定位，并查看处理记录。

针对符合条件的 **DJI Osmo Action 6 D-Log M MP4**，工具还支持使用 DJI 官方 LUT 批量还原至 Rec.709。请自行确认素材类型，程序不能单凭文件名可靠识别 D-Log M。

当前源码版本：**4.0.17**。目前提供源码运行方式；本仓库暂不提供 Windows 免安装包。

## 使用前请先看

- **请先备份原照片和视频，并用素材副本试用。** 数据库备份仅包含记录，不是媒体文件备份。
- 颜色还原需要重新编码，默认选择**覆盖原文件**且不保留原片备份；也可在执行前选择「另存还原版」。请核对每次的保存方式。
- 部分 DJI 专有轨道需要用户明确同意移除才可处理，覆盖后可能影响后期防抖。重新编码不能保证画质、文件大小及所有元数据与原片完全一致。
- 视频 GPS 的快速写入会修改原文件；意外断电、磁盘故障或网络断连仍可能造成损坏。处理网络盘素材时请特别注意备份。
- 高德地点搜索需要用户自己的 Key 和网络；输入经纬度、照片水印不需要高德服务。图片不上传高德。

## 从源码运行

需要 Windows x64 与 Python 3.12 或更高版本。下列命令在项目目录中的 PowerShell 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe organizer_app.py
```

也可以在安装依赖后双击 `启动数字媒体整理工具.cmd`。在操作真实文件之前，请先准备好下述 ExifTool，并按需要准备 DJI LUT。

### 准备 ExifTool

定位和照片水印等功能依赖 ExifTool。本仓库不附带 ExifTool 便携二进制包。请从 [ExifTool for Windows 发布页](https://oliverbetz.de/pages/Artikel/ExifTool-for-Windows) 自行取得 64 位 **13.61** 便携包（[来源记录与校验值](tools/exiftool/SOURCE.json)），解压后保留程序随附的完整目录、许可证及说明，放置为：

```text
tools/exiftool/exiftool.exe
tools/exiftool/exiftool_files/...
```

如果压缩包中的可执行文件名称不同，需将其改名为 `exiftool.exe`，并保留与之配套的 `exiftool_files` 目录。缺少它时，依赖 ExifTool 的功能无法正常运行。

### 准备 Action 6 官方 LUT

本仓库不附带 DJI LUT；如需颜色还原，请自行从 [DJI 官方下载页](https://www.dji.com/downloads/softwares/dji-osmo-action-6-d-log-m-to-rec-709-lut) 获取 Action 6 D-Log M to Rec.709 LUT，放到 `assets/luts/action6_dlogm_rec709.cube`。程序会检查文件内容的 SHA-256；目前版本要求：

```text
1972eb9b05699ba19d4aaa67aaa5fb00b46935c60ba9219183289f79e044cbb2
```

PowerShell 可用 `Get-FileHash -Algorithm SHA256 assets/luts/action6_dlogm_rec709.cube` 检查。若 DJI 更新了文件导致摘要不同，本版本不会接受；不要跳过校验。缺失 LUT 时颜色还原及相关测试不能通过。来源记录见 [SOURCE.txt](assets/luts/SOURCE.txt)。

## 功能概览

- 多文件夹扫描、格式筛选，预览后按规则整理照片和视频。
- Action 6 D-Log M MP4 的 Rec.709 颜色还原。
- 照片水印、照片及支持的视频定位、素材查询和处理记录。
- 支持选择本地路径或可访问的网络共享路径；实际速度和兼容性因环境而异。

## 测试与反馈

测试使用合成素材和临时数据库，不应对真实媒体或正式记录库运行写入测试。部分测试需要上述 ExifTool 和匹配的 LUT；不同设备及编码器环境会影响结果。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

欢迎在仓库的 **Issues** 提出需求、报告问题或分享使用体验。描述系统版本、软件版本、操作步骤、预期和实际结果会有助于排查。请先删除截图、日志和样片中的个人信息、文件路径、GPS 坐标、Key 及其他隐私，不要提交原始私人素材。

## 第三方资源

示例照片、缩略图的作者与许可见 [素材来源](assets/watermark_examples/SOURCES.md)；坐标转换代码的许可见 [eviltransform 许可](assets/licenses/eviltransform.txt)。其他依赖按各自许可使用。DJI LUT 与 ExifTool 由使用者自行取得并遵循其各自条款。

项目自身目前尚未声明开源许可证；第三方资源不因此改变其原有许可。
