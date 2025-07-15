import sys
import asyncio
import aiohttp
import json
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
    QHBoxLayout, QDoubleSpinBox, QPushButton, QMenu
)
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QFont, QColor, QBrush
from qasync import QEventLoop, asyncSlot

FUT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
SPOT_WS_URL = "wss://stream.bybit.com/v5/public/spot"
KLINE_URL = "https://api.bybit.com/v5/market/kline"
CATEGORIES = ["spot", "linear"]

SCAN_INTERVAL = 60
PERCENT_THRESHOLD = 10  # порог по умолчанию
KLINE_INTERVAL = "15"

last_prices = {}  # (symbol, category) -> (price, last_update_time)
current_movers = set()  # {(symbol, category)}
mover_history = {}  # (symbol, category) -> {'entered': ..., 'left': ..., 'max_change': ..., 'entry_price': ...}

# Добавим глобальные переменные для статуса
scan_status = ""
last_scan_time = None
num_scanned = 0

async def get_all_symbols():
    all_symbols = set()
    async with aiohttp.ClientSession() as session:
        for cat in CATEGORIES:
            url = f"https://api.bybit.com/v5/market/instruments-info?category={cat}"
            async with session.get(url) as resp:
                data = await resp.json()
                for x in data['result']['list']:
                    all_symbols.add((x['symbol'], cat))
    return list(all_symbols)

