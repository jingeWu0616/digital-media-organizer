"""Compact, collapsible per-file progress using the app's shared table model."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QComboBox, QLineEdit, QProgressBar, QHeaderView, QAbstractItemView,
    QStyledItemDelegate, QSizePolicy)
from task_queue import LABELS, ACTIVE


def processing_text(row):
    value=row['phase']
    if row.get('percent') is not None and row['status']=='running':value+=f" {row['percent']:.1f}%"
    encoder=row.get('encoder','')
    label=next((v for k,v in [('_qsv','Intel'),('_nvenc','NVIDIA'),('_amf','AMD')] if encoder.endswith(k)), 'CPU' if encoder in ('libx264','libx265') else '')
    if label:value+=' · '+label
    if row.get('fps') and row['status']=='running':value+=f" · {row['fps']} fps"
    if row.get('speed') and row['status']=='running':value+=' · '+row['speed']
    if row.get('bytes_per_second') and row['status']=='running':value+=f" · {row['bytes_per_second']/1024**2:.1f} MB/s"
    return value


def duration(seconds):
    seconds=max(0,int(seconds))
    return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}' if seconds>=3600 else f'{seconds//60:02}:{seconds%60:02}'


class PhaseDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        row=index.model().rows[index.row()]
        percent=row.get('percent')
        if row['status']!='running':return super().paint(painter,option,index)
        rect=option.rect.adjusted(3,4,-3,-4)
        painter.save()
        painter.fillRect(rect,QColor('#f5f7fa'))
        if percent is not None:
            filled=rect.adjusted(0,0,-round(rect.width()*(1-percent/100)),0)
            painter.fillRect(filled,QColor('#dceaff'))
        painter.setPen(QColor('#263344'))
        painter.drawText(rect.adjusted(6,0,-4,0),Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         painter.fontMetrics().elidedText(processing_text(row),Qt.TextElideMode.ElideRight,rect.width()-12))
        painter.restore()


class TaskPanel(QWidget):
    def __init__(self, queue, model_factory, table_factory, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Maximum)
        self.queue=queue;self.records=[];self.batches=[]
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(4)
        bar=QHBoxLayout()
        self.expand=QPushButton('收起进度');self.expand.setCheckable(True);self.expand.setChecked(True)
        self.expand.toggled.connect(self.toggle);bar.addWidget(self.expand)
        self.scope=QComboBox();self.scope.setMinimumWidth(160);self.scope.setMaximumWidth(240)
        self.scope.addItem('全部任务','');self.scope.currentIndexChanged.connect(self.render);bar.addWidget(self.scope)
        self.summary=QLabel();bar.addWidget(self.summary,1)
        self.overall=QProgressBar();self.overall.setFixedWidth(110);bar.addWidget(self.overall)
        self.pause=QPushButton('暂停');self.pause.clicked.connect(self.pause_clicked);bar.addWidget(self.pause)
        self.stop=QPushButton('停止');self.stop.clicked.connect(lambda:self.queue.control(self.scope.currentData(),'stop'));bar.addWidget(self.stop)
        layout.addLayout(bar)
        self.body=QWidget();body=QVBoxLayout(self.body);body.setContentsMargins(0,0,0,0);body.setSpacing(3)
        self.model=model_factory(['文件名','动作','当前阶段','已用时间'],['source_name','action_label','phase','elapsed_text'])
        self.table=table_factory(self.model)
        self.table.setFixedHeight(96)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1,90);self.table.setColumnWidth(3,90)
        self.table.setItemDelegateForColumn(2,PhaseDelegate(self.table))
        self.table.selectionModel().selectionChanged.connect(self.path_changed)
        body.addWidget(self.table)
        self.path=QLineEdit();self.path.setReadOnly(True);self.path.setAccessibleName('任务文件完整路径')
        body.addWidget(self.path);layout.addWidget(self.body)
        self.hide()

    def toggle(self, expanded):
        self.body.setVisible(expanded);self.expand.setText('收起进度' if expanded else '展开进度')

    def update_records(self, records):
        self.records=records
        for r in records:
            if r['batch_id'] in self.batches:continue
            self.batches.append(r['batch_id'])
            self.scope.addItem(f"{len(self.batches)} · {LABELS[r['action']]}",r['batch_id'])
        self.setVisible(bool(records));self.render()

    def render(self, *_):
        batch=self.scope.currentData()
        rows=[dict(r,action_label=LABELS[r['action']],elapsed_text=duration(r['elapsed']))
              for r in self.records if not batch or r['batch_id']==batch]
        active=sum(r['status'] in ACTIVE for r in rows)
        complete=sum(r['status']=='complete' for r in rows)
        failed=sum(r['status']=='failed' for r in rows)
        finished=len(rows)-active
        skipped=sum(r['status']=='skipped' for r in rows)
        stopped=sum(r['status']=='cancelled' for r in rows)
        self.summary.setText(f'完成 {complete} · 待完成 {active}' + (f' · 异常 {failed}' if failed else '')
                             + (f' · 跳过 {skipped}' if skipped else '') + (f' · 停止 {stopped}' if stopped else ''))
        self.overall.setRange(0,max(1,len(rows)));self.overall.setValue(finished)
        self.overall.setFormat('已处理 %v/%m')
        paused=any(r['status']=='paused' for r in rows)
        self.pause.setText('继续' if paused else '暂停')
        self.pause.setToolTip('当前文件处理结束后暂停后续文件')
        self.pause.setEnabled(any(r['status'] in ('queued','paused') for r in rows))
        self.stop.setEnabled(bool(active))
        # Keep running files visible first, then queued files, then recent results.
        rows.sort(key=lambda r: (0 if r['status']=='running' else 1 if r['status'] in ACTIVE else 2))
        old_ids=[r['id'] for r in self.model.rows];new_ids=[r['id'] for r in rows]
        if old_ids==new_ids:
            self.model.rows=rows
            if rows:self.model.dataChanged.emit(self.model.index(0,0),self.model.index(len(rows)-1,3))
        else:
            selected=self.table.selectionModel().selectedRows()
            identifier=old_ids[selected[0].row()] if selected and selected[0].row()<len(old_ids) else None
            self.model.replace(rows)
            if rows:self.table.selectRow(next((i for i,r in enumerate(rows) if r['id']==identifier),0))
        self.path_changed()

    def pause_clicked(self):
        self.queue.control(self.scope.currentData(),'resume' if self.pause.text()=='继续' else 'pause')
        self.update_records(self.queue.snapshot())

    def path_changed(self, *_):
        selected=self.table.selectionModel().selectedRows()
        row=self.model.rows[selected[0].row()] if selected and selected[0].row()<len(self.model.rows) else None
        value=row['source_path'] if row else ''
        if value!=self.path.text():self.path.setText(value);self.path.setCursorPosition(0)
        self.path.setToolTip((row.get('error','') or row.get('note','') or processing_text(row)) if row else '')
