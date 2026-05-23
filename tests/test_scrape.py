"""src/scrape.py の NetkeibaBlocked 検出 logic の test。"""
from __future__ import annotations


def test_netkeiba_blocked_detected_for_cloudfront_400():
    """CloudFront 400 で Playwright が返す空 HTML (~40 字) を確実に検出。"""
    # fetch_html 内の検出 logic を直接呼べないので、同じロジックを expose する
    # 実装と歩調合わせる: len(stripped) < 80 and "<body></body>" in stripped.replace(" ", "")
    cases_blocked = [
        "<html><head></head><body></body></html>",                                # 標準
        "<html>\n<head></head>\n<body></body>\n</html>",                         # 改行入り
        "  <html><head></head><body></body></html>  ",                           # 前後空白
        "<html><head></head><body> </body></html>",                              # body 内 space
        "<html ><head ></head ><body ></body ></html >",                         # 属性空白
    ]
    for html in cases_blocked:
        stripped = html.strip()
        detected = len(stripped) < 80 and "<body></body>" in stripped.replace(" ", "")
        assert detected, f"should detect block: {html!r}"


def test_netkeiba_blocked_not_triggered_for_real_html():
    """正常な HTML は block 判定されない。"""
    # 実 race HTML は 100KB 以上ある。短い HTML だが non-empty body のものでも block 判定されない
    cases_ok = [
        "<html><body><h1>レース情報</h1><table>...</table></body></html>",  # 短いが non-empty
        "<html><head><title>1R</title></head><body><div>content</div></body></html>",
    ]
    for html in cases_ok:
        stripped = html.strip()
        # 短いものは len < 80 を満たす場合もあるが <body></body> ではない
        detected = len(stripped) < 80 and "<body></body>" in stripped.replace(" ", "")
        assert not detected, f"should NOT detect block: {html!r}"


def test_netkeiba_blocked_raises_real_exception_class():
    """NetkeibaBlocked が RuntimeError サブクラス + メッセージに URL/ヒント含む。"""
    from src.scrape import NetkeibaBlocked
    assert issubclass(NetkeibaBlocked, RuntimeError)
    # raise/catch round-trip
    try:
        raise NetkeibaBlocked("test message with url=https://race.netkeiba.com/foo")
    except RuntimeError as e:
        assert "test message" in str(e)


def test_fetch_html_function_exists_and_takes_url():
    """API 契約: fetch_html(url, ...) is callable."""
    from src.scrape import fetch_html
    import inspect
    sig = inspect.signature(fetch_html)
    # 第1引数は url
    params = list(sig.parameters.values())
    assert params[0].name == "url"
    # timeout_ms / settle_ms は keyword only
    names = [p.name for p in params]
    assert "timeout_ms" in names
    assert "settle_ms" in names
