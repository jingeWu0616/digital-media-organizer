"""Optional historical inventory window; also runnable without the organizer UI."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from media_catalog import Catalog

STATUS = {'running': '扫描中', 'completed': '完成', 'completed_with_errors': '完成，有读取异常',
          'cancelled': '已停止', 'failed': '失败'}
GPS = {'present': '有 GPS', 'missing': '无 GPS', 'unknown': '未确认'}
DATE = {'metadata': '文件元数据', 'filename_inferred': '文件名推断', 'unknown': '未知'}


class HistoryIndexWindow(tk.Toplevel):
    def __init__(self, master, database=None, can_scan=None):
        super().__init__(master)
        self.title('历史素材索引')
        self.geometry('1080x680')
        self.minsize(880, 560)
        self.catalog = Catalog(database)
        self.catalog.initialize()
        self.can_scan = can_scan or (lambda: True)
        self.scanning = False
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.query_version = 0
        self.offset = 0
        self.total = 0
        self.rows = {}
        self.batch_ids = ['']
        self.closed = False
        self.pending_close = False
        self._build()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = self.after(100, self._poll)
        self.refresh()

    def _build(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        top = ttk.Frame(frame)
        top.pack(fill='x')
        self.folder = tk.StringVar()
        self.folder_entry = ttk.Entry(top, textvariable=self.folder)
        self.folder_entry.pack(side='left', fill='x', expand=True)
        self.choose_button = ttk.Button(top, text='选择文件夹', command=self.choose_folder)
        self.choose_button.pack(side='left', padx=(8, 0))
        self.scan_button = ttk.Button(top, text='开始索引', command=self.start_scan)
        self.scan_button.pack(side='left', padx=(8, 0))
        self.stop_button = ttk.Button(top, text='停止', command=self.stop_scan, state='disabled')
        self.stop_button.pack(side='left', padx=(8, 0))
        note = ttk.Frame(frame)
        note.pack(fill='x', pady=(8, 12))
        ttk.Label(note, text='只读扫描，不移动或修改素材。').pack(side='left')
        self.retry = tk.BooleanVar(value=False)
        self.retry_button = ttk.Checkbutton(note, text='重新读取元数据', variable=self.retry)
        self.retry_button.pack(side='right')

        filters = ttk.Frame(frame)
        filters.pack(fill='x', pady=(0, 8))
        ttk.Label(filters, text='文件名／路径／日期').pack(side='left')
        self.query = tk.StringVar()
        search = ttk.Entry(filters, textvariable=self.query, width=28)
        search.pack(side='left', padx=8, fill='x', expand=True)
        search.bind('<Return>', lambda _: self.refresh(reset=True))
        self.kind = ttk.Combobox(filters, values=['全部类型', '照片', '视频'], state='readonly', width=9)
        self.kind.current(0)
        self.kind.pack(side='left', padx=4)
        self.gps = ttk.Combobox(filters, values=['全部定位', '有 GPS', '无 GPS', '未确认'], state='readonly', width=10)
        self.gps.current(0)
        self.gps.pack(side='left', padx=4)
        ttk.Button(filters, text='查询', command=lambda: self.refresh(reset=True)).pack(side='left', padx=(4, 0))
        for box in (self.kind, self.gps):
            box.bind('<<ComboboxSelected>>', lambda _: self.refresh(reset=True))

        batch_bar = ttk.Frame(frame)
        batch_bar.pack(fill='x', pady=(0, 8))
        ttk.Label(batch_bar, text='历史扫描批次').pack(side='left')
        self.batch = ttk.Combobox(batch_bar, values=['全部批次'], state='readonly')
        self.batch.current(0)
        self.batch.pack(side='left', padx=8, fill='x', expand=True)
        self.batch.bind('<<ComboboxSelected>>', lambda _: self.refresh(reset=True))
        ttk.Button(batch_bar, text='查看异常', command=self.show_errors).pack(side='left')

        table = ttk.Frame(frame)
        table.pack(fill='both', expand=True)
        columns = ('name', 'inferred', 'type', 'date', 'date_source', 'gps', 'path')
        self.table = ttk.Treeview(table, columns=columns, show='headings', selectmode='browse')
        headings = ['现文件名', '推测原名', '类型', '拍摄时间', '时间依据', 'GPS', '所在文件夹']
        widths = [205, 150, 55, 150, 85, 70, 280]
        for name, title, width in zip(columns, headings, widths):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=50)
        vertical = ttk.Scrollbar(table, orient='vertical', command=self.table.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.table.bind('<Double-1>', lambda _: self.show_details())

        footer = ttk.Frame(frame)
        footer.pack(fill='x', pady=(12, 0))
        ttk.Button(footer, text='查看详情', command=self.show_details).pack(side='left')
        ttk.Button(footer, text='打开所在文件夹', command=self.open_folder).pack(side='left', padx=8)
        self.next_button = ttk.Button(footer, text='下一页', command=lambda: self.page(1))
        self.next_button.pack(side='right')
        self.prev_button = ttk.Button(footer, text='上一页', command=lambda: self.page(-1))
        self.prev_button.pack(side='right', padx=8)
        self.page_label = ttk.Label(footer)
        self.page_label.pack(side='right', padx=8)
        self.status = tk.StringVar(value='选择文件夹后开始索引')
        ttk.Label(frame, textvariable=self.status).pack(anchor='w', pady=(10, 0))

    def choose_folder(self):
        selected = filedialog.askdirectory(parent=self, title='选择历史素材文件夹', mustexist=True)
        if selected:
            self.folder.set(selected)

    def start_scan(self):
        if self.scanning:
            return
        if not self.can_scan():
            messagebox.showinfo('任务进行中', '请等待当前整理任务结束。', parent=self)
            return
        root = self.folder.get().strip()
        if not root:
            self.choose_folder()
            return
        self.scanning = True
        self.cancel_event.clear()
        self.scan_button.configure(state='disabled')
        self.choose_button.configure(state='disabled')
        self.folder_entry.configure(state='disabled')
        self.retry_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.status.set('正在扫描')
        retry = self.retry.get()

        def worker():
            try:
                result = self.catalog.scan(root, self.cancel_event,
                    lambda stats: self.events.put(('progress', stats)), retry, recursive=False)
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('scan_error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def stop_scan(self):
        self.cancel_event.set()
        self.status.set('正在停止，等待当前文件读取结束')
        self.stop_button.configure(state='disabled')

    def refresh(self, reset=False):
        if reset:
            self.offset = 0
        self.query_version += 1
        version = self.query_version
        selected_batch = self.batch_ids[max(0, self.batch.current())]
        args = dict(text=self.query.get().strip(), kind=['', 'photo', 'video'][self.kind.current()],
                    gps=['', 'present', 'missing', 'unknown'][self.gps.current()],
                    batch_id=selected_batch, offset=self.offset)

        def worker():
            try:
                result = self.catalog.search(**args)
                batches = self.catalog.batches()
                self.events.put(('query', (version, result, batches, selected_batch)))
            except Exception as exc:
                self.events.put(('query_error', (version, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                self.status.set(f"{STATUS[value['status']]}    照片 {value['photos']}    视频 {value['videos']}    复用 {value['unchanged']}    异常 {value['errors']}")
            elif kind in ('done', 'scan_error'):
                self.scanning = False
                for widget in (self.scan_button, self.choose_button, self.folder_entry, self.retry_button):
                    widget.configure(state='normal')
                self.stop_button.configure(state='disabled')
                if kind == 'scan_error':
                    self.status.set('索引失败')
                    messagebox.showerror('索引失败', value, parent=self)
                self.refresh()
                if self.pending_close:
                    self.close()
                    return
            elif kind == 'query':
                version, (total, rows), batches, selected = value
                if version != self.query_version:
                    continue
                self.total = total
                self.rows = {r['id']: r for r in rows}
                self.table.delete(*self.table.get_children())
                for row in rows:
                    self.table.insert('', 'end', iid=row['id'], values=(
                        row['observed_name'], row['inferred_original'] or '',
                        '照片' if row['kind'] == 'photo' else '视频', row['capture_time'] or '',
                        DATE[row['date_source']], GPS[row['gps_status']], str(Path(row['current_path']).parent)))
                self.batch_ids = [''] + [b['id'] for b in batches]
                labels = ['全部批次'] + [f"{b['started_at']}    照片 {b['photos']}    视频 {b['videos']}    {STATUS.get(b['status'], b['status'])}" for b in batches]
                self.batch.configure(values=labels)
                self.batch.current(self.batch_ids.index(selected) if selected in self.batch_ids else 0)
                self.page_label.configure(text=f'{self.offset + 1 if total else 0}–{self.offset + len(rows)} / {total}')
                self.prev_button.configure(state='normal' if self.offset else 'disabled')
                self.next_button.configure(state='normal' if self.offset + len(rows) < total else 'disabled')
            elif kind == 'query_error' and value[0] == self.query_version:
                self.status.set('查询失败  ' + value[1])
        self.poll_id = self.after(100, self._poll)

    def page(self, direction):
        self.offset = max(0, self.offset + 200 * direction)
        self.refresh()

    def selected(self):
        ids = self.table.selection()
        return self.rows.get(ids[0]) if ids else None

    def show_text(self, title, content):
        window = tk.Toplevel(self)
        window.title(title)
        window.geometry('720x420')
        text = tk.Text(window, wrap='word', padx=12, pady=12)
        text.pack(fill='both', expand=True)
        text.insert('1.0', content)
        text.configure(state='disabled')

    def show_details(self):
        row = self.selected()
        if not row:
            return
        items = [
            ('现文件名', row['observed_name']), ('已确认原名', row['original_name'] or '未知'),
            ('推测原名', row['inferred_original'] or '未知'), ('现路径', row['current_path']),
            ('拍摄时间', row['capture_time'] or '未知'), ('时间依据', DATE[row['date_source']]),
            ('设备', row['device'] or '未知'), ('GPS', GPS[row['gps_status']]),
            ('纬度', row['latitude']), ('经度', row['longitude']), ('视频秒数', row['duration_seconds']),
            ('文件字节数', row['size']), ('首次建立索引', row['first_indexed_at']),
            ('最近扫描到', row['last_seen_at']),
            ('可用状态', '最近扫描到' if row['availability'] == 'present' else '最近完整扫描未找到'),
            ('读取异常', row['metadata_error'] or '无'),
        ]
        self.show_text('素材详情', '\n\n'.join(f'{label}\n{value if value is not None else "未知"}' for label, value in items))

    def open_folder(self):
        row = self.selected()
        if row:
            try:
                os.startfile(str(Path(row['current_path']).parent))
            except OSError as exc:
                messagebox.showerror('无法打开文件夹', str(exc), parent=self)

    def show_errors(self):
        batch_id = self.batch_ids[max(0, self.batch.current())]
        if not batch_id:
            messagebox.showinfo('选择批次', '请先选择一个历史扫描批次。', parent=self)
            return
        rows = self.catalog.errors(batch_id)
        self.show_text('读取异常（最多 200 条）', '\n\n'.join(r['path'] + '\n' + r['message'] for r in rows) or '无读取异常')

    def close(self):
        if self.scanning:
            self.pending_close = True
            self.stop_scan()
            return
        self.closed = True
        self.after_cancel(self.poll_id)
        self.destroy()


def main():
    root = tk.Tk()
    root.withdraw()
    try:
        window = HistoryIndexWindow(root)
        window.bind('<Destroy>', lambda event: root.destroy() if event.widget is window else None)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror('无法打开索引', str(exc), parent=root)
        root.destroy()


if __name__ == '__main__':
    main()
