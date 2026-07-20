# -*- coding: utf-8 -*-
"""
나투비 데이터 파이프라인
GitHub Actions에서 매일 장 마감 후 실행되어 data/*.json 을 생성합니다.

사용법:
    python build_data.py            # 실제 데이터 수집 (FDR + DART)
    python build_data.py --sample   # 가짜 샘플 데이터 생성 (로컬 미리보기용)

환경변수:
    DART_API_KEY : DART 오픈API 키 (없으면 실적 분석은 건너뛰고 기술적 분석만 수행)
    TOP_N        : 스크리닝 대상 종목 수 (기본 100)
"""
import json
import os
import sys
import math
import time
import random
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def write_json(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(path) / 1024
    print(f"  ✅ {name} ({size:,.0f} KB)")


def now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def append_history(score, kospi, indicators=None, components=None):
    """공포-탐욕 점수와 글로벌 지표를 날짜별로 누적 (같은 날짜는 갱신, 최근 400개 유지)
    행 형식: [날짜, 점수, KOSPI종가, {지표명: 값}, {컴포넌트 원시값}]
    컴포넌트 원시값(strength·breadth)은 z-score 정규화 이력으로 쓰이므로
    1년치(FG_WIN=252)가 쌓일 수 있도록 보존 개수를 400으로 늘림."""
    path = os.path.join(DATA_DIR, "history.json")
    rows = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f).get("rows", [])
        except Exception:
            rows = []
    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows = [r for r in rows if r[0] != today]
    ind_map = {name: val for name, val, _chg, _u in (indicators or [])}
    raw = {}
    if components:
        if "_raw_strength" in components:
            raw["strength"] = components["_raw_strength"]
        if "_raw_breadth" in components:
            raw["breadth"] = components["_raw_breadth"]
    rows.append([today, score, kospi["close"] if kospi else None, ind_map, raw])
    rows = rows[-400:]
    write_json("history.json", {"rows": rows})


# ============================================================
# 실제 데이터 수집
# ============================================================

def get_latest_report_info():
    now = datetime.now(KST)
    target_year = now.year - 2 if now.month <= 3 else now.year - 1
    return target_year, "11011"


def clean_value(val, pd):
    try:
        if pd.isna(val) or val == "":
            return 0
        return int(str(val).replace(",", "").split(".")[0])
    except Exception:
        return 0


def collect_indicators(fdr):
    """글로벌 참고 지표 수집: 종목별 실패는 건너뛰고 나머지는 정상 제공"""
    specs = [
        # (표시명, FDR 심볼, 단위, 소수점)
        ("KOSDAQ",   "KQ11",    "",  2),
        ("S&P 500",  "US500",   "",  2),
        ("나스닥",    "IXIC",    "",  2),
        ("다우존스",  "DJI",     "",  2),
        ("달러/원",   "USD/KRW", "원", 2),
        ("금(온스)",  "GC=F",    "$", 2),
        ("WTI 유가",  "CL=F",    "$", 2),
    ]
    out = []
    start = (datetime.now(KST) - timedelta(days=14)).strftime("%Y-%m-%d")
    for name, sym, unit, nd in specs:
        try:
            df = fdr.DataReader(sym, start)
            if df is None or "Close" not in df:
                continue
            closes = df["Close"].dropna()
            if len(closes) < 2:
                continue
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            out.append([name, round(last, nd), round((last / prev - 1) * 100, 2), unit])
            print(f"  · {name}: {last:,.2f} ({(last/prev-1)*100:+.2f}%)")
        except Exception as e:
            print(f"  ⚠️ {name}({sym}) 수집 실패 — 건너뜀: {e}")
    return out


FG_CLIP = 2.0   # z-score 절단 폭 (±2σ → 0점/100점)
FG_WIN = 252    # 정규화 기준 창 (약 1년 거래일)


def _fg_z2s(series, win=FG_WIN, clip=FG_CLIP, name=""):
    """지표를 '평소 변동폭 대비 이탈 정도'로 0~100 환산.
    CNN Fear & Greed 핵심 원리: 절대 임계값 대신 자기 이력 대비 상대 위치(z-score).
    데이터 부족·NaN이면 None 반환(컴포넌트 제외) — 가짜 중립값 주입 방지.
    주의: 파이썬 min/max는 NaN을 조용히 통과시키므로(min(2.0, nan)==2.0)
    반드시 사전에 NaN을 걸러야 한다. NaN이 새면 z가 +clip으로 둔갑해 100점이 된다."""
    m, sd = series.rolling(win).mean(), series.rolling(win).std()
    last, mu, s = series.iloc[-1], m.iloc[-1], sd.iloc[-1]
    valid = all(v is not None and v == v for v in (last, mu, s)) and s > 0  # v==v → NaN 필터
    if not valid:
        if name:
            print(f"  ⚠️ {name}: 데이터 부족 또는 이상치 — 이번 계산에서 제외")
        return None
    z = max(-clip, min(clip, (last - mu) / s))
    return float((z + clip) / (2 * clip) * 100)


