import os
import shutil
import threading
import time
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from pymediainfo import MediaInfo
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

# ================= 核心配置 =================
DATE_FORMAT = '%Y-%m-%d'
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.raw', '.arw', '.bmp', '.gif', '.tif', '.tiff'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.lrf', '.osv'}

# 星期映射表
WEEKDAY_MAP = {0: 'Mon', 1: 'Tue', 2: 'Wed', 3: 'Thu', 4: 'Fri', 5: 'Sat', 6: 'Sun'}

# 初始化地理编码器 (用于 GPS 转地址)
geolocator = Nominatim(user_agent="media_organizer_v3")

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")
# ============================================

# --- 元数据提取工具函数 ---

def calculate_age_string(birth_date_str, target_date_str, date_format='%Y-%m-%d'):
    """根据出生日期和目标日期计算精准年龄 (X岁X个月X天)"""
    if not birth_date_str:
        return ""
    try:
        birth_date = datetime.strptime(birth_date_str, date_format)
        target_date = datetime.strptime(target_date_str, date_format)
        
        if target_date < birth_date:
            return "未出生"
            
        years = target_date.year - birth_date.year
        months = target_date.month - birth_date.month
        days = target_date.day - birth_date.day
        
        if days < 0:
            months -= 1
            # 严格处理天数借位：退回到目标日期的上一个月的最后一天，获取该月总天数
            first_day_of_target_month = target_date.replace(day=1)
            last_day_of_prev_month = first_day_of_target_month - timedelta(days=1)
            days += last_day_of_prev_month.day
            
        if months < 0:
            years -= 1
            months += 12
            
        parts = []
        if years > 0: parts.append(f"{years}岁")
        if months > 0: parts.append(f"{months}个月")
        if days > 0: parts.append(f"{days}天")
        
        if not parts:
            return "出生当天"
            
        return "".join(parts)
    except Exception:
        # 异常兜底，防止因为某张图片的脏时间数据导致程序崩溃
        return ""

def get_gps_decimal(dms, ref):
    """将度分秒格式的 GPS 转换为十进制"""
    try:
        degrees = float(dms[0])
        minutes = float(dms[1])
        seconds = float(dms[2])
        decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
        if ref in ['S', 'W']:
            decimal = -decimal
        return decimal
    except Exception:
        return None

def get_location_name(lat, lon):
    """通过经纬度获取 城市_区县 名称"""
    try:
        # 增加一点延时，防止请求过快被 OpenStreetMap 封锁 IP
        time.sleep(1) 
        location = geolocator.reverse(f"{lat}, {lon}", exactly_one=True, timeout=5)
        if location:
            address = location.raw.get('address', {})
            city = address.get('city', address.get('town', address.get('village', '')))
            district = address.get('county', address.get('suburb', ''))
            
            # 清理空值并组合
            parts = [p for p in [city, district] if p]
            if parts:
                return "_".join(parts)
    except GeocoderTimedOut:
        return "位置超时"
    except Exception:
        pass
    return ""

