# -*- coding: utf-8 -*-
"""한국은행 「경제금융용어 800선」 PDF → data/terms.json 변환 (1회성 도구)

사용법: python extract_terms.py <PDF경로>
판별 규칙(2026년판 기준):
  - 용어 제목  : SUIT-ExtraBold / DIN-Bold, 14pt
  - 본문       : KoPubBatang* 10.5pt
  - 연관검색어  : 라벨 KoPubDotumBold 9pt, 값 KoPubDotumMedium 9.5pt
  - 러닝헤드/쪽번호/색인 문자(ㄱㄴㄷ…)는 폰트로 걸러냄
"""
import sys, json, random, re
import fitz

HEAD_FONTS = ("SUIT-ExtraBold", "DIN-Bold")
BODY_FONTS = ("KoPubBatang",)
REL_FONT = "KoPubDotumMedium"
REL_LABEL = "연관검색어"


def smart_append(acc, line):
    """줄 이어붙이기: 한글은 어디서든 줄바꿈되므로 무공백 접합이 원칙이나,
    영문·숫자 단어가 줄 경계에서 만나면 원문에 있던 공백이 소실됨 → 라틴 경계에만 공백 복원.
    (하이픈 줄바꿈 'Network-' + 'Based'는 공백 없이 접합)"""
    if acc and re.search(r"[A-Za-z,;:.)]$", acc) and re.match(r'[A-Za-z(“"]', line):
        return acc + " " + line
    return acc + line


def trim_diagram_tail(d):
    """페이지 내 개념도(그림) 텍스트가 본문과 동일 폰트라 끝에 혼입되는 경우 제거.
    마지막 완결 어미('다.') 이후의 꼬리 중, 인용 괄호로 시작하지 않는 짧은 조각만 잘라냄
    — '(…를 참조)' 같은 정당한 인용 꼬리는 보존."""
    m = re.search(r"^(.*다\.)\s*(.+)$", d, re.S)
    if m:
        tail = m.group(2).strip()
        # 완결 어미로 끝나는 꼬리는 진짜 마지막 문장 — 정규식 백트래킹이
        # 끝에서 두 번째 '다.'로 물러나 마지막 문장을 꼬리로 오인하는 것 방지
        keep = (tail.startswith("(")            # 인용 괄호 꼬리
                or tail.endswith(".")            # 마침표로 끝나는 완결 문장
                or re.fullmatch(r'[)"\u201d』.]+', tail))  # 원문의 닫는 문장부호
        if not keep and len(tail) < 80:
            return m.group(1)
    return d


# 알려진 띄어쓰기 소실 교정 (원문 대조로 확인된 건만 등재)
# 한글 줄바꿈은 단어 중간에서도 일어나 무공백 접합이 원칙이나, 드물게
# 줄 경계에서 원문 공백이 레이아웃에 흡수되는 경우가 있음 — 규칙 구분 불가.
KNOWN_SPACING_FIXES = {
    "CCyB)을운용": "CCyB)을 운용",
}


def apply_known_fixes(d):
    for a, b in KNOWN_SPACING_FIXES.items():
        d = d.replace(a, b)
    return d


def line_class(spans):
    fonts = [s["font"] for s in spans]
    sizes = [round(s["size"], 1) for s in spans]
    text = "".join(s["text"] for s in spans).strip()
    if not text:
        return None, ""
    if all(any(f.startswith(h) for h in HEAD_FONTS) for f in fonts) and max(sizes) >= 13:
        return "head", text
    if any(REL_LABEL in s["text"] for s in spans):
        return "rel_label", text
    if all(f.startswith(REL_FONT) for f in fonts) and max(sizes) <= 9.6:
        return "rel", text
    if any(f.startswith(b) for b in BODY_FONTS for f in fonts):
        return "body", text
    return None, ""  # 러닝헤드·쪽번호·색인문자 등


def extract(path, p_start=18, p_end=423):
    doc = fitz.open(path)
    terms, cur = [], None
    mode = "body"
    for pno in range(p_start, min(p_end, len(doc))):
        d = doc[pno].get_text("dict")
        lines = []
        for b in d["blocks"]:
            for l in b.get("lines", []):
                if l["spans"]:
                    lines.append((l["bbox"][1], l["spans"]))
        lines.sort(key=lambda x: x[0])
        for _, spans in lines:
            cls, text = line_class(spans)
            if cls == "head":
                # 직전 줄도 제목이면 긴 용어의 줄바꿈 → 이어붙임
                if cur and mode == "head_cont":
                    cur["t"] += text
                    continue
                if cur:
                    terms.append(cur)
                cur = {"t": text, "d": "", "r": []}
                mode = "head_cont"
            elif cls == "body" and cur:
                cur["d"] = smart_append(cur["d"], text)
                mode = "body"
            elif cls == "rel_label":
                mode = "rel"
                # 라벨과 값이 같은 줄에 있는 판형: 라벨 뒤 텍스트를 값으로 수용
                rest = text.split(REL_LABEL, 1)[-1].strip(" :·")
                if cur and rest:
                    cur["r"] += [w.strip() for w in rest.split(",") if w.strip()]
            elif cls == "rel" and cur and mode == "rel":
                cur["r"] += [w.strip() for w in text.split(",") if w.strip()]
            else:
                if mode == "head_cont":
                    mode = "body"
    if cur:
        terms.append(cur)
    # 정리: 설명 공백 정규화, 빈 항목 제거
    out = []
    for t in terms:
        d = re.sub(r"\s+", " ", t["d"]).strip()
        d = re.sub(r"([다음됨함임략]\.)([가-힣(0-9A-Za-z“])", r"\1 \2", d)  # 문장 경계 공백 복원
        d = apply_known_fixes(trim_diagram_tail(d))
        if len(t["t"]) >= 2 and len(d) >= 40:
            out.append({"t": t["t"].strip(), "d": d, "r": t["r"][:6]})
    return out


if __name__ == "__main__":
    terms = extract(sys.argv[1])
    random.Random(800).shuffle(terms)  # 고정 시드 셔플 — 가나다 이웃이 연달아 나오지 않게
    payload = {"src": "한국은행 「경제금융용어 800선」", "n": len(terms), "terms": terms}
    with open("terms.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"✅ terms.json — {len(terms)}개 용어")
