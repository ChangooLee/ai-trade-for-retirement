"""DART 악재 분류기 검증 — sign-inversion 수정(네거티브/회복 레이어) + CRIT/WARN + 정규화.
설계 워크플로(2026-06-16)가 지정한 단위검증. 가운뎃점 2종(ㆍ U+318D, · U+00B7)·공백 변형 포함.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.dart import filter as F

DOT1, DOT2 = "ㆍ", "·"   # ㆍ / ·


def _variants(s):
    """공백 삽입 + 가운뎃점 2종 + 점없음 변형 생성(정규화가 모두 흡수해야 함)."""
    base = s.replace(DOT1, "·")
    outs = {base, base.replace("·", DOT1), base.replace("·", DOT2), base.replace("·", "")}
    outs |= {x.replace("계약", "계 약").replace("발행", "발 행") for x in list(outs)}  # 공백 변형
    return outs


# ── 네거티브/회복 레이어: 회복·호재성 공시는 None(과거 sign-inversion FP) ──
def test_recovery_and_bullish_are_none():
    for nm in ["주권매매거래정지해제", "불성실공시법인미지정", "상장적격성실질심사대상제외",
               "관리종목지정해제", "전환청구권행사", "만기전사채취득",
               "단일판매·공급계약체결", "자기주식취득결정", "자기주식취득신탁계약체결결정",
               "무상증자결정", "전환사채권발행결과(자율공시)", "소송등의판결·결정(기각)"]:
        for v in _variants(nm):
            assert F.classify(v) is None, f"FP(과오분류): {v!r} → {F.classify(v)}"


# ── CRIT: 상폐·거래정지급 ──
def test_crit_events():
    for nm in ["상장폐지결정", "상장적격성실질심사대상결정", "기업심사위원회심의결과",
               "사채원리금미지급발생", "감사의견거절", "감사보고서(의견거절)",
               "횡령·배임혐의발생", "회생절차개시신청", "완전자본잠식",
               "불성실공시법인지정", "주권매매거래정지(상장적격성)"]:
        for v in _variants(nm):
            assert F.classify(v) == "crit", f"CRIT 미검출: {v!r} → {F.classify(v)}"


# ── WARN: 희석·수급 악재(표시만) ──
def test_warn_events():
    for nm in ["유상증자결정", "전환사채권발행결정", "신주인수권부사채권발행결정",
               "감자완료", "주식병합결정", "무상감자결정",
               "단일판매·공급계약해지", "최대주주변경", "소송등의제기", "영업정지"]:
        for v in _variants(nm):
            assert F.classify(v) == "warn", f"WARN 미검출: {v!r} → {F.classify(v)}"


# ── 정정괄호 제거 후 본문 재분류 ──
def test_correction_bracket_stripped():
    assert F.classify("[첨부정정]주요사항보고서(전환사채권발행결정)") == "warn"
    assert F.classify("[기재정정]상장폐지결정") == "crit"


# ── 핵심 구분: 계약'체결'(호재)=None vs 계약'해지/해제'(악재)=warn ──
def test_contract_signing_vs_termination():
    assert F.classify("단일판매·공급계약체결") is None
    assert F.classify("단일판매·공급계약해지") == "warn"
    assert F.classify("단일판매·공급계약해제") == "warn"
    # 제3자배정 유상증자는 경험상 +CAR → WARN 제외(None)
    assert F.classify("유상증자결정(제3자배정)") is None


# ── crit_tickers 헬퍼 ──
def test_crit_tickers_helper():
    flags = {"000001": {"crit": ["..."], "warn": []},
             "000002": {"crit": [], "warn": ["..."]},
             "000003": {"crit": [], "warn": []}}
    assert F.crit_tickers(flags) == {"000001"}


# ── 터미널급(자동 제외) vs 소프트 crit(표시만) ──
def test_terminal_subset():
    for nm in ["상장폐지결정", "상장적격성실질심사대상결정", "주권매매거래정지(상장적격성)",
               "감사의견거절", "사채원리금미지급발생", "회생절차개시신청", "완전자본잠식"]:
        assert F.is_terminal(nm), f"터미널 미검출: {nm}"
    # 소프트 crit — 표시(crit)는 되나 터미널 아님(자동 제외 안 함)
    for nm in ["횡령·배임혐의발생", "불성실공시법인지정", "관리종목지정(자본잠식률50%이상)"]:
        assert F.classify(nm) == "crit" and not F.is_terminal(nm), f"소프트 crit 오판: {nm}"
    # 출자법인 회생/파산 = 자기 상폐 아님 → 터미널 제외
    assert not F.is_terminal("출자법인회생절차및파산관련결정")
    # 회복/호재는 애초에 crit 아님
    assert not F.is_terminal("주권매매거래정지해제")


def test_terminal_tickers_helper():
    flags = {"A": {"crit": ["x"], "warn": [], "terminal": ["x"]},
             "B": {"crit": ["y"], "warn": [], "terminal": []},   # 소프트 crit
             "C": {"crit": [], "warn": ["z"], "terminal": []}}
    assert F.terminal_tickers(flags) == {"A"}
    assert F.crit_tickers(flags) == {"A", "B"}