def extract_media_info(file_path, ext):
    """
    统一提取媒体信息：返回 (日期字符串, 星期缩写, 设备型号, 位置信息, 星级评级)
    """
    date_obj = None
    device_name = ""
    location_name = ""
    rating = 0

    if ext in PHOTO_EXTS:
        try:
            with Image.open(file_path) as img:
                exif = img.getexif()
                if exif:
                    # 0. 深度获取照片星级评级 (兼容 Windows/Lightroom 与各种 EXIF 漂移)
                    search_ifds = [exif, exif.get_ifd(34665)]  # 联合搜索主 IFD 和 Exif 子 IFD
                    for ifd in search_ifds:
                        if not ifd or rating > 0: continue
                        
                        # 遍历 Rating (18246) 和 RatingPercent (18249, Windows底层专属)
                        for tag_id, is_pct in [(18246, False), (18249, True)]:
                            val = ifd.get(tag_id)
                            if val is not None:
                                try:
                                    # 极致数据清洗：剥离元组、解析各种奇怪的字节码
                                    if isinstance(val, (tuple, list)): 
                                        val = val[0]
                                    elif isinstance(val, bytes):
                                        try: 
                                            val = int(val.decode('utf-8').strip('\x00'))
                                        except: 
                                            val = int.from_bytes(val, 'little')
                                    
                                    v_int = int(val)
                                    if is_pct:
                                        # Windows 星级百分比折算协议
                                        if v_int >= 99: rating = 5
                                        elif v_int >= 75: rating = 4
                                        elif v_int >= 50: rating = 3
                                        elif v_int >= 25: rating = 2
                                        elif v_int >= 1: rating = 1
                                    else:
                                        # 标准 1-5 星
                                        if 1 <= v_int <= 5: rating = v_int
                                        
                                    if rating > 0: break  # 成功提取，直接跳出阻断循环
                                except Exception:
                                    pass
                        if rating > 0: break

                    # 1. 获取日期 (Tag 36867: DateTimeOriginal)
                    date_str = exif.get(36867)
                    if date_str:
                        date_obj = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
                    
                    # 2. 获取设备型号 (Tag 272: Model)
                    model = exif.get(272)
                    if model:
                        device_name = str(model).strip().replace(" ", "") # 去除空格，如 iPhone14Pro

                    # 3. 获取 GPS 位置 (Tag 34853: GPSInfo)
                    gps_info = exif.get_ifd(34853)
                    if gps_info and 2 in gps_info and 4 in gps_info:
                        lat_dms = gps_info[2]
                        lat_ref = gps_info[1]
                        lon_dms = gps_info[4]
                        lon_ref = gps_info[3]
                        
                        lat = get_gps_decimal(lat_dms, lat_ref)
                        lon = get_gps_decimal(lon_dms, lon_ref)
                        if lat is not None and lon is not None:
                            location_name = get_location_name(lat, lon)
        except Exception:
            pass

    elif ext in VIDEO_EXTS:
        # 1. 尝试获取视频日期 (使用 hachoir)
        try:
            parser = createParser(str(file_path))
            if parser:
                with parser:
                    metadata = extractMetadata(parser)
                    if metadata and metadata.has("creation_date"):
                        date_obj = metadata.get("creation_date")
        except Exception:
            pass
        
        # 2. 尝试获取视频设备 (使用 pymediainfo)
        try:
            media_info = MediaInfo.parse(str(file_path))
            for track in media_info.tracks:
                if track.track_type == "General":
                    track_dict = track.to_dict()
                    device_name = track_dict.get('com_apple_quicktime_model') or \
                                  track_dict.get('encoded_application') or \
                                  track_dict.get('model') or ""
                    if device_name:
                        device_name = str(device_name).strip().replace(" ", "")
                        break
        except Exception:
                pass

    # [终极兜底] 如果前面的标准 EXIF 库依然没拿到星级 (常见于 Windows 打标的 PNG、深层 XMP 或视频文件)
    # 启用核武器：以底层二进制流读取文件前后 64KB，硬搜底层 XML/XMP 字符串特征
    if rating == 0:
        try:
            file_size = os.path.getsize(file_path)
            with open(file_path, 'rb') as f:
                # 读取头部 64KB
                raw_chunk = f.read(65536)
                # 如果文件较大，把尾部 64KB 也读出来拼接 (有些相机会把元数据写在末尾)
                if file_size > 65536:
                    f.seek(max(0, file_size - 65536))
                    raw_chunk += f.read(65536)
                
                # 1. 匹配标准 Adobe XMP 星标特征 (如 <xmp:Rating>5</xmp:Rating> 或 xmp:Rating="5")
                match_xmp = re.search(rb'xmp:Rating=["\'>]?([1-5])', raw_chunk)
                if match_xmp:
                    rating = int(match_xmp.group(1))
                else:
                    # 2. 匹配微软原生独占星标特征 (99=5星, 75=4星, 50=3星, 25=2星, 1=1星)
                    match_ms = re.search(rb'MicrosoftPhoto:Rating=["\'>]?([0-9]{1,2})', raw_chunk)
                    if match_ms:
                        ms_val = int(match_ms.group(1))
                        if ms_val >= 99: rating = 5
                        elif ms_val >= 75: rating = 4
                        elif ms_val >= 50: rating = 3
                        elif ms_val >= 25: rating = 2
                        elif ms_val >= 1: rating = 1
        except Exception:
            pass

    # 兜底：如果没拿到时间，使用文件修改时间
    if not date_obj:
        timestamp = os.path.getmtime(file_path)
        date_obj = datetime.fromtimestamp(timestamp)

    date_str = date_obj.strftime(DATE_FORMAT)
    weekday_str = WEEKDAY_MAP[date_obj.weekday()]
    
    return date_str, weekday_str, device_name, location_name, rating