def _fg_scalar_score(pd, value, hist_values, name=""):
    """스칼라 지표(가격강도·시장폭)를 과거 이력 대비 0~100 환산.
    이력이 60개 미만이면 None(컴포넌트 제외) — 초기 60거래일간 자동 축소 운영."""
    if len(hist_values) < 60:
        return None
    s = pd.Series(list(hist_values) + [value], dtype=float)
    return _fg_z2s(s, win=min(FG_WIN, len(s) - 1), name=name)


def load_past_components():
    """history.json에서 가격강도·시장폭 원시값 이력을 로드 (없으면 빈 리스트).
    오늘 날짜 행은 제외 — 같은 날 재실행 시 1차 실행값이 자기 정규화 기준에
    섞여 들어가는 자기참조를 방지한다."""
    path = os.path.join(DATA_DIR, "history.json")
    out = {"strength": [], "breadth": []}
    if not os.path.exists(path):
        return out
    today = datetime.now(KST).strftime("%Y-%m-%d")
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f).get("rows", [])
        for r in rows:
            if r[0] == today:
                continue
            raw = r[4] if len(r) > 4 and isinstance(r[4], dict) else {}
            if isinstance(raw.get("strength"), (int, float)):
                out["strength"].append(raw["strength"])
            if isinstance(raw.get("breadth"), (int, float)):
                out["breadth"].append(raw["breadth"])
    except Exception:
        pass
    return out


def calc_market_score(fdr, pd, merged=None, histories=None, past=None):
    """공포-탐욕 지수 (CNN 방법론 이식: 컴포넌트별 z-score → 동일가중 평균)
    merged    : KRX 전종목 스냅샷 (ChagesRatio, Amount) — 시장폭 계산용
    histories : {ticker: 일봉 DataFrame} — 52주 신고/신저가 계산용 (기존 수집분 재사용)
    past      : load_past_components() 결과 — 스칼라 지표 정규화용 이력
    반환      : (score, kospi, components)"""
    past = past or {}
    comp = {}
    try:
        start = (datetime.now(KST) - timedelta(days=760)).strftime("%Y-%m-%d")
        k = fdr.DataReader("^KS11", start)
        if k is None or len(k) < 150:
            return 50, None, {}

        # ── 1. 모멘텀: 125일 이동평균 대비 이격 ──
        mom = k["Close"] / k["Close"].rolling(125).mean() - 1
        sc = _fg_z2s(mom, name="모멘텀")
        if sc is not None:
            comp["모멘텀"] = sc

        # ── 2. 변동성: 20일 실현변동성 (낮을수록 탐욕 → 역방향) ──
        rv = k["Close"].pct_change().rolling(20).std()
        sc = _fg_z2s(rv, name="변동성")
        if sc is not None:
            comp["변동성"] = 100 - sc

        # ── 3. 안전자산 수요: 주식 20일 수익률 − 금 20일 수익률 ──
        try:
            g = fdr.DataReader("GC=F", start)["Close"].reindex(k.index).ffill()
            spread = (k["Close"] / k["Close"].shift(20) - 1) - (g / g.shift(20) - 1)
            sc = _fg_z2s(spread, name="안전자산")
            if sc is not None:
                comp["안전자산"] = sc
        except Exception as e:
            print(f"  ⚠️ 안전자산 지표 건너뜀: {e}")

        # ── 4. 가격 강도: 52주 신고가 vs 신저가 종목수 ──
        if histories:
            hi = lo = 0
            for df_h in histories.values():
                if df_h is None or len(df_h) < 200:
                    continue
                c = df_h["Close"]
                w = c.tail(252)
                if c.iloc[-1] >= w.max() * 0.99:
                    hi += 1
                elif c.iloc[-1] <= w.min() * 1.01:
                    lo += 1
            if hi + lo > 0:
                raw = (hi - lo) / (hi + lo)  # -1(전부 신저가) ~ +1(전부 신고가)
                comp["_raw_strength"] = round(raw, 4)
                sc = _fg_scalar_score(pd, raw, past.get("strength", []), name="가격강도")
                if sc is not None:
                    comp["가격강도"] = sc

        # ── 5. 시장 폭: 상승종목 거래대금 비중 ──
        if merged is not None and "Amount" in merged:
            up_amt = float(merged.loc[merged["ChagesRatio"] > 0, "Amount"].sum())
            dn_amt = float(merged.loc[merged["ChagesRatio"] < 0, "Amount"].sum())
            if up_amt + dn_amt > 0:
                raw = up_amt / (up_amt + dn_amt)  # 0~1
                comp["_raw_breadth"] = round(raw, 4)
                sc = _fg_scalar_score(pd, raw, past.get("breadth", []), name="시장폭")
                if sc is not None:
                    comp["시장폭"] = sc

        # ── 합산: 동일가중 평균 (CNN 방식) ──
        parts = {kk: v for kk, v in comp.items() if not kk.startswith("_")}
        if not parts:
            return 50, None, comp
        score = int(round(max(0, min(100, sum(parts.values()) / len(parts)))))
        for kk, v in parts.items():
            print(f"  · {kk}: {v:.0f}")

        kospi = {
            "close": round(float(k["Close"].iloc[-1]), 2),
            "change": round(float(k["Close"].iloc[-1] / k["Close"].iloc[-2] - 1) * 100, 2),
            "date": k.index[-1].strftime("%Y-%m-%d"),  # 휴장일 판별용 (프론트 미사용)
        }
        return score, kospi, comp
    except Exception as e:
        print(f"  ⚠️ 시장 점수 계산 실패: {e}")
        return 50, None, {}


