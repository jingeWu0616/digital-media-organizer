"""Small native location picker: on-demand search, or explicit coordinates."""
from urllib.parse import urlencode
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QCheckBox, QComboBox, QListWidget, QDoubleSpinBox,
    QDialogButtonBox, QTabWidget, QWidget, QMessageBox)
from location_service import coordinates, search_places, protect_key


from ui_common import AdaptiveDialog


class LocationDialog(AdaptiveDialog):
    def __init__(self, count, parent, video_count=0, picker_only=False):
        super().__init__(parent)
        self.host = parent
        self.picker_only=picker_only
        self.results = []
        self.searching = False
        self.setWindowTitle('选择地点' if picker_only else '添加定位')
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('选择地点名称' if picker_only else f'选中 {count-video_count} 张照片、{video_count} 个视频'))
        self.tabs = QTabWidget()
        search_page, manual_page = QWidget(), QWidget()
        self.tabs.addTab(search_page, '搜索地点')
        self.tabs.addTab(manual_page, '填写经纬度')
        layout.addWidget(self.tabs, 1)
        search_layout = QVBoxLayout(search_page)
        settings_button = QPushButton('地图设置')
        settings_button.clicked.connect(self.configure_map)
        search_layout.addWidget(settings_button)
        self.query, self.city = QLineEdit(), QLineEdit()
        self.query.setPlaceholderText('地点或地址')
        self.city.setPlaceholderText('城市（可选）')
        self.city.setText(str(parent.settings.value('amap_city', '')))
        self.city.setMaximumWidth(180)
        self.search_button = QPushButton('搜索')
        self.search_button.clicked.connect(self.search)
        self.query.returnPressed.connect(self.search)
        search_row = QHBoxLayout()
        for widget in (self.query, self.city, self.search_button): search_row.addWidget(widget)
        search_layout.addLayout(search_row)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.update_selection)
        search_layout.addWidget(self.list, 1)
        self.message = QLabel('')
        self.message.setWordWrap(True)
        search_layout.addWidget(self.message)
        self.map_button = QPushButton('在高德地图查看')
        self.map_button.setEnabled(False)
        self.map_button.clicked.connect(self.open_map)
        search_layout.addWidget(self.map_button)
        form = QFormLayout(manual_page)
        self.system = QComboBox()
        self.system.addItem('GPS / WGS84', 'WGS84')
        self.system.addItem('高德 / GCJ-02', 'GCJ02')
        self.latitude, self.longitude = QDoubleSpinBox(), QDoubleSpinBox()
        for spin, maximum in ((self.latitude, 90), (self.longitude, 180)):
            spin.setRange(-maximum, maximum); spin.setDecimals(7)
            spin.valueChanged.connect(self.update_selection)
        self.name = QLineEdit(); self.name.setPlaceholderText('可选')
        for label, widget in [('坐标系', self.system), ('纬度', self.latitude),
                              ('经度', self.longitude), ('地点名称', self.name)]:
            form.addRow(label, widget)
        self.system.currentIndexChanged.connect(self.update_selection)
        self.overwrite = QCheckBox('替换已有 GPS')
        self.overwrite.setToolTip('默认跳过已有 GPS 的素材。视频只标记单个拍摄位置。')
        self.overwrite.setVisible(not picker_only)
        layout.addWidget(self.overwrite)
        self.write_mode = QComboBox()
        self.write_mode.addItem('保护写入', 'protected')
        self.write_mode.addItem('快速写入', 'fast')
        mode_row = QWidget(); mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.addWidget(QLabel('视频 GPS 写入方式')); mode_layout.addWidget(self.write_mode, 1)
        mode_row.setVisible(bool(video_count) and not picker_only)
        layout.addWidget(mode_row)
        self.selection_label = QLabel('请选择地点')
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        self.confirm = QCheckBox('确认将此位置写入所选素材')
        self.confirm.toggled.connect(self.update_start)
        self.confirm.setVisible(not picker_only)
        layout.addWidget(self.confirm)
        self.write_notice = QLabel()
        self.write_notice.setWordWrap(True)
        self.write_notice.setVisible(bool(video_count) and not picker_only)
        layout.addWidget(self.write_notice)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.start = buttons.addButton('使用此地点' if picker_only else '写入定位', QDialogButtonBox.ButtonRole.AcceptRole)
        self.start.setEnabled(False)
        self.start.setObjectName('primary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.tabs.currentChanged.connect(self.update_selection)
        self.overwrite.toggled.connect(lambda: self.confirm.setChecked(False))
        self.write_mode.currentIndexChanged.connect(self.update_write_mode)
        self.update_write_mode()
        self.update_selection()

    def update_write_mode(self):
        fast = self.write_mode.currentData() == 'fast'
        self.confirm.setChecked(False)
        self.confirm.setText('确认写入此位置，已知快速写入中断可能损坏原视频' if fast
                             else '确认将此位置写入所选素材')
        self.write_notice.setText('直接修改视频；不支持的文件会跳过。照片仍使用保护写入。' if fast
                                 else '临时文件校验后替换原视频，不重新编码。')

    def configure_map(self):
        self.host.open_settings(0)
        if not self.city.text():
            self.city.setText(str(self.host.settings.value('amap_city', '')))

    def search(self):
        if self.searching: return
        self.results = []; self.list.clear(); self.update_selection()
        query, city = self.query.text().strip(), self.city.text().strip()
        try:
            key = protect_key(str(self.host.settings.value('amap_key_protected', '')), decrypt=True)
        except ValueError as exc:
            self.message.setText(str(exc)); return
        if not key:
            self.message.setText('请在地图设置中填写高德 Key。'); return
        if not query:
            self.message.setText('请输入地点或地址。'); return
        self.searching = True; self.search_button.setEnabled(False)
        self.message.setText('正在搜索…')
        def done(results):
            self.searching = False; self.search_button.setEnabled(True)
            self.results = results
            self.list.addItems([r['name'] + '\n' + r['address'] for r in results])
            self.message.setText('请选择具体地点' if results else '没有找到地点，请补充城市或更换关键词。')
        def failed(message):
            self.searching = False; self.search_button.setEnabled(True); self.message.setText(message)
        self.host.job(lambda _: search_places(key, query, city), done, failed=failed)

    def value(self):
        if self.tabs.currentIndex() == 0:
            index = self.list.currentRow()
            if not 0 <= index < len(self.results): return None
            row = self.results[index]
            lat, lon = coordinates(row['latitude'], row['longitude'], 'GCJ02')
            return dict(latitude=lat, longitude=lon, location_name=row['name'],
                        coordinate_source='高德 GCJ-02 转 WGS84（近似）')
        lat, lon = coordinates(self.latitude.value(), self.longitude.value(), self.system.currentData())
        return dict(latitude=lat, longitude=lon, location_name=self.name.text().strip(),
                    coordinate_source='手动 ' + self.system.currentText())

    def update_selection(self, *_):
        if not hasattr(self, 'confirm'): return
        if self.tabs.currentIndex()==1:
            self.tabs.setFixedHeight(self.tabs.widget(1).minimumSizeHint().height()+self.tabs.tabBar().sizeHint().height()+12)
        else:self.tabs.setFixedHeight(300)
        self.confirm.setChecked(False)
        value = self.value()
        self.map_button.setEnabled(self.tabs.currentIndex() == 0 and value is not None)
        self.selection_label.setText(f"{'坐标' if self.picker_only else '写入 GPS'}    纬度 {value['latitude']:.7f}    经度 {value['longitude']:.7f}"
                                    if value else '请选择地点')
        self.update_start()

    def update_start(self, *_):
        self.start.setEnabled((self.picker_only or self.confirm.isChecked()) and self.value() is not None)

    def open_map(self):
        index = self.list.currentRow()
        if 0 <= index < len(self.results):
            row = self.results[index]
            QDesktopServices.openUrl(QUrl('https://uri.amap.com/marker?' + urlencode(dict(
                position=f"{row['longitude']},{row['latitude']}", name=row['name'], coordinate='gaode', callnative=0))))

    def accept(self):
        if (self.picker_only or self.confirm.isChecked()) and self.value() is not None:
            super().accept()
