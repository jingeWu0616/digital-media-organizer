"""Small selection editor using the existing native dialog and styles."""
from PySide6.QtWidgets import (QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,
    QPlainTextEdit,QComboBox,QCheckBox,QPushButton,QDialogButtonBox,QMessageBox,QDialog)
from ui_common import AdaptiveDialog
from location_dialog import LocationDialog


class ActivityDialog(AdaptiveDialog):
    def __init__(self,rows,events,parent):
        super().__init__(parent);self.rows=rows;self.events={e['id']:e for e in events};self.loading=True;self.saving=False
        self.setWindowTitle('活动与地点');self.resize(740,640)
        layout=QVBoxLayout(self)
        photos=sum(r['kind']=='photo' for r in rows)
        layout.addWidget(QLabel(f'已选 {photos} 张照片、{len(rows)-photos} 个视频'))
        hint=QLabel('地点与备注不改文件名；写入文件坐标请使用“添加定位”。');hint.setWordWrap(True);hint.setObjectName('secondary');layout.addWidget(hint)
        form=QFormLayout();self.form=form;form.setVerticalSpacing(12);layout.addLayout(form)
        self.activity=QComboBox();self.activity.addItem('保持各自活动',None);self.activity.addItem('新建活动','new');self.activity.addItem('不关联活动','')
        for event in events:self.activity.addItem(f"{event['name']}  ·  创建于 {event['created_at'][:10]}",event['id'])
        form.addRow('所属活动',self.activity)
        self.edit_event=QCheckBox('修改活动信息');form.addRow('',self.edit_event)
        self.name=QLineEdit();self.name.setMaxLength(100);self.name.setPlaceholderText('例如 周末亲子游')
        self.companions=QLineEdit();self.companions.setMaxLength(300);self.companions.setPlaceholderText('可选，例如 家人、小王')
        self.note=QPlainTextEdit();self.note.setFixedHeight(76);self.note.setPlaceholderText('这次活动想记下的事')
        for label,editor in [('活动名称',self.name),('同行人',self.companions),('活动备注',self.note)]:form.addRow(label,editor)
        self.scope=QLabel();self.scope.setWordWrap(True);self.scope.setObjectName('secondary');form.addRow('',self.scope)
        self.fields={};self.toggles={}
        for key,title in [('place','地点'),('media_note','素材备注')]:
            checkbox=QCheckBox(title);edit=QLineEdit() if key=='place' else QPlainTextEdit()
            if key=='place':edit.setMaxLength(200)
            else:edit.setFixedHeight(76)
            values={r.get(key) or '' for r in rows};value=next(iter(values)) if len(values)==1 else ''
            if key=='place':edit.setText(value)
            else:edit.setPlainText(value)
            edit.setPlaceholderText('多个值，勾选后修改' if len(values)>1 else '勾选后修改')
            edit.setEnabled(False);checkbox.toggled.connect(edit.setEnabled)
            self.fields[key]=edit;self.toggles[key]=checkbox
            line=QHBoxLayout();line.addWidget(edit,1)
            clear=QPushButton('清空');clear.clicked.connect(lambda checked=False,k=key:self.clear_field(k));line.addWidget(clear)
            form.addRow(checkbox,line)
        map_button=QPushButton('从地图选地点');map_button.clicked.connect(self.pick_place)
        map_row=QHBoxLayout();map_row.addWidget(map_button);map_row.addStretch();form.addRow('',map_row)
        layout.addStretch(1)
        self.message=QLabel();self.message.setWordWrap(True);layout.addWidget(self.message)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.save=buttons.addButton('保存到所选素材',QDialogButtonBox.ButtonRole.AcceptRole);self.save.setObjectName('primary')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.activity.currentIndexChanged.connect(self.select_event);self.edit_event.toggled.connect(self.enable_event)
        ids={r.get('activity_id') for r in rows}
        if len(ids)==1 and next(iter(ids)) in self.events:self.activity.setCurrentIndex(self.activity.findData(next(iter(ids))))
        self.loading=False;self.select_event()

    def select_event(self,*_):
        key=self.activity.currentData();event=self.events.get(key,{})
        self.name.setText(event.get('name',''));self.companions.setText(event.get('companions',''));self.note.setPlainText(event.get('note',''))
        self.edit_event.setVisible(key in self.events);self.edit_event.setChecked(False);self.enable_event()

    def enable_event(self,*_):
        key=self.activity.currentData();active=key=='new' or key in self.events and self.edit_event.isChecked()
        for editor in (self.name,self.companions,self.note):
            editor.setEnabled(active);self.form.setRowVisible(editor,key=='new' or key in self.events)
        self.scope.setText(f"修改活动信息将影响已关联的 {self.events[key]['total']} 个素材。" if key in self.events else '')
        self.scope.setVisible(key in self.events)

    def clear_field(self,key):
        self.toggles[key].setChecked(True);self.fields[key].clear()

    def pick_place(self):
        dialog=LocationDialog(len(self.rows),self.parent(),picker_only=True)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            name=dialog.value()['location_name']
            if not name:self.message.setText('请为地点填写名称。');return
            self.toggles['place'].setChecked(True);self.fields['place'].setText(name)

    def value(self):
        key=self.activity.currentData();result=dict(activity_id=key,patch={})
        for field,editor in self.fields.items():
            if self.toggles[field].isChecked():result['patch'][field]=(editor.text() if field=='place' else editor.toPlainText()).strip()
        values=dict(name=self.name.text().strip(),companions=self.companions.text().strip(),note=self.note.toPlainText().strip())
        if key=='new':result.update(activity_id=None,new_activity=values)
        elif key in self.events and self.edit_event.isChecked():
            result.update(activity_patch={k:v for k,v in values.items() if self.events[key][k]!=v},expected_activity=self.events[key])
        if (key=='new' or result.get('activity_patch')) and not values['name']:raise ValueError('请填写活动名称。')
        if len(values['note'])>2000 or len(result['patch'].get('media_note',''))>1000:raise ValueError('活动备注最多 2000 字，素材备注最多 1000 字。')
        return result

    def accept(self):
        try:value=self.value()
        except ValueError as exc:self.message.setText(str(exc));return
        warnings=[]
        if value.get('activity_patch'):warnings.append(f"修改活动信息，影响已关联的 {self.events[value['activity_id']]['total']} 个素材。")
        if self.activity.currentData() is not None and any(r.get('activity_id') and r['activity_id']!=self.activity.currentData() for r in self.rows):warnings.append('替换所选素材原来的活动归属。')
        for key,label in [('place','地点'),('media_note','素材备注')]:
            if key in value['patch'] and any(r.get(key) and r.get(key)!=value['patch'][key] for r in self.rows):warnings.append(('清空' if not value['patch'][key] else '替换')+f'所选素材已有的{label}。')
        if warnings and QMessageBox.question(self,'确认修改','\n'.join(warnings),QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        self.saving=True;self.setEnabled(False)
        def done(result):
            self.saving=False;self.setEnabled(True);self.save_result=result
            super(ActivityDialog,self).accept()
        def failed(message):
            self.saving=False;self.setEnabled(True);self.message.setText(message)
        self.parent().job(lambda _:self.parent().activity_service.apply(self.rows,**value),done,failed=failed)

    def reject(self):
        if not self.saving:super().reject()