def check_fundamental(dart, pd, ticker, mcap, target_year, report_code):
    """DART 실적 기반 장기 필터 (기존 로직 이식). 통과 시 dict, 미통과 시 None."""
    try:
        dart_df = dart.finstate(ticker, target_year, reprt_code=report_code)
        time.sleep(0.15)
        if dart_df is None:
            return None
        op_df = dart_df[
            (dart_df["fs_nm"].str.contains("연결")) & (dart_df["account_nm"].str.contains("영업이익"))
        ]
        if op_df.empty:
            op_df = dart_df[dart_df["account_nm"].str.contains("영업이익")]
        if op_df.empty:
            return None

        val_curr = clean_value(op_df.iloc[0]["thstrm_amount"], pd)
        val_prev = clean_value(op_df.iloc[0]["frmtrm_amount"], pd)
        val_pprev = clean_value(op_df.iloc[0]["bfefrmtrm_amount"], pd)
        if not (val_curr > 0 and val_curr > val_prev):
            return None

        def growth(curr, prev):
            if prev <= 0:
                return "흑자전환", 999.0
            rate = round(((curr / prev) - 1) * 100, 1)
            return f"{rate}%", rate

        disp_curr, rate_curr = growth(val_curr, val_prev)
        disp_prev, rate_prev = growth(val_prev, val_pprev)
        multiple = round(mcap / val_curr, 1)

        # 흑자전환(직전 결산 적자→흑자, rate_curr=999 센티널)은 별도 라벨로 분리.
        # 배점 조건(score_accel)은 종전 로직과 비트 단위로 동일하게 유지 —
        # 라벨만 바꾸고 점수를 바꾸면 추천 결과가 달라지므로 의도적으로 분리하지 않음.
        # (2년 연속 적자 후 전환은 종전대로 +10, 전년 흑자→적자→금년 전환은 종전대로 +35)
        is_turn = rate_curr == 999.0
        score_accel = rate_curr > rate_prev and rate_prev != 999.0   # 종전 is_accel 그대로
        is_accel = score_accel and not is_turn                        # 라벨 표시용
        is_streak = (not is_turn) and rate_curr > 0 and rate_prev > 0 and rate_prev != 999.0
        if is_turn:
            trait = "🔄흑자전환"
        else:
            trait = ("🚀성장가속 " if is_accel else "") + ("📈연속성장" if is_streak else "✅반등/전환")

        score = 0
        if multiple <= 10:
            score += 40
        elif multiple <= 15:
            score += 25
        elif multiple <= 20:
            score += 10
        if score_accel:
            score += 35
        elif is_streak:
            score += 20
        else:
            score += 10
        if val_curr >= 10_000_000_000:
            score += 15
        elif val_curr >= 5_000_000_000:
            score += 8
        if rate_curr != 999.0 and rate_curr >= 50:
            score += 10
        elif rate_curr != 999.0 and rate_curr >= 20:
            score += 5

        return {
            "marcap": int(mcap / 100000000),
            "multiple": multiple,
            "growthCurr": disp_curr,
            "growthPrev": disp_prev,
            "trait": trait.strip(),
            "opProfit": int(val_curr / 100000000),
            "profitDelta": int((val_curr - val_prev) / 100000000),
            "fundScore": min(score, 100),
        }
    except Exception:
        return None