async def get_last_two_klines(symbol, category):
    url = f"{KLINE_URL}?category={category}&symbol={symbol}&interval={KLINE_INTERVAL}&limit=2"
    import aiohttp
    import asyncio
    for attempt in range(3):  # 3 попытки
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    try:
                        data = await resp.json()
                    except Exception:
                        continue
                    klines = data.get('result', {}).get('list', [])
                    if len(klines) >= 2:
                        prev_close = float(klines[1][4])
                        last_close = float(klines[0][4])
                        prev_time = datetime.fromtimestamp(int(klines[1][0]) // 1000)
                        last_time = datetime.fromtimestamp(int(klines[0][0]) // 1000)
                        return prev_close, last_close, prev_time, last_time
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
            await asyncio.sleep(2)  # подождать и попробовать снова
        except Exception:
            await asyncio.sleep(2)
    return None, None, None, None

async def scan_movers_loop():
    global current_movers, scan_status, last_scan_time, num_scanned
    while True:
        scan_status = "Подключение к Bybit..."
        print(scan_status)
        try:
            symbols = await get_all_symbols()
        except Exception as e:
            scan_status = f"Ошибка API: {e}"
            print(scan_status)
            await asyncio.sleep(10)
            continue
        num_scanned = len(symbols)
        scan_status = f"Сканируем {num_scanned} тикеров (spot + linear)..."
        print(scan_status)
        tasks = [get_last_two_klines(symbol, category) for symbol, category in symbols]
        results = await asyncio.gather(*tasks)
        new_movers = set()
        change_map = {}
        for (symbol, category), (prev_close, last_close, prev_time, last_time) in zip(symbols, results):
            if prev_close and last_close:
                change_pct = (last_close - prev_close) / prev_close * 100
                if abs(change_pct) > PERCENT_THRESHOLD:
                    new_movers.add((symbol, category))
                    change_map[(symbol, category)] = change_pct
                    if (symbol, category) not in current_movers:
                        mover_history[(symbol, category)] = {
                            'entered': datetime.now(),
                            'left': None,
                            'max_change': change_pct,
                            'entry_price': last_close
                        }
                    else:
                        if abs(change_pct) > abs(mover_history[(symbol, category)]['max_change']):
                            mover_history[(symbol, category)]['max_change'] = change_pct
        for mover in new_movers - current_movers:
            asyncio.create_task(start_ws_for_mover(mover[0], mover[1]))
        for mover in current_movers - new_movers:
            if mover in mover_history:
                mover_history[mover]['left'] = datetime.now()
        current_movers = new_movers
        last_scan_time = datetime.now()
        scan_status = f"Обновлено: {last_scan_time.strftime('%H:%M:%S')}, тикеров: {num_scanned}, movers: {len(current_movers)}"
        print(scan_status)
        await asyncio.sleep(SCAN_INTERVAL)

async def start_ws_for_mover(symbol, category):
    ws_url = FUT_WS_URL if category == "linear" else SPOT_WS_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                sub_msg = {"op": "subscribe", "args": [f"tickers.{symbol}"]}
                await ws.send_json(sub_msg)
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                obj = json.loads(msg.data)
                                if 'data' in obj and 'topic' in obj:
                                    topic = obj['topic']
                                    if topic.startswith('tickers.'):
                                        data = obj['data']
                                        price = data['lastPrice']
                                        last_prices[(symbol, category)] = (price, datetime.now().strftime('%H:%M:%S'))
                            except Exception:
                                pass
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
                except asyncio.CancelledError:
                    await ws.close()
                    raise
    except Exception:
        pass

class MoversWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Movers Bybit")
        self.setMinimumWidth(700)
        self.setMinimumHeight(400)
        self.setStyleSheet(self.dark_stylesheet())
        layout = QVBoxLayout(self)

        # Строка статуса
        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Arial", 10))
        layout.addWidget(self.status_label)

        # --- Новый блок: выбор процента ---
        percent_layout = QHBoxLayout()
        percent_label = QLabel("Порог Δ% (за 15 мин):")
        percent_label.setFont(QFont("Arial", 10))
        percent_layout.addWidget(percent_label)
        self.percent_spin = QDoubleSpinBox()
        self.percent_spin.setDecimals(2)
        self.percent_spin.setMinimum(0.1)
        self.percent_spin.setMaximum(100.0)
        self.percent_spin.setSingleStep(0.1)
        self.percent_spin.setValue(PERCENT_THRESHOLD)
        percent_layout.addWidget(self.percent_spin)
        self.percent_btn = QPushButton("Применить")
        percent_layout.addWidget(self.percent_btn)
        layout.addLayout(percent_layout)
        self.percent_btn.clicked.connect(self.apply_percent_threshold)

        movers_label = QLabel("Movers (активные)")
        movers_label.setFont(QFont("Arial", 12, QFont.Bold))
        layout.addWidget(movers_label)

        self.movers_table = QTableWidget(0, 7)
        self.movers_table.setHorizontalHeaderLabels([
            "Тикер", "Тип", "Цена", "Время обновл.", "Цена (добавл.)", "Макс.движ.", "Δ% (добавл.)"
        ])
        # Tooltips для movers_table
        tooltips = [
            "Тикер — название торговой пары",
            "Тип — категория (spot/linear)",
            "Цена — последняя цена",
            "Время обновл. — время последнего обновления цены",
            "Цена (добавл.) — цена при попадании в movers",
            "Макс.движ. — максимальное изменение цены с момента попадания в movers (обновляется в реальном времени)",
            "Δ% (добавл.) — изменение цены с момента попадания в movers"
        ]
        for i, tip in enumerate(tooltips):
            item = self.movers_table.horizontalHeaderItem(i)
            if item:
                item.setToolTip(tip)
        self.movers_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.movers_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.movers_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.movers_table.setContextMenuPolicy(3)  # Qt.CustomContextMenu
        self.movers_table.customContextMenuRequested.connect(self.movers_table_menu)
        layout.addWidget(self.movers_table)

        archive_label = QLabel("Архив movers")
        archive_label.setFont(QFont("Arial", 12, QFont.Bold))
        layout.addWidget(archive_label)

        self.archive_table = QTableWidget(0, 6)
        self.archive_table.setHorizontalHeaderLabels([
            "Тикер", "Тип", "Добавлен", "Удалён", "Макс.движ.", "Текущее Δ%"
        ])
        # Tooltips для archive_table
        arch_tooltips = [
            "Тикер — название торговой пары",
            "Тип — категория (spot/linear)",
            "Добавлен — время попадания в movers",
            "Удалён — время выхода из movers",
            "Макс.движ. — максимальное изменение цены за период",
            "Текущее Δ% — текущее изменение цены относительно цены попадания в movers (отслеживается с момента попадания и далее)"
        ]
        for i, tip in enumerate(arch_tooltips):
            item = self.archive_table.horizontalHeaderItem(i)
            if item:
                item.setToolTip(tip)
        self.archive_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.archive_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.archive_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.archive_table.setContextMenuPolicy(3)
        self.archive_table.customContextMenuRequested.connect(self.archive_table_menu)
        layout.addWidget(self.archive_table)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_tables)
        self.timer.start(1000)

    def apply_percent_threshold(self):
        global PERCENT_THRESHOLD
        PERCENT_THRESHOLD = self.percent_spin.value()
        from __main__ import scan_status
        scan_status = f"Порог Δ% изменён на {PERCENT_THRESHOLD:.2f}%. Ожидайте обновления..."

    def update_tables(self):
        from __main__ import scan_status
        self.status_label.setText(scan_status)
        # Movers
        movers = list(current_movers)
        self.movers_table.setRowCount(len(movers))
        for row, (symbol, category) in enumerate(movers):
            price, upd = last_prices.get((symbol, category), ("-", "-"))
            entry_price = mover_history.get((symbol, category), {}).get('entry_price', '-')
            try:
                # Форматируем цену и цену добавления
                if price != "-":
                    price_str = f"{float(price):.8f}"
                else:
                    price_str = "-"
                if entry_price != "-":
                    entry_str = f"{float(entry_price):.8f}"
                else:
                    entry_str = "-"
            except Exception:
                price_str = str(price)
                entry_str = str(entry_price)
            try:
                if price != "-" and entry_price != "-":
                    price_f = float(price)
                    entry_f = float(entry_price)
                    if price_f > entry_f:
                        color = QBrush(QColor(0, 200, 0))  # зелёный
                    elif price_f < entry_f:
                        color = QBrush(QColor(220, 0, 0))  # красный
                    else:
                        color = None
                else:
                    color = None
            except Exception:
                color = None
            # Макс.движ.
            maxchg = mover_history.get((symbol, category), {}).get('max_change', None)
            if maxchg is not None:
                maxchg_str = f"{maxchg:+.2f}%"
            else:
                maxchg_str = "-"
            # Δ% (добавл.)
            try:
                if price != "-" and entry_price != "-":
                    change_since_entry = (float(price) - float(entry_price)) / float(entry_price) * 100
                    change_str = f"{change_since_entry:+.2f}%"
                else:
                    change_str = "-"
            except Exception:
                change_str = "-"
            self.movers_table.setItem(row, 0, QTableWidgetItem(symbol))
            self.movers_table.setItem(row, 1, QTableWidgetItem(category))
            self.movers_table.setItem(row, 2, QTableWidgetItem(price_str))
            self.movers_table.setItem(row, 3, QTableWidgetItem(str(upd)))
            self.movers_table.setItem(row, 4, QTableWidgetItem(entry_str))
            self.movers_table.setItem(row, 5, QTableWidgetItem(maxchg_str))
            self.movers_table.setItem(row, 6, QTableWidgetItem(change_str))
        # Archive
        archive = [(s, c, info) for (s, c), info in mover_history.items() if (s, c) not in current_movers and info['left']]
        self.archive_table.setRowCount(len(archive))
        for row, (symbol, category, info) in enumerate(archive):
            entered = info['entered'].strftime('%Y-%m-%d %H:%M:%S') if info['entered'] else '-'
            left = info['left'].strftime('%Y-%m-%d %H:%M:%S') if info['left'] else '-'
            maxchg = f"{info['max_change']:+.2f}%"
            # Текущее Δ%
            entry_price = info.get('entry_price', '-')
            last_price = last_prices.get((symbol, category), (None,))[0]
            try:
                if last_price is not None and entry_price != "-":
                    curr_delta = (float(last_price) - float(entry_price)) / float(entry_price) * 100
                    curr_delta_str = f"{curr_delta:+.2f}%"
                else:
                    curr_delta_str = "-"
            except Exception:
                curr_delta_str = "-"
            self.archive_table.setItem(row, 0, QTableWidgetItem(symbol))
            self.archive_table.setItem(row, 1, QTableWidgetItem(category))
            self.archive_table.setItem(row, 2, QTableWidgetItem(entered))
            self.archive_table.setItem(row, 3, QTableWidgetItem(left))
            self.archive_table.setItem(row, 4, QTableWidgetItem(maxchg))
            self.archive_table.setItem(row, 5, QTableWidgetItem(curr_delta_str))

    def movers_table_menu(self, pos):
        item = self.movers_table.itemAt(pos)
        if item and item.column() == 0:  # Только колонка тикера
            ticker_text = item.text()
            menu = QMenu()
            copy_action = menu.addAction("Скопировать тикер")
            action = menu.exec_(self.movers_table.viewport().mapToGlobal(pos))
            if action == copy_action:
                QApplication.clipboard().setText(ticker_text)

    def archive_table_menu(self, pos):
        item = self.archive_table.itemAt(pos)
        if item and item.column() == 0:
            ticker_text = item.text()
            menu = QMenu()
            copy_action = menu.addAction("Скопировать тикер")
            action = menu.exec_(self.archive_table.viewport().mapToGlobal(pos))
            if action == copy_action:
                QApplication.clipboard().setText(ticker_text)

    def dark_stylesheet(self):
        return """
        QWidget {
            background-color: #232629;
            color: #e0e0e0;
        }
        QHeaderView::section {
            background-color: #2c2f33;
            color: #e0e0e0;
            font-weight: bold;
            border: 1px solid #444;
        }
        QTableWidget {
            background-color: #232629;
            gridline-color: #444;
            selection-background-color: #44475a;
            selection-color: #f8f8f2;
        }
        QTableWidget QTableCornerButton::section {
            background-color: #2c2f33;
            border: 1px solid #444;
        }
        QLabel {
            color: #f8f8f2;
        }
        """

# Вместо async def main и asyncio.run(main()):
if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication
    from qasync import QEventLoop
    import asyncio

    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    w = MoversWidget()
    w.show()
    asyncio.ensure_future(scan_movers_loop())
    with loop:
        loop.run_forever() 