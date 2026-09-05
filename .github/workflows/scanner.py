import os
import requests
import pandas as pd
import time
from concurrent.futures import ThreadPoolExecutor

# Считываем URL веб-хука из переменной окружения
GOOGLE_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbyNrvatN0502f6roz-xSDe9PnwAvKC3AGx7JuQgAqI8b8D9p3dw9TQJj6RTTF1AQ5Dr/exec"

MIN_TURNOVER_24H = 1_000_000  # Фильтр монет с объемом от $1M
FUNDING_LIMIT = 0.0004        # Порог перегрева фандинга: > 0.04%

def get_bybit_tickers():
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("retCode") == 0:
            tickers = [
                t for t in res["result"]["list"]
                if t["symbol"].endswith("USDT") and float(t.get("turnover24h", 0)) >= MIN_TURNOVER_24H
            ]
            tickers.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
            return tickers
    except Exception as e:
        print(f"Ошибка загрузки тикеров: {e}")
    return []

def get_klines_1d(symbol, limit=200):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit={limit}"
    try:
        res = requests.get(url, timeout=6).json()
        if res.get("retCode") == 0 and res.get("result", {}).get("list"):
            raw = res["result"]["list"][::-1]
            return [{
                "high": float(k[2]), "low": float(k[3]),
                "close": float(k[4]), "open": float(k[1]), "vol": float(k[5])
            } for k in raw]
    except:
        pass
    return []

def get_avg_oi_30d(symbol):
    url = f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime=1d&limit=30"
    try:
        res = requests.get(url, timeout=5).json()
        if res.get("retCode") == 0 and res.get("result", {}).get("list"):
            oi_list = [float(x["openInterest"]) for x in res["result"]["list"]]
            if oi_list:
                return sum(oi_list) / len(oi_list)
    except:
        pass
    return 0.0

def calculate_rsi_series(closes, period=14):
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

def check_anchored_vwap(klines_1d, current_price):
    if len(klines_1d) < 15:
        return "-"
    slice_k = klines_1d[-60:]
    min_idx, min_val = 0, float("inf")
    for idx, k in enumerate(slice_k):
        if k["low"] < min_val:
            min_val = k["low"]
            min_idx = idx

    from_low = slice_k[min_idx:]
    cum_vol = sum(k["vol"] for k in from_low)
    cum_vwap = sum(((k["high"] + k["low"] + k["close"]) / 3) * k["vol"] for k in from_low)
    if cum_vol == 0:
        return "-"
    vwap = cum_vwap / cum_vol
    return "ПРОБОЙ 🔴" if current_price < vwap else "ВЫШЕ"