def fetch_history(fdr, ticker):
    """종목 일봉 이력 1회 조회 (타이밍 지표·포트폴리오 신호가 공유)"""
    try:
        # 52주(252거래일) 신고가 계산을 위해 달력일 기준 420일 조회 (휴장일 감안)
        df = fdr.DataReader(ticker, (datetime.now(KST) - timedelta(days=420)).strftime("%Y-%m-%d"))
        if df is None or len(df) < 60:
            return None
        return df
    except Exception:
        return None


def calc_signal(pd, df):
    """포트폴리오 추세 신호 재료 (기존 check_my_stocks 이식 + 손절 로직 보강)
    반환: [현재가, ATR, 단기 데드크로스(5-20), 중기 데드크로스(20-60), 20일 최고 종가]
    손절가는 클라이언트에서 샹들리에 스톱 방식으로 계산:
        stop = max(20일 최고 종가 − 2×ATR, 매수가×0.95)
    ※ '현재가 − 2×ATR'를 손절 기준으로 쓰면 주가와 함께 손절선이 내려가
       (cur ≤ cur − 2×ATR 은 항상 거짓) 알림이 절대 발동하지 않는다.
       고점 기준으로 계산해야 손절선이 위로만 올라가는 트레일링 스톱이 된다."""
    try:
        close = df["Close"]
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        curr = float(close.iloc[-1])

        high_low = df["High"] - df["Low"]
        high_close = (df["High"] - close.shift()).abs()
        low_close = (df["Low"] - close.shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        # Wilder 평활 ATR (업계 표준, RSI와 동일 계열) — 단순이동평균 대비 급등락에 덜 과민
        atr = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])

        dead_5_20 = (ma5 < ma20) & (ma5.shift(1) >= ma20.shift(1))
        dead_20_60 = (ma20 < ma60) & (ma20.shift(1) >= ma60.shift(1))
        hi20 = float(close.tail(20).max())  # 샹들리에 스톱 기준 고점

        return [round(curr), round(atr, 1),
                int(bool(dead_5_20.tail(5).any())), int(bool(dead_20_60.tail(5).any())),
                round(hi20)]
    except Exception:
        return None


def analyze_timing(pd, df):
    """추세 관문 + 셋업(눌림목/돌파) 분석. 기존 '신호 4개 중 2개' 방식을 대체."""
    try:
        close, volume, high = df["Close"], df["Volume"], df["High"]

        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        curr = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        m20, m60 = float(ma20.iloc[-1]), float(ma60.iloc[-1])

        # ── 추세 관문: 상승 흐름인가 ──
        trend = (m20 > m60) and (curr > m60)

        # 골든크로스 (최근 5거래일 내) — 가점 요소
        crossings = (ma20 > ma60) & (ma20.shift(1) <= ma60.shift(1))
        gc_recent = bool(crossings.tail(5).any())
        gc_date = crossings[crossings].index[-1].strftime("%Y-%m-%d") if gc_recent else "-"

        # RSI (Wilder)
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi_last = (100 - (100 / (1 + rs))).iloc[-1]
        rsi = 100.0 if pd.isna(rsi_last) else round(float(rsi_last), 1)

        # 거래량 (방향 포함: 상승일 급증만 돌파 신호로 인정)
        vol_ma20 = volume.rolling(20).mean()
        vol_ratio = round(float(volume.iloc[-1] / vol_ma20.iloc[-1]), 2) if vol_ma20.iloc[-1] > 0 else 0.0
        up_day = curr > prev

        # 52주 고점 대비
        high_52w = float(high.tail(252).max())
        ratio52 = round(curr / high_52w * 100, 1)

        # ── 셋업 판정 ──
        near_ma20 = m20 > 0 and abs(curr / m20 - 1) <= 0.03      # 20일선 ±3%
        pullback = trend and (35 <= rsi <= 55) and near_ma20      # 🔵 눌림목
        breakout = trend and ratio52 >= 92 and vol_ratio >= 1.5 and up_day  # 🔴 돌파

        # 셋업 강도 점수 (0~100)
        setup_score = 0
        if breakout:
            setup_score = 60 + (15 if gc_recent else 0) + min(20, int((vol_ratio - 1.5) * 10))
        elif pullback:
            setup_score = 60 + (15 if gc_recent else 0) + max(0, 10 - int(abs(rsi - 45)))
        setup_score = min(setup_score, 100)

        chart = {
            "dates": [d.strftime("%Y-%m-%d") for d in df.tail(120).index],
            "close": [round(float(v)) for v in close.tail(120)],
            "ma20": [None if pd.isna(v) else round(float(v)) for v in ma20.tail(120)],
            "ma60": [None if pd.isna(v) else round(float(v)) for v in ma60.tail(120)],
        }

        return {
            "price": int(curr), "diff": int(curr - prev),
            "trend": trend, "gcRecent": gc_recent, "gcDate": gc_date,
            "rsi": rsi, "volRatio": vol_ratio, "upDay": up_day,
            "high52Ratio": ratio52,
            "pullback": pullback, "breakout": breakout, "setupScore": setup_score,
            "_chart": chart,
        }
    except Exception:
        return None


