"""PySide6 desktop GUI: entirely local; never uploads database dumps."""
from __future__ import annotations
import sys
from threading import Event
from collections import Counter
from pathlib import Path
from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot, QStandardPaths
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QScrollArea, QLayout,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from .engine import ConversionCancelled, convert_file, default_output_name, validate_output_name
from .streaming import MAX_SQL_FILE_BYTES, validate_file_size
from .mappings import default_rows, load_overrides, save_overrides

DESTINATION_TYPES = [
    'BIGINT', 'INTEGER', 'SMALLINT', 'BOOLEAN', 'NUMERIC(p,s)', 'NUMERIC(20,0)',
    'DECIMAL(p,s)', 'DECIMAL(20,0)', 'REAL', 'FLOAT', 'DOUBLE PRECISION', 'DOUBLE',
    'VARCHAR(n)', 'VARCHAR(255)', 'CHAR(n)', 'CHAR(255)', 'TEXT', 'LONGTEXT',
    'MEDIUMTEXT', 'JSON', 'JSONB', 'BYTEA', 'BLOB', 'LONGBLOB', 'TIMESTAMP',
    'TIMESTAMP(p)', 'TIMESTAMP(6)', 'DATETIME', 'DATETIME(p)', 'DATETIME(6)',
    'DATE', 'TIME', 'TINYINT(1)', 'INT', 'UUID',
]

STYLE = """
QWidget { font-family: 'Microsoft YaHei', 'Segoe UI'; font-size: 12px; color: #243247; }
QMainWindow, QWidget#root { background: #F5F7FB; }
QGroupBox { font-size: 13px; font-weight: 600; background: white;
            border: 1px solid #E3E8F0; border-radius: 9px; margin-top: 15px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 1px 6px; }
QLineEdit, QComboBox, QPlainTextEdit, QTableWidget {
    border: 1px solid #DDE4EE; background: #FFFFFF; border-radius: 6px;
    padding: 5px; selection-background-color: #D7E8FF; }
QLineEdit:focus, QComboBox:focus { border-color: #4889E7; }
QPushButton { background: white; border: 1px solid #D7DFEC;
              padding: 8px 12px; border-radius: 6px; font-weight: 500; }
QPushButton:hover { background: #EEF5FF; border-color: #8DB5EC; }
QPushButton#primary { background: #2777DF; color: white; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #1769CA; }
QPushButton:disabled { background: #E8EDF4; color: #A3AEBD; }
QHeaderView::section { background: #F4F7FB; border: none; border-bottom: 1px solid #E3E8F0; padding: 9px; }
QTableWidget { gridline-color: #EDF1F7; }
QScrollArea { border: none; background: #F5F7FB; }
QLabel#subtitle { color: #758398; }
QProgressBar { border: 1px solid #DDE4EE; border-radius: 6px; background: #ECF2F9;
               text-align: center; min-height: 20px; }
QProgressBar::chunk { background: #3286E6; border-radius: 5px; }
"""