class ModernMediaOrganizer(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("数字资产管家 V3.0 - 全息元数据版")
        self.geometry("700x600")
        self.resizable(False, False)

        self.source_path = ""
        self.target_path = ""
        self.is_running = False
        self.pause_event = threading.Event()
        self.pause_event.set()

        self.setup_ui()

    def setup_ui(self):
        self.title_label = ctk.CTkLabel(self, text="Media Organizer", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.pack(pady=(20, 10))

        self.frame_paths = ctk.CTkFrame(self, corner_radius=10, fg_color="transparent")
        self.frame_paths.pack(fill="x", padx=40, pady=10)

        self.btn_source = ctk.CTkButton(self.frame_paths, text="📁 选择源文件夹", width=140, command=self.browse_source)
        self.btn_source.grid(row=0, column=0, padx=(0, 10), pady=10)
        self.lbl_source = ctk.CTkLabel(self.frame_paths, text="未选择...", text_color="gray", anchor="w", width=400)
        self.lbl_source.grid(row=0, column=1, sticky="w")

        self.btn_target = ctk.CTkButton(self.frame_paths, text="🎯 选择目标文件夹", width=140, command=self.browse_target)
        self.btn_target.grid(row=1, column=0, padx=(0, 10), pady=10)
        self.lbl_target = ctk.CTkLabel(self.frame_paths, text="未选择...", text_color="gray", anchor="w", width=400)
        self.lbl_target.grid(row=1, column=1, sticky="w")

        # 增加手动地点备注框
        self.lbl_location = ctk.CTkLabel(self.frame_paths, text="📍 统一地点备注:", width=120, anchor="e")
        self.lbl_location.grid(row=2, column=0, padx=(0, 10), pady=10)

        self.entry_location = ctk.CTkEntry(self.frame_paths, placeholder_text="如: 天津 (多地点可用 天津_北京)", width=400)
        self.entry_location.grid(row=2, column=1, sticky="w")

        # 增加宝宝生日输入框
        self.lbl_birthday = ctk.CTkLabel(self.frame_paths, text="👶 宝宝生日:", width=120, anchor="e")
        self.lbl_birthday.grid(row=3, column=0, padx=(0, 10), pady=10)

        self.entry_birthday = ctk.CTkEntry(self.frame_paths, placeholder_text="格式: YYYY-MM-DD (如 2025-03-21，不填则跳过)", width=400)
        self.entry_birthday.grid(row=3, column=1, sticky="w")

        # [需求1] 初始化并尝试加载本地配置文件
        self.config_file = "organizer_config.json"
        self._load_config()

        self.frame_controls = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_controls.pack(fill="x", padx=40, pady=15)

        self.start_btn = ctk.CTkButton(self.frame_controls, text="▶ 开始整理", font=ctk.CTkFont(weight="bold"), 
                                       fg_color="#2FA572", hover_color="#107C41", command=self.start_processing)
        self.start_btn.pack(side="left", padx=(0, 10))

        self.pause_btn = ctk.CTkButton(self.frame_controls, text="⏸ 暂停", fg_color="#E5A63B", hover_color="#C48827", 
                                       state="disabled", command=self.toggle_pause)
        self.pause_btn.pack(side="left")

        self.history_btn = ctk.CTkButton(self.frame_controls, text="历史素材索引",
                                        command=self.open_history_index)
        self.history_btn.pack(side="right")
        self.history_window = None

        self.progress = ctk.CTkProgressBar(self, orientation="horizontal", height=8, corner_radius=4)
        self.progress.pack(fill="x", padx=40, pady=(5, 10))
        self.progress.set(0)

        self.lbl_status = ctk.CTkLabel(self, text="准备就绪", text_color="gray", font=ctk.CTkFont(size=12))
        self.lbl_status.pack(pady=(0, 5))

        self.log_area = ctk.CTkTextbox(self, corner_radius=10, state="disabled", wrap="word")
        self.log_area.pack(fill="both", expand=True, padx=40, pady=(0, 30))

    def open_history_index(self):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            return
        from history_index import HistoryIndexWindow
        try:
            self.history_window = HistoryIndexWindow(self, can_scan=lambda: not self.is_running)
        except Exception as exc:
            messagebox.showerror("无法打开索引", str(exc), parent=self)

    def _load_config(self):
        """读取本地配置文件以恢复上次的输入状态"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    saved_birthday = config.get("baby_birthday", "")
                    if saved_birthday:
                        self.entry_birthday.insert(0, saved_birthday)
        except Exception:
            pass

    def _save_config(self, birthday_str):
        """将当前输入状态固化到本地文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump({"baby_birthday": birthday_str}, f, ensure_ascii=False)
        except Exception:
            pass

    def browse_source(self):
        path = filedialog.askdirectory(title="选择源文件夹")
        if path:
            self.source_path = path
            self.lbl_source.configure(text=path, text_color=("black", "white"))

    def browse_target(self):
        path = filedialog.askdirectory(title="选择目标文件夹")
        if path:
            self.target_path = path
            self.lbl_target.configure(text=path, text_color=("black", "white"))

    def log(self, message):
        """利用 after 实现线程安全的 UI 更新"""
        def update_log():
            self.log_area.configure(state="normal")
            self.log_area.insert("end", message + "\n")
            self.log_area.see("end")
            self.log_area.configure(state="disabled")
        self.after(0, update_log)

    def update_progress(self, current, total):
        """线程安全的进度条更新"""
        def update_ui():
            progress_val = current / total
            self.progress.set(progress_val)
            self.lbl_status.configure(text=f"整理进度: {current} / {total}  ({int(progress_val * 100)}%)")
        self.after(0, update_ui)

    def toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="▶ 恢复", fg_color="#2FA572", hover_color="#107C41")
            self.log("\n⏸️ 任务已挂起...")
            self.lbl_status.configure(text="已暂停")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="⏸ 暂停", fg_color="#E5A63B", hover_color="#C48827")
            self.log("\n▶️ 任务继续...")

    def start_processing(self):
        if (self.history_window is not None and self.history_window.winfo_exists()
                and self.history_window.scanning):
            messagebox.showinfo("任务进行中", "请等待历史索引完成或停止索引。", parent=self)
            return
        if not self.source_path or not self.target_path:
            messagebox.showwarning("提示", "请先完善文件夹路径！")
            return
        if self.is_running: return

        self.is_running = True
        self.pause_event.set() 
        self.start_btn.configure(state="disabled", text="处理中...")
        self.pause_btn.configure(state="normal")
        self.progress.set(0)
        
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.configure(state="disabled")

        threading.Thread(target=self.process_logic, daemon=True).start()

    def process_logic(self):
        try:
            # 补齐了丢失的文件扫描和路径初始化逻辑
            source_p = Path(self.source_path)
            target_p = Path(self.target_path)
            
            # 1. 获取你在界面上填写的地点备注与宝宝生日
            manual_location = self.entry_location.get().strip()
            baby_birthday = self.entry_birthday.get().strip()

            # [需求1] 将本次有效的生日配置写入本地，供下次打开时复用
            self._save_config(baby_birthday)

            # 基础校验生日格式，防止用户误填导致整体失败
            if baby_birthday:
                try:
                    datetime.strptime(baby_birthday, DATE_FORMAT)
                except ValueError:
                    self.log("⚠️ 警告: 宝宝生日格式错误，已自动忽略。请使用类似 2025-03-21 的格式。")
                    baby_birthday = ""

            self.log("🔍 正在扫描文件，这可能需要一些时间...")
            all_files = [f for f in source_p.rglob('*') if f.is_file() and f.suffix.lower() in (PHOTO_EXTS | VIDEO_EXTS)]
            total_files = len(all_files)
            
            if total_files == 0:
                self.log("📭 未发现媒体文件。")
                self.finish_processing()
                return

            self.log(f"📦 共计 {total_files} 个文件，开始深度元数据解析...\n")
            moved_count = 0

            for i, file_path in enumerate(all_files):
                self.pause_event.wait() 
                
                # [需求调整] 识别已整理过的文件。
                # 不再跳过处理，而是仅作为标记，用于后续保留原文件名，但依然会重新计算它的目标文件夹（如星级）
                is_already_named = bool(re.match(r'^\d{4}-\d{2}-\d{2}_[A-Za-z]{3}_', file_path.name))

                ext = file_path.suffix.lower()
                
                # 提取元数据 (包含照片自带的 GPS 城市与星级)
                date_str, weekday_str, device, auto_location, rating = extract_media_info(file_path, ext)
                
                # 2. 核心补位逻辑：如果有 GPS 就用 GPS，没 GPS 就用你手动填的备注
                final_file_location = auto_location if auto_location else manual_location
                
                # 3. 决定最终文件名
                if is_already_named:
                    # 如果已经整理过，直接沿用原有名称，避免出现 _1_1 的套娃现象
                    new_filename = file_path.name
                else:
                    # 如果是首次整理，按规范拼接新文件名
                    name_parts = [date_str, weekday_str]
                    if device: name_parts.append(device)
                    if final_file_location: name_parts.append(final_file_location) 
                    name_parts.append(file_path.stem)
                    new_filename = "_".join(name_parts) + ext
                
                # 4. 计算宝宝年龄并决定母文件夹默认名称
                age_str = calculate_age_string(baby_birthday, date_str, DATE_FORMAT)
                
                folder_parts = [date_str]
                if manual_location: folder_parts.append(manual_location)
                if age_str: folder_parts.append(age_str)
                folder_name = " ".join(folder_parts)
                
                # [需求2] 智能归档合并：扫描目标路径下是否已有同一天（同 date_str）的文件夹
                # 如果存在，则直接劫持/覆盖 folder_name，强制合流，避免同日产生多重文件夹
                if target_p.exists():
                    existing_dirs = [d for d in target_p.iterdir() if d.is_dir() and d.name.startswith(date_str)]
                    if existing_dirs:
                        folder_name = existing_dirs[0].name

                # 按具体日期(带地点与年龄)建文件夹，里面再分照片/视频
                media_type = "照片" if ext in PHOTO_EXTS else "视频"
                final_dir = target_p / folder_name / media_type
                
                # [新增] 按照照片星级建立子文件夹 (仅对成功解析到 1-5 星的照片生效)
                # 未分级或无相关元数据的文件将默认保留在 "照片" 或 "视频" 根目录下
                if rating > 0:
                    final_dir = final_dir / f"{rating}星"
                    
                final_dir.mkdir(parents=True, exist_ok=True)
                
                final_file_path = final_dir / new_filename
                
                # 增强防覆盖重命名逻辑 (已修复缩进)
                counter = 1
                while final_file_path.exists() and final_file_path != file_path:
                    final_file_path = final_dir / f"{'_'.join(name_parts)}_{counter}{ext}"
                    counter += 1
                    
                # 移动文件
                if file_path != final_file_path:
                    shutil.move(str(file_path), str(final_file_path))
                    moved_count += 1
                    self.log(f"✅ 成功 -> {final_file_path.name}")

                self.update_progress(i + 1, total_files)

            self.log(f"\n🎉 整理完成！成功移动: {moved_count} 个文件")
            self.after(0, lambda: messagebox.showinfo("完成", f"整理完成！\n成功移动: {moved_count} 个文件"))

        except Exception as e:
            self.log(f"\n❌ 发生错误: {str(e)}")
        finally:
            self.finish_processing()

    def finish_processing(self):
        def reset_ui():
            self.is_running = False
            self.start_btn.configure(state="normal", text="▶ 开始整理")
            self.pause_btn.configure(state="disabled", text="⏸ 暂停")
            self.lbl_status.configure(text="任务结束")
        self.after(0, reset_ui)

if __name__ == "__main__":
    # The desktop original remains untouched. This project entry launches v4.
    from organizer_app import main
    raise SystemExit(main())