def classify(t):
    """상태 라벨 + 초보자용 한 줄 근거 생성"""
    if not t["trend"]:
        return "dn", "20일선이 60일선 아래 — 흐름이 꺾여 있어 지금은 매수보다 관망이 안전해요."
    if t["breakout"]:
        return "brk", (f"1년 최고가의 {t['high52Ratio']:.0f}% 지점 — 가장 강한 구간을 "
                       f"거래량 {t['volRatio']:.1f}배가 실린 상승으로 뚫는 중이에요.")
    if t["pullback"]:
        return "pull", (f"상승 흐름을 유지한 채 20일선 부근까지 쉬어가는 구간(RSI {t['rsi']:.0f}) — "
                        f"조정 후 재상승을 노리는 자리예요.")
    return "wait", "상승 흐름은 살아있지만 아직 매수 타이밍(눌림목·돌파) 신호가 없어요. 지켜보세요."


NEWS_POS_KW = ["급등", "상승", "호실적", "흑자", "신고가", "성장", "수주", "매수", "기대", "돌파", "강세", "개선"]
NEWS_NEG_KW = ["급락", "하락", "적자", "손실", "우려", "위기", "매도", "리스크", "부진", "약세", "하향", "감소"]


def collect_news(rec_entries):
    """오늘의 추천 종목만 구글 뉴스 RSS로 헤드라인 수집 (제목·출처·날짜·링크만)"""
    import requests
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    out = {}
    for e in rec_entries:
        try:
            q = requests.utils.quote(f"{e['name']} 주가")
            url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            items, seen = [], set()
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = it.findtext("link") or ""
                srcname = (it.findtext("source") or "").strip()
                if title.endswith(" - " + srcname):     # 제목 끝의 언론사 중복 제거
                    title = title[: -len(" - " + srcname)]
                key = "".join(title.split())[:20]        # 재배포 기사 중복 제거
                if not title or key in seen:
                    continue
                seen.add(key)
                try:
                    date = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST).strftime("%m-%d")
                except Exception:
                    date = ""
                items.append([title, srcname, date, link])
                if len(items) >= 5:
                    break
            pos = sum(1 for t, *_ in items if any(k in t for k in NEWS_POS_KW))
            neg = sum(1 for t, *_ in items if any(k in t for k in NEWS_NEG_KW) and not any(k in t for k in NEWS_POS_KW))
            out[e["code"]] = {"items": items, "senti": [pos, neg, len(items) - pos - neg]}
            print(f"  📰 {e['name']}: 뉴스 {len(items)}건")
            time.sleep(0.4)
        except Exception as ex:
            print(f"  ⚠️ {e['name']} 뉴스 수집 실패 — 건너뜀: {ex}")
    return out