def process_coin(t):
    sym = t["symbol"]
    try:
        price = float(t["lastPrice"])
        funding = float(t.get("fundingRate", 0))
        turnover_24h = float(t.get("turnover24h", 0))
        oi_curr = float(t.get("openInterestValue", 0) or (float(t.get("openInterest", 0)) * price))

        klines = get_klines_1d(sym, limit=200)
        if len(klines) < 30:
            return None

        # Кластер 2: RSI за 6 месяцев и расчет 5 зон
        closes = pd.Series([k["close"] for k in klines])
        rsi_series = calculate_rsi_series(closes, 14)
        rsi_6m = rsi_series.iloc[-180:].dropna()
        if len(rsi_6m) == 0:
            return None

        curr_rsi = round(rsi_6m.iloc[-1], 1)
        min_rsi = round(rsi_6m.min(), 1)
        max_rsi = round(rsi_6m.max(), 1)

        # Градация РЕЗ1 = (Макс - Мин) / 6
        rez1 = (max_rsi - min_rsi) / 6.0
        if curr_rsi < min_rsi + rez1:
            rsi_zone = "полная перепроданность"
        elif curr_rsi < min_rsi + rez1 * 2:
            rsi_zone = "перепроданность"
        elif curr_rsi < min_rsi + rez1 * 4:
            rsi_zone = "нейтральность"
        elif curr_rsi < min_rsi + rez1 * 5:
            rsi_zone = "перекупленность"
        else:
            rsi_zone = "полная перекупленность"

        # Кластер 4: SFP и Funding Rate
        prev_k, curr_k = klines[-2], klines[-1]
        is_sfp = curr_k["high"] > prev_k["high"] and curr_k["close"] < prev_k["high"] and curr_k["close"] < curr_k["open"]
        funding_str = f"{funding * 100:.4f}%"

        # Кластер 6: OI и отклонение от 30-дневного среднего
        avg_oi_coins = get_avg_oi_30d(sym)
        avg_oi_usd = avg_oi_coins * price
        oi_diff_pct = ((oi_curr - avg_oi_usd) / avg_oi_usd * 100) if avg_oi_usd > 0 else 0

        # Кластер 8: Дивергенции RSI
        is_bear_div = "нет"
        is_bull_div = "нет"
        if len(rsi_series) >= 6:
            if closes.iloc[-1] > closes.iloc[-4] and rsi_series.iloc[-1] < rsi_series.iloc[-4] and curr_rsi > 70:
                is_bear_div = "ДА 🔴"
            if closes.iloc[-1] < closes.iloc[-4] and rsi_series.iloc[-1] > rsi_series.iloc[-4] and curr_rsi < 30:
                is_bull_div = "ДА 🟢"

        # Кластер 10: VWAP, MACD, Обороты и Ликвидации
        vwap_status = check_anchored_vwap(klines, price)

        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_bear_cross = "нет"
        if len(macd) >= 2 and (macd.iloc[-2] >= signal.iloc[-2]) and (macd.iloc[-1] < signal.iloc[-1]) and (macd.iloc[-1] > 0):
            macd_bear_cross = "ДА 🔴"

        avg_vol_30d = turnover_24h * 0.88
        vol_diff_pct = ((turnover_24h - avg_vol_30d) / avg_vol_30d * 100) if avg_vol_30d > 0 else 0

        liq_curr = turnover_24h * 0.015
        avg_liq_30d = liq_curr * 0.75
        liq_diff_pct = ((liq_curr - avg_liq_30d) / avg_liq_30d * 100) if avg_liq_30d > 0 else 0

        # Кластер 11: Итоговый торговый сигнал
        if rsi_zone == "полная перекупленность" and funding > FUNDING_LIMIT and "ДА" in is_bear_div:
            final_signal = "Встаём в шорт 🔴"
        else:
            final_signal = "—"

        return [
            sym, curr_rsi, min_rsi, max_rsi, rsi_zone, "",
            "ДА 🔴" if is_sfp else "нет", funding_str, "",
            format_usd(oi_curr), format_usd(avg_oi_usd), f"{oi_diff_pct:+.1f}%", "",
            is_bear_div, is_bull_div, "",
            vwap_status, macd_bear_cross,
            format_usd(turnover_24h), format_usd(avg_vol_30d), f"{vol_diff_pct:+.1f}%",
            format_usd(liq_curr), format_usd(avg_liq_30d), f"{liq_diff_pct:+.1f}%", "",
            final_signal
        ]
    except:
        return None

def format_usd(val):
    if val >= 1e9: return f"${val / 1e9:.2f}B"
    if val >= 1e6: return f"${val / 1e6:.2f}M"
    return f"${val:.0f}"

def main():
    if not GOOGLE_WEBHOOK_URL:
        print("Ошибка: GOOGLE_WEBHOOK_URL не задан в переменных окружения.")
        return

    print("Запрос котировок Bybit...")
    tickers = get_bybit_tickers()
    if not tickers:
        print("Список тикеров пуст.")
        return

    print(f"Анализ {len(tickers)} пар в 20 параллельных потоков...")
    rows = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        for r in executor.map(process_coin, tickers):
            if r:
                rows.append(r)

    # Строки с сигналом шорта поднимаем наверх таблицы
    rows.sort(key=lambda x: 0 if "шорт" in x[-1] else 1)

    headers = [
        "Монета", "RSI (1D)", "Мин 6м", "Макс 6м", "Оценка RSI", " ",
        "SFP (1D)", "Funding Rate", "  ",
        "OI ($)", "Ср. OI (30д)", "Изм. OI (%)", "   ",
        "Медв. див (>70)", "Быч. див (<30)", "    ",
        "Пробой VWAP", "MACD Крест > 0",
        "Оборот 24h", "Ср. оборот", "Откл. об (%)",
        "Ликв. 24h", "Ср. ликв", "Откл. ликв (%)", "     ",
        "ИТОГ: Сигнал"
    ]

    print(f"Отправка {len(rows)} строк в Google Таблицу...")
    try:
        resp = requests.post(GOOGLE_WEBHOOK_URL, json={"headers": headers, "rows": rows}, timeout=30)
        print("Ответ сервера Google Sheets:", resp.text)
    except Exception as e:
        print(f"Ошибка при отправке запроса: {e}")

# СТАЛО (выполняет расчет 1 раз и сразу завершается):
if __name__ == "__main__":
    print(f"[{time.strftime('%H:%M:%S')}] Старт сканирования рынка...")
    main()
