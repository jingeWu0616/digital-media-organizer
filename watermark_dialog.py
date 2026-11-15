"""One-photo native watermark preview with coalesced background rendering."""
import hashlib
import io
import json
from pathlib import Path
from PIL import Image, ImageOps

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QVBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QComboBox, QCheckBox, QPlainTextEdit, QDoubleSpinBox,
    QSpinBox, QDialogButtonBox, QFileDialog, QWidget, QScrollArea, QSizePolicy,
    QTabWidget, QGridLayout, QToolButton, QButtonGroup, QFrame, QLayout)

from watermark_service import DEFAULTS, FONTS, STYLES, normalize, load_logo, read_photo, caption, render
from watermark_presets import ASSETS, LOOKS, example_rows, look_options


from ui_common import fit_window


class WatermarkDialog(QDialog):
    def __init__(self, rows, parent, template_mode=False):
        super().__init__(parent)
        self.template_mode = template_mode
        if template_mode:
            rows = example_rows()
        self.host, self.rows = parent, rows
        self.loading = True
        self.revision = 0
        self.preview_running = False
        self.closed = False
        self.cache = None
        self.pixmap = None
        self.preview_valid = False
        self.setWindowTitle('水印模板' if template_mode else '照片水印')
        self.resize(1080,740)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12,10,12,10)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        top = QHBoxLayout()
        top.addWidget(QLabel('模板示例预览' if template_mode else f'{len(rows)} 张照片'))
        top.addStretch()
        if not template_mode: top.addWidget(QLabel('另存 JPEG · 保留原片'))
        layout.addLayout(top)
        body = QHBoxLayout(); layout.addLayout(body,1)
        sidebar=QWidget(); sidebar.setMinimumWidth(320);sidebar.setMaximumWidth(352)
        side_layout=QVBoxLayout(sidebar);side_layout.setContentsMargins(0,0,0,0)
        self.editor_tabs=QTabWidget();side_layout.addWidget(self.editor_tabs,1);body.addWidget(sidebar)
        def page(label):
            scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame)
            content=QWidget();form=QVBoxLayout(content);form.setContentsMargins(4,8,4,8);form.setSpacing(8)
            scroll.setWidget(content);content.setAutoFillBackground(False)
            self.editor_tabs.addTab(scroll,label)
            return form
        templates=page('模板')
        cards=QGridLayout();cards.setSpacing(8);templates.addLayout(cards)
        self.look_buttons={};self.look_group=QButtonGroup(self);self.look_group.setExclusive(True)
        for index,(key,look) in enumerate(LOOKS.items()):
            button=QToolButton();button.setObjectName('watermarkLook');button.setText(look['name'])
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIcon(QIcon(str(ASSETS/(key+'_card.jpg'))));button.setIconSize(QSize(136,78))
            button.setCheckable(True);button.setMinimumHeight(108)
            button.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
            button.setToolTip(look['name']);button.clicked.connect(lambda checked=False,key=key:self.apply_look(key))
            cards.addWidget(button,index//2,index%2);self.look_buttons[key]=button;self.look_group.addButton(button)
        self.presets = self.read_settings('watermark_presets', {})
        if not isinstance(self.presets,dict): self.presets = {}
        self.preset = QComboBox(); self.preset.addItem('我的模板')
        self.preset.addItems(sorted(self.presets))
        self.preset.currentTextChanged.connect(self.apply_preset)
        templates.addWidget(self.preset);templates.addStretch()
        content_layout=page('内容')
        form=QFormLayout();form.setVerticalSpacing(10);form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        content_layout.addLayout(form)
        self.title=QLineEdit();self.title.setPlaceholderText('可选，例如 海边的一天');self.title.setMaxLength(60)
        self.signature=QLineEdit();self.signature.setPlaceholderText('可选，例如 摄影：小王');self.signature.setMaxLength(60)
        for label,field in [('标题',self.title),('署名',self.signature)]:
            field.textChanged.connect(self.schedule_preview);form.addRow(label,field)
        self.style = QComboBox()
        for value,label in STYLES.items():self.style.addItem(label,value)
        self.style.currentIndexChanged.connect(self.style_changed)
        self.style.hide()
        self.frame_note = QLabel('边框会增加成品尺寸');self.frame_note.setObjectName('secondary')
        fields = QWidget(); field_layout = QVBoxLayout(fields);field_layout.setContentsMargins(0,0,0,0)
        self.fields = {}
        for value,label in [('date','拍摄日期'),('camera','相机型号'),('location','地点名称'),('parameters','焦距 / 光圈 / 快门 / ISO')]:
            box=QCheckBox(label);box.toggled.connect(self.schedule_preview);self.fields[value]=box;field_layout.addWidget(box)
        form.addRow('自动内容',fields)
        self.custom = QPlainTextEdit();self.custom.setPlaceholderText('其他想写在照片上的内容');self.custom.setFixedHeight(68)
        self.custom.textChanged.connect(self.schedule_preview);form.addRow('自定义文字',self.custom)
        self.logo = QLineEdit();self.logo.setReadOnly(True);self.logo.setPlaceholderText('PNG / JPG')
        logo_row=QHBoxLayout();logo_row.addWidget(self.logo,1)
        upload=QPushButton('上传');upload.clicked.connect(self.choose_logo);logo_row.addWidget(upload)
        clear=QPushButton('移除');clear.clicked.connect(lambda:self.set_logo(''));logo_row.addWidget(clear)
        self.logo.textChanged.connect(self.schedule_preview);form.addRow('Logo',logo_row)
        self.advanced_toggle=QPushButton('展开高级调整');self.advanced_toggle.setCheckable(True)
        content_layout.addWidget(self.advanced_toggle)
        self.advanced=QWidget();advanced_form=QFormLayout(self.advanced);advanced_form.setContentsMargins(0,0,0,0)
        advanced_form.setVerticalSpacing(10);advanced_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        content_layout.addWidget(self.advanced);self.advanced.hide()
        def toggle_advanced(checked):
            self.advanced.setVisible(checked);self.advanced_toggle.setText('收起高级调整' if checked else '展开高级调整')
        self.advanced_toggle.toggled.connect(toggle_advanced)
        self.position=QComboBox()
        for value,label in [('bottom-right','右下'),('bottom-left','左下'),('top-right','右上'),('top-left','左上')]:self.position.addItem(label,value)
        self.position.currentIndexChanged.connect(self.schedule_preview);advanced_form.addRow('位置',self.position)
        self.theme=QComboBox();self.theme.addItem('深色底 / 白字','dark');self.theme.addItem('浅色底 / 黑字','light')
        self.theme.currentIndexChanged.connect(self.schedule_preview);advanced_form.addRow('配色',self.theme)
        self.font=QComboBox();self.font.addItems(FONTS);self.font.currentIndexChanged.connect(self.schedule_preview);advanced_form.addRow('字体',self.font)
        self.size=QDoubleSpinBox();self.size.setRange(1,6);self.size.setSingleStep(.5);self.size.setSuffix(' %')
        self.opacity=QSpinBox();self.opacity.setRange(10,100);self.opacity.setSuffix(' %')
        self.logo_size=QSpinBox();self.logo_size.setRange(3,25);self.logo_size.setSuffix(' %')
        self.quality=QSpinBox();self.quality.setRange(80,100)
        for label,widget in [('字号比例',self.size),('不透明度',self.opacity),('Logo 比例',self.logo_size),('JPEG 质量',self.quality)]:
            widget.valueChanged.connect(self.schedule_preview);advanced_form.addRow(label,widget)
        content_layout.addWidget(self.frame_note);content_layout.addStretch()
        save_row=QHBoxLayout();self.preset_name=QLineEdit();self.preset_name.setPlaceholderText('模板名称');self.preset_name.setMaxLength(30)
        save=QPushButton('保存模板');save.clicked.connect(self.save_preset)
        save_row.addWidget(self.preset_name,1);save_row.addWidget(save);side_layout.addLayout(save_row)
        preview_column=QVBoxLayout();body.addLayout(preview_column,1)
        nav=QHBoxLayout()
        previous=QPushButton('上一张');previous.clicked.connect(lambda:self.change_photo(-1));previous.setVisible(not template_mode);nav.addWidget(previous)
        self.photo=QComboBox()
        for row in rows:self.photo.addItem(row['observed_name'],row['id'])
        self.photo.currentIndexChanged.connect(self.schedule_preview);nav.addWidget(self.photo,1)
        if template_mode:
            choose=QPushButton('用自己的照片');choose.clicked.connect(self.choose_preview);nav.addWidget(choose)
        following=QPushButton('下一张');following.clicked.connect(lambda:self.change_photo(1));following.setVisible(not template_mode);nav.addWidget(following)
        preview_column.addLayout(nav)
        self.preview=QLabel();self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter);self.preview.setMinimumSize(300,160)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview.setStyleSheet('background:#e8edf3;border:1px solid #d4dce6;')
        preview_column.addWidget(self.preview,1)
        preview_footer=QHBoxLayout()
        self.preview_status=QLabel();self.preview_status.setWordWrap(True);preview_footer.addWidget(self.preview_status,1)
        self.detail_button=QPushButton('查看细节');self.detail_button.setEnabled(False)
        self.detail_button.clicked.connect(self.show_preview_detail);preview_footer.addWidget(self.detail_button)
        preview_column.addLayout(preview_footer)
        self.credit=QLabel();self.credit.setObjectName('secondary');self.credit.setWordWrap(True)
        self.credit.setOpenExternalLinks(True);self.credit.setVisible(template_mode);preview_column.addWidget(self.credit)
        output=QHBoxLayout();output.addWidget(QLabel('保存到'))
        default=str(Path(rows[0]['current_path']).parent/'水印照片')
        self.target=QLineEdit(default);output.addWidget(self.target,1)
        folder=QPushButton('选择文件夹');folder.clicked.connect(self.choose_target);output.addWidget(folder)
        output_widget=QWidget();output_widget.setLayout(output);output_widget.setVisible(not template_mode);layout.addWidget(output_widget)
        self.message=QLabel();self.message.setWordWrap(True);layout.addWidget(self.message)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.start=buttons.addButton('保存为默认模板' if template_mode else f'批量另存（{len(rows)}）',QDialogButtonBox.ButtonRole.AcceptRole);self.start.setEnabled(False);self.start.setObjectName('primary')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(220);self.timer.timeout.connect(self.launch_preview)
        initial=dict(DEFAULTS,**look_options('corner'))
        self.apply_options(self.read_settings('watermark_default',self.read_settings('watermark_last',initial)))
        self.loading=False;self.schedule_preview()

    def read_settings(self,key,default):
        try:return json.loads(str(self.host.settings.value(key,json.dumps(default))))
        except (ValueError,TypeError):return default

    def options(self):
        return normalize(dict(style=self.style.currentData(),fields=[k for k,v in self.fields.items() if v.isChecked()],
            custom=self.custom.toPlainText(),logo=self.logo.property('path') or '',position=self.position.currentData(),theme=self.theme.currentData(),
            font=self.font.currentText(),size=self.size.value(),opacity=self.opacity.value(),logo_size=self.logo_size.value(),quality=self.quality.value(),
            title=self.title.text(),signature=self.signature.text()))

    def apply_options(self,value):
        try:options=normalize(value)
        except (ValueError,TypeError,AttributeError):options=dict(DEFAULTS)
        previous=self.loading;self.loading=True
        for widget,key in [(self.style,'style'),(self.position,'position'),(self.theme,'theme')]:widget.setCurrentIndex(widget.findData(options[key]))
        self.font.setCurrentText(options['font']);self.custom.setPlainText(options['custom']);self.set_logo(options['logo'])
        self.title.setText(options['title']);self.signature.setText(options['signature'])
        for key,box in self.fields.items():box.setChecked(key in options['fields'])
        for key in ('size','opacity','logo_size','quality'):getattr(self,key).setValue(options[key])
        self.loading=previous;self.update_style_controls();self.schedule_preview()

    def update_style_controls(self):
        key=self.style.currentData()
        self.position.setEnabled(key in ('corner','film','outdoor'))
        self.frame_note.setVisible(key in ('frame','gallery','memory','postcard'))
        self.theme.setItemText(0,'暖橙' if key=='film' else '白字' if key=='corner' else '深色底 / 白字')
        self.theme.setItemText(1,'深棕' if key=='film' else '黑字' if key=='corner' else '浅色底 / 黑字')
        if key in self.look_buttons:self.look_buttons[key].setChecked(True)

    def style_changed(self,*_):
        if self.loading:return
        self.update_style_controls();self.schedule_preview()

    def apply_look(self,key):
        self.loading=True
        look=look_options(key)
        for widget,field in ((self.style,'style'),(self.position,'position'),(self.theme,'theme')):
            widget.setCurrentIndex(widget.findData(look[field]))
        self.font.setCurrentText(look['font']);self.size.setValue(look['size']);self.opacity.setValue(look['opacity'])
        self.loading=False;self.update_style_controls();self.schedule_preview()

    def choose_preview(self):
        path,_=QFileDialog.getOpenFileName(self,'选择预览照片','','照片 (*.jpg *.jpeg *.png *.heic *.heif *.tif *.tiff)')
        if not path:return
        self.rows.append(dict(id='local-'+path,observed_name=Path(path).name,current_path=path))
        self.photo.addItem(Path(path).name,self.rows[-1]['id']);self.photo.setCurrentIndex(len(self.rows)-1)

    def apply_preset(self,name):
        if name in self.presets:
            self.preset_name.setText(name);self.apply_options(self.presets[name])

    def save_preset(self):
        name=self.preset_name.text().strip()
        if not name:self.message.setText('请填写模板名称。');return
        try:options=self.options()
        except ValueError as exc:self.message.setText(str(exc));return
        if len(self.presets)>=30 and name not in self.presets:self.message.setText('最多保存 30 个常用模板。');return
        self.presets[name]=options
        self.host.settings.setValue('watermark_presets',json.dumps(self.presets,ensure_ascii=False));self.host.settings.sync()
        if self.preset.findText(name)<0:self.preset.addItem(name)
        self.message.setText('模板已保存')

    def choose_logo(self):
        path,_=QFileDialog.getOpenFileName(self,'选择 Logo','','图片 (*.png *.jpg *.jpeg)')
        if not path:return
        def store(_):
            logo=load_logo(path)
            try:
                buf=io.BytesIO();logo.save(buf,format='PNG');data=buf.getvalue()
            finally:logo.close()
            root=self.host.data_dir/'watermark_assets';root.mkdir(parents=True,exist_ok=True)
            file=root/(hashlib.sha256(data).hexdigest()+'.png')
            if not file.exists():
                with file.open('xb') as stream:stream.write(data)
            elif file.read_bytes()!=data:raise ValueError('已保存的 Logo 校验失败。')
            return str(file)
        self.host.job(store,lambda value:self.set_logo(value,Path(path).name) if not self.closed else None,
                      failed=lambda value:self.message.setText(value) if not self.closed else None)

    def set_logo(self,path,label='图片 Logo'):
        self.logo.setProperty('path',path)
        self.logo.setText(label if path else '')
        self.schedule_preview()

    def choose_target(self):
        value=QFileDialog.getExistingDirectory(self,'保存水印照片',self.target.text())
        if value:self.target.setText(value)

    def change_photo(self,step):
        self.photo.setCurrentIndex((self.photo.currentIndex()+step)%len(self.rows))

    def schedule_preview(self,*_):
        if self.loading or self.closed or not hasattr(self,'timer'):return
        self.revision+=1;self.preview_valid=False;self.start.setEnabled(False);self.detail_button.setEnabled(False)
        self.preview_status.setText('正在预览…');self.timer.start()

    def launch_preview(self):
        if self.closed or self.preview_running:return
        try:options=self.options()
        except ValueError as exc:self.preview_status.setText(str(exc));return
        revision=self.revision;row=dict(self.rows[self.photo.currentIndex()]);self.preview_running=True
        if self.template_mode:
            self.credit.setText(f'<a style="color:#64748b;text-decoration:none" href="{row["link"]}">图源：{row["credit"]}</a>' if row.get('link') else '本地照片 · 仅预览')
        def work(_):
            if self.template_mode and row.get('filename'):
                if not self.cache or self.cache[0]!=row['id']:
                    if self.cache:self.cache[1].close()
                    with Image.open(ASSETS/row['filename']) as source:
                        image=source.convert('RGB')
                    if row['portrait']:
                        cropped=ImageOps.fit(image,(600,800),method=Image.Resampling.LANCZOS);image.close();image=cropped
                    elif row['id']=='landscape':
                        cropped=ImageOps.fit(image,(1200,800),method=Image.Resampling.LANCZOS);image.close();image=cropped
                    self.cache=(row['id'],image,{37386:24,33437:2.8,33434:1/250,34855:100})
            else:
                path=Path(row['current_path']);info=path.stat();key=(str(path),info.st_size,info.st_mtime_ns)
                if not self.cache or self.cache[0]!=key:
                    image,values,_=read_photo(path,preview=True)
                    if self.cache:self.cache[1].close()
                    self.cache=(key,image,values)
            _,image,values=self.cache
            text=caption(values,row,options);logo=load_logo(options['logo'])
            try:
                if not text and logo is None:raise ValueError('这张照片没有所选信息，请添加文字或 Logo。')
                result=render(image,text,options,logo)
                try:
                    data=io.BytesIO();result.save(data,format='PNG',compress_level=1)
                    return data.getvalue()
                finally:result.close()
            finally:
                if logo:logo.close()
        def done(data):
            self.preview_running=False
            if self.closed:self.release_preview();return
            if revision==self.revision:
                pixmap=QPixmap();pixmap.loadFromData(data);self.pixmap=pixmap;self.fit_preview()
                self.preview_valid=True;self.start.setEnabled(True);self.detail_button.setEnabled(True);self.preview_status.setText(f'预览 {self.photo.currentIndex()+1} / {len(self.rows)}')
            else:self.timer.start()
        def failed(message):
            self.preview_running=False
            if self.closed:self.release_preview();return
            if revision==self.revision:
                self.preview.clear();self.pixmap=None;self.preview_status.setText(message)
            else:self.timer.start()
        self.host.job(work,done,failed=failed)

    def fit_preview(self):
        if self.pixmap:
            ratio=self.preview.devicePixelRatioF()
            scaled=self.pixmap.scaled(self.preview.size()*ratio,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio);self.preview.setPixmap(scaled)

    def show_preview_detail(self):
        if not self.preview_valid or self.pixmap is None:return
        dialog=QDialog(self);dialog.setWindowTitle('水印细节');dialog.resize(1000,700)
        layout=QVBoxLayout(dialog);scroll=QScrollArea();label=QLabel()
        label.setPixmap(self.pixmap);label.resize(self.pixmap.size());scroll.setWidget(label)
        layout.addWidget(scroll)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if self.style.currentData() in ('frame','bar','gallery','postcard','memory') or self.position.currentData().startswith('bottom'):
            QTimer.singleShot(0,lambda:scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum()))
        fit_window(dialog);dialog.exec()

    def showEvent(self,event):
        super().showEvent(event)
        # The editor panes already scroll; keep the preview and footer in view.
        fit_window(self)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'preview'):self.fit_preview()

    def accept(self):
        if not self.preview_valid:return
        if not self.template_mode and not self.target.text().strip():self.message.setText('请选择保存文件夹。');return
        self.host.settings.setValue('watermark_default' if self.template_mode else 'watermark_last',json.dumps(self.options(),ensure_ascii=False));self.host.settings.sync()
        super().accept()

    def done(self,result):
        self.closed=True;self.timer.stop()
        if not self.preview_running:self.release_preview()
        super().done(result)

    def release_preview(self):
        if self.cache:self.cache[1].close();self.cache=None
        self.pixmap=None;self.preview.clear()