def build_real():
    import pandas as pd
    import FinanceDataReader as fdr

    top_n = int(os.environ.get("TOP_N", "100"))
    dart_key = os.environ.get("DART_API_KEY", "").strip()
    dart = None
    if dart_key:
        try:
            import OpenDartReader
            dart = OpenDartReader(dart_key)
            print("🔑 DART 키 확인 — 실적 분석 포함")
        except Exception as e:
            print(f"⚠️ DART 초기화 실패({e}) — 기술적 분석만 수행")
    else:
        print("⚠️ DART_API_KEY 없음 — 기술적 분석만 수행")

    updated = now_str()

    # ── 1. 전 종목 시세 + 섹터 ──
    print("📥 KRX 전 종목 시세 수집...")
    df_price = fdr.StockListing("KRX")
    df_desc = fdr.StockListing("KRX-DESC")
    merged = pd.merge(
        df_price[["Code", "Name", "Close", "ChagesRatio", "Marcap", "Volume", "Amount"]],
        df_desc[["Code", "Sector"]], on="Code", how="left",
    )
    merged["Amount"] = pd.to_numeric(merged["Amount"], errors="coerce").fillna(0)
    merged["Close"] = pd.to_numeric(merged["Close"], errors="coerce").fillna(0)
    merged["ChagesRatio"] = pd.to_numeric(merged["ChagesRatio"], errors="coerce").fillna(0)
    merged["Marcap"] = pd.to_numeric(merged["Marcap"], errors="coerce").fillna(0)
    merged = merged[merged["Close"] > 0]

    # prices.json — [코드, 종목명, 종가, 등락률] 압축 배열
    prices = [
        [r.Code, r.Name, int(r.Close), round(float(r.ChagesRatio), 2)]
        for r in merged.itertuples()
    ]
    write_json("prices.json", {"updated": updated, "rows": prices})

    # sectors.json — 시총 상위 500 (트리맵용)
    top500 = merged[merged["Sector"].notna()].nlargest(500, "Marcap")
    sectors = [
        [r.Name, r.Sector, int(r.Marcap / 100000000), round(float(r.ChagesRatio), 2)]
        for r in top500.itertuples()
    ]
    write_json("sectors.json", {"updated": updated, "rows": sectors})

    # ── 2. 글로벌 지표 ──
    # 공포-탐욕 지수는 52주 신고/신저가 계산에 종목 히스토리가 필요하므로
    # 스크리닝 루프(3번)에서 히스토리를 모은 뒤에 계산한다.
    print("🌍 글로벌 지표 수집...")
    indicators = collect_indicators(fdr)

    # ── 3. 종목 스크리닝 + 포트폴리오 신호 ──
    signal_n = max(int(os.environ.get("SIGNAL_N", "300")), top_n)
    rec_max = int(os.environ.get("REC_MAX", "10"))
    print(f"🔍 상위 {top_n}종목 스크리닝 + 상위 {signal_n}종목 추세 신호 계산...")
    target_year, report_code = get_latest_report_info()
    candidates = merged.nlargest(signal_n, "Marcap")

    results, charts, signals = [], {}, {}
    histories = {}  # 공포-탐욕 '가격강도' 계산용 (추가 API 호출 없이 재사용)
    for i, row in enumerate(candidates.itertuples(), 1):
        ticker, name, mcap = row.Code, row.Name, row.Marcap
        if i % 25 == 0:
            print(f"  ... {i}/{signal_n} ({name})")

        df_hist = fetch_history(fdr, ticker)
        if df_hist is None:
            continue
        histories[ticker] = df_hist

        # 포트폴리오 신호 (상위 signal_n 전체)
        sig = calc_signal(pd, df_hist)
        if sig:
            signals[ticker] = sig

        # 스크리닝 분석 (상위 top_n)
        if i > top_n:
            continue
        t = analyze_timing(pd, df_hist)
        if t is None:
            continue
        fund = check_fundamental(dart, pd, ticker, mcap, target_year, report_code) if dart else None
        status, reason = classify(t)

        entry = {"code": ticker, "name": name, "fundPass": fund is not None,
                 "status": status, "reason": reason}
        chart = t.pop("_chart")
        entry.update(t)
        if fund:
            entry.update(fund)
            # 종합점수 = 실적 40% + 셋업 강도 60% (셋업이 있을 때만 의미)
            entry["score"] = round(fund["fundScore"] * 0.4 + t["setupScore"] * 0.6)
        else:
            entry["score"] = round(t["setupScore"] * 0.6)
        charts[ticker] = chart
        results.append(entry)

    # ── 오늘의 추천 선정: 실적 관문 + 상승 흐름 + 셋업 보유 → 점수 상위 rec_max ──
    eligible = [e for e in results if e["fundPass"] and e["status"] in ("pull", "brk")]
    eligible.sort(key=lambda e: e["score"], reverse=True)
    for rank, e in enumerate(eligible[:rec_max], 1):
        e["rec"] = True
        e["rank"] = rank
    rec_list = eligible[:rec_max]
    print(f"⭐ 오늘의 추천 {len(rec_list)}종목 (눌림목 {sum(1 for e in rec_list if e['status']=='pull')} · "
          f"돌파 {sum(1 for e in rec_list if e['status']=='brk')})")

    # ── 4. 시장 점수 (히스토리 확보 후 계산) ──
    print("📊 공포-탐욕 지수 계산...")
    past = load_past_components()
    score, kospi, components = calc_market_score(fdr, pd, merged, histories, past)
    up = int((merged["ChagesRatio"] > 0).sum())
    down = int((merged["ChagesRatio"] < 0).sum())
    flat = int((merged["ChagesRatio"] == 0).sum())
    comp_display = {kk: round(v) for kk, v in components.items() if not kk.startswith("_")}
    write_json("market.json", {
        "updated": updated, "score": score, "kospi": kospi,
        "breadth": {"up": up, "down": down, "flat": flat},
        "indicators": indicators,
        "components": comp_display,
    })
    # 휴장일 가드: KOSPI 최종 데이터 날짜가 오늘이 아니면(공휴일 실행·수동 주말 실행)
    # 전일 값이 중복 누적되어 z-score 정규화 이력을 오염시키므로 history 누적만 생략.
    # market.json 등 화면 데이터 갱신은 그대로 수행(멱등).
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    if kospi and kospi.get("date") == today_kst:
        append_history(score, kospi, indicators, components)
    else:
        print(f"  ℹ️ KOSPI 최종 거래일({kospi.get('date') if kospi else '?'}) ≠ 오늘({today_kst}) "
              f"— 휴장일로 판단해 history 누적 생략")

    # ── 뉴스: 추천 종목만 수집 ──
    news = collect_news(rec_list) if rec_list else {}

    write_json("screening.json", {
        "updated": updated, "targetYear": target_year,
        "hasFundamentals": dart is not None,
        "analyzedN": len(results), "recMax": rec_max,
        "results": results,
    })
    write_json("charts.json", {"updated": updated, "charts": charts})
    write_json("news.json", {"updated": updated, "items": news})
    write_json("signals.json", {"updated": updated, "topN": signal_n, "rows": signals})
    print(f"✅ 완료 — 분석 {len(results)}종목 · 추천 {len(rec_list)}종목 · 신호 {len(signals)}종목")