class ConversionWorker(QObject):
    progress = Signal(int)
    succeeded = Signal(str, str, object)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, source: Path, folder: Path, direction: str, mappings: dict, encoding: str, output_name: str):
        super().__init__()
        self.source = source
        self.folder = folder
        self.direction = direction
        self.mappings = mappings
        self.encoding = encoding
        self.output_name = output_name
        self._cancel_requested = Event()

    def cancel(self):
        self._cancel_requested.set()

    @Slot()
    def run(self):
        try:
            def progress(done: int, total: int) -> None:
                self.progress.emit(round(100 * done / max(total, 1)))
            sql_path, report_path, report = convert_file(
                self.source, self.folder, self.direction, self.mappings, self.encoding, progress, self.output_name,
                cancel_check=self._cancel_requested.is_set)
            self.succeeded.emit(str(sql_path), str(report_path), report)
        except ConversionCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(f'{type(exc).__name__}: {exc}')
        finally:
            self.finished.emit()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('SQL 转换工具 · MySQL ↔ openGauss')
        self.resize(1110, 830)
        self.setMinimumSize(780, 560)
        folder = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
        self.settings_path = Path(folder or str(Path.home() / '.sql-converter')) / 'mappings.json'
        self.custom_settings = load_overrides(self.settings_path)
        self._direction = 'mysql_to_opengauss'
        self._busy = False
        self._result_file = ''
        self._report_file = ''
        self._output_name_custom = False
        self._setup_ui()
        self._fill_mappings()

    def _setup_ui(self):
        # The old one-piece expanding layout could compress group boxes until
        # labels overlapped the mapping table. A scroll viewport prevents this
        # on small / scaled Windows displays while keeping controls usable.
        viewport = QScrollArea(self)
        viewport.setWidgetResizable(True)
        viewport.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setCentralWidget(viewport)
        root = QWidget(objectName='root')
        root.setMinimumWidth(735)
        viewport.setWidget(root)
        layout = QVBoxLayout(root)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 14, 20, 18)
        title = QLabel('SQL 转换工具')
        title.setStyleSheet('font-size: 22px; font-weight: 700;')
        subtitle = QLabel('MySQL ↔ openGauss  ·  本地离线转换  ·  未支持语法会生成阻断报告')
        subtitle.setObjectName('subtitle')
        layout.addWidget(title)
        layout.addWidget(subtitle)

        files = QGroupBox('文件与转换方向')
        fv = QVBoxLayout(files)
        row = QHBoxLayout()
        self.source_line = QLineEdit()
        self.source_line.setReadOnly(True)
        self.source_line.setPlaceholderText(f'选择本地 .sql 文件（最多 {MAX_SQL_FILE_BYTES // (1024 * 1024)} MiB）')
        self.source_line.setToolTip('文件只在本地读取，不会上传服务器。超大 SQL 采用分块处理。')
        choose = QPushButton('选择 SQL 文件')
        choose.clicked.connect(self._choose_input)
        row.addWidget(self.source_line, 1)
        row.addWidget(choose)
        fv.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel('转换方向'))
        self.direction_box = QComboBox()
        self.direction_box.addItem('MySQL  →  openGauss', 'mysql_to_opengauss')
        self.direction_box.addItem('openGauss  →  MySQL', 'opengauss_to_mysql')
        self.direction_box.currentIndexChanged.connect(self._direction_changed)
        row.addWidget(self.direction_box, 2)
        row.addWidget(QLabel('文件编码'))
        self.encoding_box = QComboBox()
        self.encoding_box.addItem('自动检测（UTF-8 / GB18030）', 'auto')
        self.encoding_box.addItem('UTF-8', 'utf-8-sig')
        self.encoding_box.addItem('GB18030', 'gb18030')
        row.addWidget(self.encoding_box, 2)
        fv.addLayout(row)
        row = QHBoxLayout()
        self.folder_line = QLineEdit()
        self.folder_line.setReadOnly(True)
        self.folder_line.setPlaceholderText('默认输出到原文件所在目录')
        browse_folder = QPushButton('选择输出目录')
        browse_folder.clicked.connect(self._choose_folder)
        row.addWidget(QLabel('输出路径'))
        row.addWidget(self.folder_line, 1)
        row.addWidget(browse_folder)
        fv.addLayout(row)
        row = QHBoxLayout()
        name_label = QLabel('输出文件名')
        name_label.setMinimumWidth(70)
        self.output_name_line = QLineEdit()
        self.output_name_line.setPlaceholderText('自动生成：原文件名_opengauss.sql（可修改）')
        self.output_name_line.setToolTip('输入 .sql 文件名，不要包含目录。发生阻断时自动追加 _review，防止误当成完整脚本。')
        self.output_name_line.textEdited.connect(self._name_edited)
        restore_name = QPushButton('使用默认名')
        restore_name.clicked.connect(self._restore_name)
        row.addWidget(name_label)
        row.addWidget(self.output_name_line, 1)
        row.addWidget(restore_name)
        fv.addLayout(row)
        layout.addWidget(files)

        maps = QGroupBox('数据类型映射（目标类型可搜索 / 下拉 / 手动修改）')
        mv = QVBoxLayout(maps)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('模糊搜索：类型、目标类型或备注（不区分大小写）')
        self.search.textChanged.connect(self._filter_mappings)
        reset_btn = QPushButton('恢复默认')
        reset_btn.clicked.connect(self._reset_mappings)
        save_btn = QPushButton('保存映射配置')
        save_btn.clicked.connect(self._save_mappings)
        tools.addWidget(self.search, 1)
        tools.addWidget(reset_btn)
        tools.addWidget(save_btn)
        mv.addLayout(tools)
        self.map_table = QTableWidget(0, 3)
        self.map_table.setHorizontalHeaderLabels(['来源类型', '目标类型（可编辑）', '说明'])
        self.map_table.verticalHeader().setVisible(False)
        self.map_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.map_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.map_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.map_table.setAlternatingRowColors(True)
        self.map_table.setMinimumHeight(280)
        self.map_table.setMaximumHeight(280)
        self.map_table.verticalHeader().setDefaultSectionSize(36)
        self.map_table.setWordWrap(False)
        self.map_table.setStyleSheet('QTableWidget::item { padding: 3px 5px; }')
        mv.addWidget(self.map_table)
        hint = QLabel('映射可临时修改；点击“保存映射配置”后，会保存在本机用户配置目录。危险类型或特殊语法仍可能被阻断。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #758398; font-size: 11px;')
        mv.addWidget(hint)
        layout.addWidget(maps)

        runrow = QHBoxLayout()
        self.run_button = QPushButton('开始转换')
        self.run_button.setObjectName('primary')
        self.run_button.setMinimumWidth(150)
        self.run_button.clicked.connect(self._start)
        self.cancel_button = QPushButton('取消转换')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        runrow.addWidget(self.run_button)
        runrow.addWidget(self.cancel_button)
        runrow.addWidget(self.progress, 1)
        layout.addLayout(runrow)

        results = QGroupBox('转换结果与语法报告')
        rv = QVBoxLayout(results)
        self.status = QLabel('请先选择 SQL 文件。报告中的 BLOCKER 表示输出 SQL 仍需人工处理。')
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color:#69788D;')
        rv.addWidget(self.status)
        self.report_text = QPlainTextEdit()
        self.report_text.setReadOnly(True)
        self.report_text.setPlaceholderText('完成后显示转换数量、阻断语法的位置以及输出文件路径。')
        self.report_text.setMinimumHeight(155)
        self.report_text.setMaximumHeight(230)
        rv.addWidget(self.report_text, 1)
        buttons = QHBoxLayout()
        self.open_result = QPushButton('打开结果文件')
        self.open_result.clicked.connect(lambda: self._open(self._result_file))
        self.open_report = QPushButton('查看报告 JSON')
        self.open_report.clicked.connect(lambda: self._open(self._report_file))
        self.open_result.setEnabled(False)
        self.open_report.setEnabled(False)
        buttons.addWidget(self.open_result)
        buttons.addWidget(self.open_report)
        buttons.addStretch()
        rv.addLayout(buttons)
        layout.addWidget(results)
        layout.addStretch(1)

    def _choose_input(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 SQL 文件', '', 'SQL 文件 (*.sql);;所有文件 (*)')
        if path:
            try:
                validate_file_size(Path(path))
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, '文件大小不符合要求', str(exc))
                return
            self.source_line.setText(path)
            if not self.folder_line.text():
                self.folder_line.setText(str(Path(path).parent))
            if not self._output_name_custom:
                self.output_name_line.setText(default_output_name(Path(path), self._direction))

    def _name_edited(self, value: str):
        self._output_name_custom = True

    def _restore_name(self):
        self._output_name_custom = False
        source_text = self.source_line.text()
        self.output_name_line.setText(default_output_name(Path(source_text), self._direction) if source_text else '')

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择转换结果目录', self.folder_line.text())
        if folder:
            self.folder_line.setText(folder)

    def _collect(self) -> dict[str, str]:
        values = {}
        for i, row in enumerate(default_rows(self._direction)):
            combo = self.map_table.cellWidget(i, 1)
            if combo is not None:
                target = combo.currentText().strip()
                if target and target != row.target:
                    values[row.source] = target
        return values

    def _fill_mappings(self):
        rows = default_rows(self._direction)
        overrides = self.custom_settings.get(self._direction, {})
        self.map_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            item = QTableWidgetItem(row.source)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.map_table.setItem(i, 0, item)
            combo = QComboBox()
            combo.setEditable(True)
            opts = list(dict.fromkeys([row.target, *row.choices, *DESTINATION_TYPES]))
            combo.addItems(opts)
            combo.setInsertPolicy(QComboBox.NoInsert)
            completer = QCompleter(opts, combo)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            combo.setCompleter(completer)
            combo.setCurrentText(overrides.get(row.source, row.target))
            self.map_table.setCellWidget(i, 1, combo)
            note = QTableWidgetItem(row.note)
            note.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.map_table.setItem(i, 2, note)
        self._filter_mappings(self.search.text())

    def _filter_mappings(self, value: str):
        needle = value.strip().casefold()
        for i in range(self.map_table.rowCount()):
            parts = [self.map_table.item(i, 0).text(),
                     self.map_table.cellWidget(i, 1).currentText(),
                     self.map_table.item(i, 2).text()]
            self.map_table.setRowHidden(i, bool(needle) and not any(needle in p.casefold() for p in parts))

    def _direction_changed(self):
        self.custom_settings[self._direction] = self._collect()
        self._direction = self.direction_box.currentData()
        if not self._output_name_custom and self.source_line.text():
            self.output_name_line.setText(default_output_name(Path(self.source_line.text()), self._direction))
        self._fill_mappings()

    def _reset_mappings(self):
        self.custom_settings[self._direction] = {}
        self._fill_mappings()

    def _save_mappings(self):
        self.custom_settings[self._direction] = self._collect()
        try:
            save_overrides(self.settings_path, self.custom_settings)
            QMessageBox.information(self, '已保存', f'映射配置已保存到：\n{self.settings_path}')
        except OSError as exc:
            QMessageBox.critical(self, '保存失败', str(exc))

    def _start(self):
        if self._busy:
            return
        source = Path(self.source_line.text())
        if not source.is_file() or source.suffix.lower() != '.sql':
            QMessageBox.warning(self, '文件错误', '请选择存在的 .sql 文件')
            return
        try:
            validate_file_size(source)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, '文件大小不符合要求', str(exc))
            return
        folder = Path(self.folder_line.text() or str(source.parent))
        if not folder.is_dir():
            QMessageBox.warning(self, '路径错误', '输出目录不存在')
            return
        try:
            output_name = validate_output_name(self.output_name_line.text() or default_output_name(source, self._direction))
        except ValueError as exc:
            QMessageBox.warning(self, '输出文件名错误', str(exc))
            return
        self.custom_settings[self._direction] = self._collect()
        self._busy = True
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText('正在本机分析和转换 SQL，请稍候…')
        self.report_text.setPlainText('')
        self.open_result.setEnabled(False)
        self.open_report.setEnabled(False)
        self._thread = QThread(self)
        self._worker = ConversionWorker(source, folder, self._direction,
                                        self.custom_settings[self._direction].copy(),
                                        self.encoding_box.currentData(), output_name)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.succeeded.connect(self._success)
        self._worker.failed.connect(self._failure)
        self._worker.cancelled.connect(self._cancelled)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._finished)
        self._thread.start()

    def _cancel(self):
        if self._busy:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText('正在取消转换并清理临时文件…')

    def _cancelled(self):
        self.status.setText('转换已取消，没有生成不完整的 SQL 文件。')
        self.report_text.setPlainText('已取消，临时文件已清理。')

    def _success(self, sql_path: str, report_path: str, report):
        self._result_file = sql_path
        self._report_file = report_path
        self.progress.setValue(100)
        self.status.setText('转换结束 · 可在测试数据库验证' if report.ready else
                            f'转换结束 · {report.blocked} 条阻断语法，SQL 文件标记为 _review，不能直接当作完整迁移脚本')
        counts = Counter((f.severity, f.code) for f in report.findings)
        lines = [f'结果：{"完成静态转换（请在目标库测试）" if report.ready else "存在阻断，禁止直接导入"}',
                 f'SQL 文件：{sql_path}', f'报告文件：{report_path}',
                 f'语句总数：{report.total}   已转换：{report.converted}   跳过：{report.skipped}   阻断：{report.blocked}',
                 f"耗时：{report.metrics.get('elapsed_seconds', 0):.2f} 秒   平均处理速度：{report.metrics.get('average_mib_per_second', 0):.2f} MiB/s",
                 '', '问题摘要（相同类别合并显示）：']
        if not counts:
            lines.append('  无问题')
        else:
            for (severity, code), count in sorted(counts.items(), key=lambda x: (x[0][0] != 'BLOCKER', x[0][0] != 'WARNING', x[0][1])):
                lines.append(f'  {severity} · {code}：{count} 项')
        blockers = [f for f in report.findings if f.severity == 'BLOCKER']
        if blockers:
            lines.append('\n阻断详情：')
            for f in blockers[:120]:
                lines.append(f'  第 {f.line} 行 · {f.code}：{f.message}')
            if len(blockers) > 120:
                lines.append(f'  还有 {len(blockers) - 120} 项阻断，请查看 JSON 报告')
        else:
            lines.append('\n提示：字符序、大小写与索引约束语义仍需在真实目标数据库核对。')
        if report.omitted_findings:
            lines.append(f'报告详情超过上限，另省略 {report.omitted_findings} 条，请参考类别摘要。')
        self.report_text.setPlainText('\n'.join(lines))
        self.open_result.setEnabled(True)
        self.open_report.setEnabled(True)

    def _failure(self, message: str):
        self.status.setText('转换失败，未生成有效结果。')
        self.report_text.setPlainText(message)
        QMessageBox.critical(self, '转换失败', message)

    def _finished(self):
        self._busy = False
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _open(self, path: str):
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def closeEvent(self, event):
        if self._busy:
            QMessageBox.information(self, '转换进行中', '请等待当前转换任务结束后关闭程序。')
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    win = Window()
    win.show()
    return app.exec()