# ============================================================
# 샘플 데이터 (로컬 미리보기용 — 외부 통신 없음)
# ============================================================

def build_sample():
    print("🧪 샘플 데이터 생성 (미리보기용)")
    rng = random.Random(42)
    updated = now_str() + " (샘플)"
    sectors_pool = ["반도체", "2차전지", "제약·바이오", "자동차", "금융", "인터넷", "조선", "화학", "유통", "엔터"]

    names = []
    prefix = ["한빛", "미래", "국민", "대한", "신성", "글로벌", "퍼스트", "코어", "넥스트", "정상",
              "동방", "세종", "한울", "청담", "백두", "은하", "가온", "누리", "다온", "라온"]
    suffix = ["전자", "바이오", "화학", "중공업", "소재", "테크", "제약", "금융", "모빌리티", "에너지"]
    for p in prefix:
        for s in suffix:
            names.append(p + s)
    rng.shuffle(names)

    prices, sec_rows, results, charts, signals = [], [], [], {}, {}
    news = {}
    for i, name in enumerate(names[:180]):
        code = f"{100000 + i * 137 % 900000:06d}"
        base = rng.choice([8000, 15000, 32000, 54000, 71000, 120000, 260000])
        chg = round(rng.gauss(0, 1.8), 2)
        price = int(base * (1 + chg / 100))
        sector = sectors_pool[i % len(sectors_pool)]
        marcap_e = int(abs(rng.gauss(30000, 40000))) + 1500
        prices.append([code, name, price, chg])
        if i < 150:
            sec_rows.append([name, sector, marcap_e, chg])
        atr = round(price * rng.uniform(0.015, 0.05), 1)
        signals[code] = [price, atr, 1 if rng.random() < 0.12 else 0, 1 if rng.random() < 0.06 else 0,
                         round(price * rng.uniform(1.0, 1.12))]  # 20일 최고 종가(현재가 이상)

        if i < 60:  # 스크리닝 분석 샘플
            trend = rng.random() < 0.62
            rsi = round(rng.uniform(25, 78), 1)
            vol_ratio = round(rng.uniform(0.5, 3.4), 2)
            up_day = rng.random() < 0.55
            ratio52 = round(rng.uniform(55, 99.5), 1)
            gc = trend and rng.random() < 0.2
            pullback = trend and 35 <= rsi <= 55 and rng.random() < 0.5
            breakout = trend and ratio52 >= 92 and vol_ratio >= 1.5 and up_day
            setup_score = 0
            if breakout:
                setup_score = min(100, 60 + (15 if gc else 0) + min(20, int((vol_ratio - 1.5) * 10)))
            elif pullback:
                setup_score = min(100, 60 + (15 if gc else 0) + max(0, 10 - int(abs(rsi - 45))))
            t = {"price": price, "diff": int(price * chg / 100), "trend": trend,
                 "gcRecent": gc, "gcDate": "2026-07-13" if gc else "-",
                 "rsi": rsi, "volRatio": vol_ratio, "upDay": up_day,
                 "high52Ratio": ratio52, "pullback": pullback, "breakout": breakout,
                 "setupScore": setup_score}
            status, reason = classify(t)
            fund_pass = rng.random() < 0.55
            entry = {"code": code, "name": name, "fundPass": fund_pass,
                     "status": status, "reason": reason, **t}
            if fund_pass:
                multiple = round(rng.uniform(4, 28), 1)
                f_score = min(100, (40 if multiple <= 10 else 25 if multiple <= 15 else 10)
                              + rng.choice([10, 20, 35]) + rng.choice([0, 8, 15]))
                entry.update({
                    "marcap": marcap_e, "multiple": multiple,
                    "growthCurr": f"{round(rng.uniform(5, 80), 1)}%",
                    "growthPrev": f"{round(rng.uniform(-10, 50), 1)}%",
                    "trait": rng.choice(["🚀성장가속 📈연속성장", "📈연속성장", "✅반등/전환", "🔄흑자전환"]),
                    "opProfit": int(marcap_e / multiple), "profitDelta": rng.randint(50, 3000),
                    "fundScore": f_score,
                })
                entry["score"] = round(f_score * 0.4 + setup_score * 0.6)
            else:
                entry["score"] = round(setup_score * 0.6)
            results.append(entry)
            # 랜덤워크 차트
            dates, closes = [], []
            p = price * rng.uniform(0.75, 0.9)
            d = datetime.now(KST) - timedelta(days=170)
            while len(dates) < 120:
                d += timedelta(days=1)
                if d.weekday() >= 5:
                    continue
                p *= math.exp(rng.gauss(0.0008, 0.02))
                dates.append(d.strftime("%Y-%m-%d"))
                closes.append(round(p))
            def sma(arr, w):
                return [None if j < w - 1 else round(sum(arr[j - w + 1: j + 1]) / w) for j in range(len(arr))]
            charts[code] = {"dates": dates, "close": closes, "ma20": sma(closes, 20), "ma60": sma(closes, 60)}

    eligible = [e for e in results if e["fundPass"] and e["status"] in ("pull", "brk")]
    eligible.sort(key=lambda e: e["score"], reverse=True)
    for rank, e in enumerate(eligible[:10], 1):
        e["rec"] = True
        e["rank"] = rank
        news[e["code"]] = {"items": [
            [f"{e['name']}, 2분기 실적 시장 기대치 상회", "샘플경제", "07-14", "https://example.com"],
            [f"{e['name']} 신제품 출시에 증권가 목표가 상향", "샘플투데이", "07-13", "https://example.com"],
            [f"외국인, {e['name']} 사흘째 순매수", "샘플뉴스", "07-12", "https://example.com"],
        ], "senti": [2, 0, 1]}

    write_json("prices.json", {"updated": updated, "rows": prices})
    write_json("sectors.json", {"updated": updated, "rows": sec_rows})
    sample_score = rng.randint(20, 80)
    write_json("market.json", {
        "updated": updated, "score": sample_score,
        "kospi": {"close": 3124.56, "change": 0.84},
        "breadth": {"up": 98, "down": 71, "flat": 11},
        "indicators": [
            ["KOSDAQ", 892.41, 1.12, ""], ["S&P 500", 6871.22, -0.34, ""],
            ["나스닥", 22984.15, -0.87, ""], ["다우존스", 44120.55, 0.21, ""],
            ["달러/원", 1342.50, 0.45, "원"], ["금(온스)", 3310.80, 0.62, "$"],
            ["WTI 유가", 71.34, -1.05, "$"],
        ],
        "components": {
            kk: max(0, min(100, sample_score + rng.randint(-18, 18)))
            for kk in ["모멘텀", "변동성", "안전자산", "가격강도", "시장폭"]
        },
    })
    write_json("screening.json", {
        "updated": updated, "targetYear": 2025, "hasFundamentals": True,
        "analyzedN": len(results), "recMax": 10, "results": results,
    })
    write_json("charts.json", {"updated": updated, "charts": charts})
    write_json("news.json", {"updated": updated, "items": news})
    write_json("signals.json", {"updated": updated, "topN": 300, "rows": signals})
    hist, sc = [], 50
    g = {"KOSDAQ": 880.0, "나스닥": 22800.0, "달러/원": 1340.0, "금(온스)": 3300.0, "WTI 유가": 71.0}
    for d in range(45, 0, -1):
        day = datetime.now(KST) - timedelta(days=d)
        if day.weekday() >= 5:
            continue
        sc = max(5, min(95, sc + rng.randint(-8, 8)))
        for k in g:
            g[k] = round(g[k] * (1 + rng.gauss(0.0005, 0.011)), 2)
        hist.append([day.strftime("%Y-%m-%d"), sc,
                     round(3000 + sc * 5 + rng.uniform(-30, 30), 2), dict(g),
                     {"strength": round(rng.uniform(-0.8, 0.8), 4),
                      "breadth": round(rng.uniform(0.25, 0.75), 4)}])
    write_json("history.json", {"rows": hist})
    print("✅ 샘플 생성 완료 — 로컬에서 'python -m http.server' 후 확인하세요")


if __name__ == "__main__":
    if "--sample" in sys.argv:
        build_sample()
    else:
        build_real()
